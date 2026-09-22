"""CFTC Commitments of Traders positioning in CME Bitcoin futures.

The weekly Traders in Financial Futures report is the one free, official
breakdown of WHO holds the regulated Bitcoin future: dealers, asset managers,
leveraged funds (hedge funds and CTAs), other reportables and the small
non-reportable accounts. It is the institution-side positioning read that the
venue-side one (Hyperliquid's leaderboard) and the price-side one (the CME
basis) do not carry.

Read from the CFTC's public Socrata API (``publicreporting.cftc.gov``), no key.
The dataset id and the column names are not a stable contract; both are
module constants with the date they were last checked, and a 404 on the
dataset is reported as the id having moved rather than as a network failure.

**As-of is the publication date, not the report date.** Each report is as of
Tuesday's close and is published on Friday afternoon (15:30 ET). A run on a
Wednesday or Thursday therefore must serve the PREVIOUS week's report: the
new one exists as a fact about Tuesday but nobody could have read it yet.
Filtering on the report date would leak three days of the future into every
mid-week backtest, so the filter is on a publication date the module
derives — the first Saturday after the report date (``publication_date``) —
and the report prints both dates and says the second is derived. Saturday
rather than Friday because the release is Friday evening in UTC and
``curr_date`` has no hour: on Friday itself a report has to be withheld from
the one cycle that could see it rather than served to the five that could
not. Anchored to the weekday, not to "report date + 4", because in a
holiday week the report itself is as of Monday, and +4 from a Monday is the
Friday the fourth day exists to avoid. What the derived date does NOT cover
is a release delayed past Saturday — a holiday Friday, or the weeks-late
batch releases after the late-2025 US government shutdown — where the truth
is later than the derived date and a backtest of that window sees a report
a few days early; the dataset carries no release-date column to do better
with, and the report's "derived" label is the disclosure.

One contract only: the 5-BTC standard future (code 133741). The Micro
contract is a separate series with a different holder mix and is not
merged in; the report says so.

The whole BTC series is small (441 weekly rows on 2026-09-21, back to
2018-04), so one fetch serves every question: it is cached as a rolling JSON
snapshot through the family's skeleton
(``sosovalue_common.load_rolling_snapshot``), refreshed daily, served stale
for up to ``MAX_STALE_DAYS`` when the fetch fails, and never written on a
failed fetch. Holding the series also pays for the scale: the report says
what size of weekly change is ordinary and where each net level sits in its
own trailing year, computed from the rows it already has.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

import requests

from . import sosovalue_common as family
from .errors import VendorError, VendorRateLimitError, VendorUnavailableError
from .sosovalue_common import (
    _cache_dir,
    _cache_rejecter,
    _read_cache_preamble,
    _stale_caveat,
    load_rolling_snapshot,
)
from .symbol_utils import classify_crypto_asset
from .utils import (
    data_lag_note,
    date_refusal,
    echo_argument,
    failure_account,
    is_unreached,
    json_body_or_outage,
    quote_argument,
    raise_for_http_status,
)

logger = logging.getLogger(__name__)

VENDOR = "CFTC"

# Traders in Financial Futures, futures only. Checked 2026-09-21: the dataset
# id, the contract code and every column below were read back from a live
# query. A Socrata dataset id is an assignment, not a promise.
DATASET_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
# "BITCOIN - CHICAGO MERCANTILE EXCHANGE", the 5-BTC contract. The Micro
# (133742) and the Coinbase nano contracts are other series.
CONTRACT_CODE = "133741"
CONTRACT_UNITS = "5 BTC"

REQUEST_TIMEOUT = 30
# Socrata pages at 1000 rows by default; the series has a few hundred, and a
# cap makes a runaway answer a contract break rather than a memory bill.
MAX_ROWS = 2000

# Reports are published Friday 15:30 ET, Friday evening in UTC; the derived
# publication date is the first Saturday after the report date (see the
# module docstring for why a weekday and why Saturday). A normal Tuesday
# report derives to +4 days; a holiday-week Monday report to +5.
PUBLICATION_WEEKDAY = 5  # Saturday, in ``date.weekday()`` terms

# The change columns compare against the reports this many places back in
# the published series, and the columns are labelled with the comparison
# report's date rather than with "weeks": the CFTC has never skipped a week
# in 441 rows (it shifts the report day to Monday around holidays instead),
# but a row this module could not read would leave a gap, and "1-week"
# over a gap is a lie the label would tell on its own.
TREND_REPORTS = 4

# The trailing window the scale is measured over: a year of weekly reports.
SCALE_REPORTS = 52
# Fewer published reports than this and no scale is printed: a median over
# a handful of changes is not a scale.
MIN_SCALE_REPORTS = 13

# The categories the report reasons about, with the dataset's column stems.
# Order is display order. Non-reportables carry no spreading column.
CATEGORIES = (
    ("Dealer", "dealer_positions", "_all"),
    ("Asset manager", "asset_mgr_positions", ""),
    ("Leveraged funds", "lev_money_positions", ""),
    ("Other reportables", "other_rept_positions", ""),
    ("Non-reportable", "nonrept_positions", "_all"),
)
# The three the closing line names: the institutional split. The other two
# are in the table for completeness, not in the reading.
HEADLINE_CATEGORIES = ("Dealer", "Asset manager", "Leveraged funds")

# Rendered against curr_date: a report is usually 3 to 4 days old when read,
# and up to 10 in the week a holiday delays the next one. Past this the
# report carries the family's lag line.
MAX_DATA_LAG_DAYS = 14
# Past this the newest publishable report is too old to describe the
# present, and the whole report is withheld: three missed weeks.
MAX_STALENESS_DAYS = 21

CACHE_TTL_HOURS = 24
# A fetch that failed keeps serving the cache for this long.
MAX_STALE_DAYS = 21
CACHE_SCHEMA = 1

# What the report says the leveraged-fund short usually is. In the report's
# Method line and in the news analyst's hint, both read from here so the two
# cannot drift apart. Supported by the series: leveraged funds were net short
# in 52 of the last 52 reports to 2026-09-15 (dealers were net LONG in all
# 52, which is why the report no longer calls them "the sell side").
CARRY_NOTE = (
    "A large leveraged-fund short is usually the futures leg of a cash-and-carry trade "
    "against spot or ETF holdings — the carry the futures basis pays — not a bearish view"
)

# CME lists Bitcoin and Ether futures; only Bitcoin is wired, and another
# asset's positioning is its own contract's, not a proxy of this one.
SUPPORTED_ASSETS = frozenset({"BTC"})


class CftcError(VendorError):
    """The CFTC answered, but not with the report this module can read."""


class CftcUnavailableError(CftcError, VendorUnavailableError):
    """The CFTC could not be reached or answered without data."""


class CftcRateLimitError(CftcError, VendorRateLimitError):
    """The CFTC is rate limiting this client."""


class Category(NamedTuple):
    name: str
    long: int
    short: int
    spreading: int | None

    @property
    def net(self) -> int:
        return self.long - self.short


class Report(NamedTuple):
    """One week's report, as the module keeps it."""

    report_date: date
    published: date
    open_interest: int
    categories: tuple[Category, ...]

    def category(self, name: str) -> Category:
        return next(c for c in self.categories if c.name == name)


def _utc_now() -> datetime:
    """The family's clock, read through its module so one patch moves both.

    The snapshot skeleton stamps ``fetched_at`` and judges TTL and staleness
    on ``sosovalue_common``'s clock; a clock of this module's own would be a
    second seam beside it, and the rendered cache age could then disagree
    with the decision to serve it (the split #279 measured on farside).
    """
    return family._utc_now()


def _rate_limited(_response) -> None:
    raise CftcRateLimitError(f"{VENDOR} is rate limiting this client (HTTP 429)")


def _request() -> list:
    """GET the whole BTC series, newest first, or raise a typed failure.

    A 404 is the dataset id having moved, and is said so: a "not found" from
    a Socrata host is that, never a symbol the vendor does not cover.
    """
    params = {
        "$where": f"cftc_contract_market_code='{CONTRACT_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(MAX_ROWS),
    }
    try:
        response = requests.get(DATASET_URL, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            raise CftcError(
                f"{VENDOR} answered HTTP 404 for dataset {DATASET_URL.rsplit('/', 1)[-1]}: "
                f"the dataset id has probably moved (last checked 2026-09-21)"
            )
        raise_for_http_status(response, VENDOR, rate_limit=_rate_limited)
    except (requests.RequestException, VendorUnavailableError) as e:
        down = isinstance(e, VendorUnavailableError) or is_unreached(e)
        cls = CftcUnavailableError if down else CftcError
        raise cls(f"{VENDOR} request failed ({failure_account(e)})") from e
    try:
        payload = json_body_or_outage(response, VENDOR)
    except VendorUnavailableError as e:
        raise CftcUnavailableError(str(e)) from e
    if not isinstance(payload, list):
        raise CftcError(f"{VENDOR} returned a JSON {type(payload).__name__}, expected an array")
    if len(payload) >= MAX_ROWS:
        raise CftcError(f"{VENDOR} returned {len(payload)} rows for one contract; refusing to read")
    return payload


def _int(value: object) -> int | None:
    """A Socrata number cell (a string) as a non-negative int, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number < 0 or number > 1e12 or number != int(number):
        return None
    return int(number)


def _iso_day(value: object) -> str | None:
    """The date in a Socrata floating timestamp ("2026-09-15T00:00:00.000"), or None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _parse_rows(payload: list) -> tuple[list[dict], list[str]]:
    """The rows the module keeps, and the report dates of the rows it could not.

    A row missing any position column — spreading included, for the four
    categories that carry one — is dropped, never zeroed: a category rendered
    as flat because its column was absent would read as a fact. The dropped
    rows' report dates (where that much could be read) are returned and
    logged, because the one that matters is the NEWEST: dropped silently, the
    previous week would become "the newest report" with nothing in the
    report to say so. Rows are de-duplicated on the report date, keeping the
    first the payload listed, then sorted newest first.
    """
    rows: dict[str, dict] = {}
    dropped: list[str] = []
    for raw in payload:
        if not isinstance(raw, dict):
            dropped.append("?")
            continue
        report_date = _iso_day(raw.get("report_date_as_yyyy_mm_dd"))
        open_interest = _int(raw.get("open_interest_all"))
        cats: dict | None = {}
        for name, stem, suffix in CATEGORIES:
            long = _int(raw.get(f"{stem}_long{suffix}"))
            short = _int(raw.get(f"{stem}_short{suffix}"))
            spreading = (
                _int(raw.get(f"{stem}_spread{suffix}")) if name != "Non-reportable" else None
            )
            if long is None or short is None or (spreading is None) != (name == "Non-reportable"):
                cats = None
                break
            cats[name] = [long, short, spreading]
        if report_date is None or open_interest is None or cats is None:
            dropped.append(report_date or "?")
            continue
        rows.setdefault(
            report_date, {"report_date": report_date, "oi": open_interest, "cats": cats}
        )
    if dropped:
        logger.warning(
            "%s: dropped %d malformed row(s) of %d (report dates: %s)",
            VENDOR,
            len(dropped),
            len(payload),
            ", ".join(dropped),
        )
    if not rows:
        raise CftcError(f"{VENDOR} returned {len(payload)} rows and none was a readable report")
    return [rows[k] for k in sorted(rows, reverse=True)], sorted(dropped, reverse=True)


def _cache_path() -> str:
    return os.path.join(_cache_dir(), "cftc_cot_btc.json")


def _valid_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if _iso_day(row.get("report_date")) != row.get("report_date"):
        return False
    if _int(row.get("oi")) is None:
        return False
    cats = row.get("cats")
    if not isinstance(cats, dict) or set(cats) != {name for name, _, _ in CATEGORIES}:
        return False
    for name, values in cats.items():
        if not (isinstance(values, list) and len(values) == 3):
            return False
        long, short, spreading = values
        if _int(long) is None or _int(short) is None:
            return False
        if (spreading is None) != (name == "Non-reportable") or (
            spreading is not None and _int(spreading) is None
        ):
            return False
    return True


def _read_cache(path: str) -> dict | None:
    """A fully validated cached payload, or None: a rejected cache costs one fetch."""
    reject = _cache_rejecter(path, cache_name=f"{VENDOR} COT cache", log=logger)
    payload = _read_cache_preamble(path, reject=reject)
    if payload is None:
        return None
    if payload.get("schema") != CACHE_SCHEMA:
        return reject(f"schema {payload.get('schema')!r} is not {CACHE_SCHEMA}")
    if not isinstance(payload.get("fetched_at"), str):
        return reject("'fetched_at' is missing")
    rows = payload.get("rows")
    if not (
        isinstance(rows, list) and rows and len(rows) <= MAX_ROWS and all(map(_valid_row, rows))
    ):
        return reject("'rows' is missing, empty, oversized, or contains a malformed report")
    dates = [r["report_date"] for r in rows]
    if dates != sorted(set(dates), reverse=True):
        return reject("'rows' are not unique and newest first")
    dropped = payload.get("dropped")
    if not (isinstance(dropped, list) and all(isinstance(d, str) for d in dropped)):
        return reject("'dropped' is missing or not a list of report dates")
    return payload


def _fetch_all(_cached: dict | None) -> dict:
    # ``fetched_at`` is the skeleton's to stamp, on its own clock.
    rows, dropped = _parse_rows(_request())
    return {"schema": CACHE_SCHEMA, "rows": rows, "dropped": dropped}


class _Snapshot(NamedTuple):
    reports: list[Report]
    # Report dates of rows the parse could not read, newest first ("?" for a
    # row whose date could not be read either).
    dropped: list[str]
    fetched_at: str
    stale: bool


def publication_date(report_date: date) -> date:
    """The first Saturday after ``report_date``: the derived publication date."""
    return report_date + timedelta(days=(PUBLICATION_WEEKDAY - report_date.weekday()) % 7 or 7)


def _to_report(row: dict) -> Report:
    report_date = date.fromisoformat(row["report_date"])
    return Report(
        report_date=report_date,
        published=publication_date(report_date),
        open_interest=row["oi"],
        categories=tuple(
            Category(name, *row["cats"][name][:2], row["cats"][name][2])
            for name, _, _ in CATEGORIES
        ),
    )


def _load_snapshot() -> _Snapshot:
    payload, fetched_at, stale, _refetched = load_rolling_snapshot(
        path=_cache_path(),
        read_cache=_read_cache,
        fetch_all=_fetch_all,
        ttl_hours=lambda _cached: CACHE_TTL_HOURS,
        label="COT",
        cache_name=f"{VENDOR} COT cache",
        max_stale_hours=MAX_STALE_DAYS * 24,
        log=logger,
        vendor=VENDOR,
    )
    return _Snapshot(
        [_to_report(r) for r in payload["rows"]], payload["dropped"], fetched_at, stale
    )


def _humanize_age(fetched_at: str) -> str:
    stamp = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hours = (_utc_now() - stamp).total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f} hours"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole > 0 else "n/a"


def _signed(value: int) -> str:
    return f"{value:+,d}"


def _withheld(coin: str, curr_date: str, why: str) -> str:
    return (
        f"## CFTC Commitments of Traders — CME Bitcoin futures ({coin})\n"
        f"- Withheld for {curr_date}\n"
        f"\nThe positioning report is withheld for this date. {why} Treat {coin} "
        f"futures positioning as unavailable for {curr_date}; do not infer it from "
        f"another report."
    )


def _as_of(reports: list[Report], curr_day: date) -> list[Report]:
    """The reports PUBLISHED on or before ``curr_day``, newest first."""
    return [r for r in reports if r.published <= curr_day]


def _scale_line(published: list[Report]) -> str:
    """What size of change is ordinary, and where each headline net sits in its year.

    Measured from the reports the analysis date could see, over the trailing
    ``SCALE_REPORTS`` of them: the median and upper-quartile absolute change
    in net between consecutive reports, and the range of net as a share of
    open interest. Without it a model reads "+1,538" with only its priors to
    say whether that is large — over the year to 2026-09-15 the leveraged-fund
    median was about 660 contracts, so it was.
    """
    window = published[: SCALE_REPORTS + 1]
    if len(window) < MIN_SCALE_REPORTS + 1:
        return (
            f"_Scale: withheld — only {len(window)} published reports are in the trailing "
            f"window, fewer than the {MIN_SCALE_REPORTS + 1} a scale needs._"
        )
    parts = []
    for name in HEADLINE_CATEGORIES:
        nets = [r.category(name).net for r in window]
        changes = sorted(abs(a - b) for a, b in zip(nets, nets[1:], strict=False))
        shares = sorted(
            r.category(name).net / r.open_interest * 100 for r in window if r.open_interest > 0
        )
        median = changes[len(changes) // 2]
        upper = changes[(len(changes) * 3) // 4]
        span = f"{shares[0]:+.1f}% to {shares[-1]:+.1f}%" if shares else "n/a"
        parts.append(
            f"{name.lower()}: a weekly change under about {median:,d} contracts is ordinary and "
            f"under {upper:,d} unremarkable, and net has ranged {span} of OI"
        )
    return (
        f"_Scale, over the {len(window) - 1} report-to-report changes before this one — "
        + "; ".join(parts)
        + "._"
    )


def get_futures_positioning(asset: str, curr_date: str) -> str:
    """Fetch CFTC COT positioning in CME Bitcoin futures as a markdown report.

    Args:
        asset: "BTC", or a pair form like "BTC-USD". Any other symbol gets a
            no-signal sentence: the report covers one contract, and another
            asset's positioning is not a proxy of it.
        curr_date: The analysis date (yyyy-mm-dd). The report served is the
            newest one whose derived publication date — the first Saturday
            after its report date — is on or before it, so a mid-week date
            sees the previous week's report, as a reader on that day did. An
            unusable date is refused with the shared ``INVALID_CURR_DATE``
            sentinel.

    Returns:
        A markdown report: report and publication dates, open interest, each
        category's long, short, spreading and net with its share of open
        interest and the change against the previous published report and
        against the one ``TREND_REPORTS`` back (each column labelled with the
        comparison report's date), the scale of ordinary weekly changes and
        the trailing-year range of each headline net, and a closing line on
        the three institutional categories. Withheld, with no figures, when
        no report had been published by ``curr_date`` or the newest one is
        more than ``MAX_STALENESS_DAYS`` old.

    Raises:
        CftcError and its subclasses: a throttle, an outage, a moved dataset
        or an unreadable answer, typed for the router. ``WiringGapError``
        from the shared cache-directory guard when this deployment's
        ``data_cache_dir`` is misconfigured: a project bug, not a vendor
        failure, and it leaves as one.
    """
    refusal = date_refusal(curr_date, what="futures positioning", kind="point")
    if refusal is not None:
        return refusal
    curr_day = datetime.strptime(curr_date, "%Y-%m-%d").date()
    curr_date = curr_day.isoformat()

    if asset and not isinstance(asset, str):
        raise CftcError(f"asset must be a symbol string, got {type(asset).__name__}")
    asset = echo_argument(asset)
    coin, is_proxy = classify_crypto_asset(asset, SUPPORTED_ASSETS)
    if coin is None or is_proxy:
        return (
            f"There is no futures-positioning signal for {quote_argument(asset)}: the CFTC "
            f"report this tool reads covers the CME Bitcoin future only. Do not substitute "
            f"BTC's positioning for another asset's."
        )

    snapshot = _load_snapshot()
    published = _as_of(snapshot.reports, curr_day)
    if not published:
        return _withheld(
            coin,
            curr_date,
            f"No report in the series had been published by {curr_date} (the oldest report "
            f"is as of {snapshot.reports[-1].report_date.isoformat()}).",
        )
    current = published[0]
    prior = published[1] if len(published) > 1 else None
    trend_base = published[TREND_REPORTS] if len(published) > TREND_REPORTS else None
    stale_days = (curr_day - current.published).days
    if stale_days > MAX_STALENESS_DAYS:
        return _withheld(
            coin,
            curr_date,
            f"The newest report published by {curr_date} is as of "
            f"{current.report_date.isoformat()} (published about "
            f"{current.published.isoformat()}), {stale_days} days earlier; more than "
            f"{MAX_STALENESS_DAYS} days is not a description of the present.",
        )
    # Rows the parse dropped that a reader on curr_date would have seen: the
    # newest of them, if it is newer than the report served, is the one that
    # makes this report older than the CFTC's — said, since nothing else
    # would (the lag line needs two missed weeks to fire).
    missed_newer = [
        d
        for d in snapshot.dropped
        if d != "?"
        and d > current.report_date.isoformat()
        and publication_date(date.fromisoformat(d)) <= curr_day
    ]

    lines = [
        f"## CFTC Commitments of Traders — CME Bitcoin futures ({coin})",
        f"- Report as of {current.report_date.isoformat()} "
        f"({current.report_date.strftime('%A')} close), published about "
        f"{current.published.isoformat()} (derived: the first Saturday after the report date) "
        f"| analysis date {curr_date} | {CONTRACT_UNITS} standard contract only, code "
        f"{CONTRACT_CODE}; the Micro contract is a separate series and is not included",
    ]
    if missed_newer:
        lines.append(
            f"_A newer report, as of {missed_newer[0]}, is in the CFTC's series but could not "
            f"be read (a malformed row), so the report below is older than the newest the CFTC "
            f"has published._"
        )
    lag = data_lag_note(
        current.published.isoformat(), curr_date, MAX_DATA_LAG_DAYS, "published COT report"
    )
    if lag:
        lines.append(lag)
    if snapshot.stale:
        lines.append(
            _stale_caveat(
                snapshot.fetched_at,
                "The series itself changes only weekly, so a stale cache is usually the same "
                "series; the risk is a missed publication.",
                causes="network error, a rate limit, a moved dataset, or a change in its shape",
                humanize=_humanize_age,
            )
        )
    lines.append("")
    oi_change = (
        ""
        if prior is None
        else (
            f" ({_signed(current.open_interest - prior.open_interest)} since the "
            f"{prior.report_date.isoformat()} report)"
        )
    )
    lines.append(f"**Open interest:** {current.open_interest:,d} contracts{oi_change}")
    lines.append("")
    week_head = (
        "Δ net vs previous report" if prior is None else f"Δ net vs {prior.report_date.isoformat()}"
    )
    trend_head = (
        f"Δ net vs {TREND_REPORTS} reports back"
        if trend_base is None
        else f"Δ net vs {trend_base.report_date.isoformat()}"
    )
    lines.append(
        f"| Category | Long | Short | Spreading | Net | Net % of OI | {week_head} | {trend_head} |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for cat in current.categories:
        spreading = "—" if cat.spreading is None else f"{cat.spreading:,d}"
        week = "n/a" if prior is None else _signed(cat.net - prior.category(cat.name).net)
        trend = (
            "n/a" if trend_base is None else _signed(cat.net - trend_base.category(cat.name).net)
        )
        lines.append(
            f"| {cat.name} | {cat.long:,d} | {cat.short:,d} | {spreading} | {_signed(cat.net)} "
            f"| {_pct(cat.net, current.open_interest)} | {week} | {trend} |"
        )
    lines.append("")
    if prior is None:
        lines.append("_No earlier published report in the series, so the change columns are n/a._")
    elif (current.report_date - prior.report_date).days != 7:
        lines.append(
            f"_The previous published report is {(current.report_date - prior.report_date).days} "
            f"days before this one, not a week: a report is missing from the series between "
            f"them, so the first change column spans more than one week._"
        )
    if trend_base is None:
        lines.append(
            f"_Fewer than {TREND_REPORTS + 1} published reports in the series, so the second "
            f"change column is n/a._"
        )
    lines.append(_scale_line(published))

    lines.append(
        f"_Method: positions are contracts held at the report date's close as reported to the "
        f"CFTC; net is long minus short, and spreading (offsetting long and short in different "
        f"months) is shown but not in net. {CARRY_NOTE}. The publication date is derived and "
        f"can be later than shown when a US holiday delays the release._"
    )
    since = "" if prior is None else f" since {prior.report_date.isoformat()}"
    headline = "; ".join(
        f"{name.lower()} net {_signed(current.category(name).net)} "
        f"({_pct(current.category(name).net, current.open_interest)} of OI"
        + (
            ""
            if prior is None
            else f", {_signed(current.category(name).net - prior.category(name).net)}{since}"
        )
        + ")"
        for name in HEADLINE_CATEGORIES
    )
    lines.append(
        f"_Reading: CME Bitcoin futures as of {current.report_date.isoformat()}, "
        f"{current.open_interest:,d} contracts of open interest — {headline}. Positioning is "
        f"a slow-moving, weekly, institution-side input: read the direction of the changes "
        f"and the net levels against open interest and against their own trailing year, and "
        f"never a single week's move as a standalone directional signal._"
    )
    return "\n".join(lines) + "\n"
