"""Farside spot-ETF flow vendor.

Scrapes BTC/ETH US spot-ETF daily net flows from farside.co.uk's HTML tables
(the site has no API). Surfaced to the news analyst as a crypto-only demand /
positioning signal: persistent ETF inflows or outflows are a flows narrative
that complements price, macro, and news.

Keyless. The site sits behind Cloudflare, so requests carry a browser-style
User-Agent; a WAF block (403) or any network error is treated like unavailable
data. Parsed flows go into one rolling cache file per asset, refreshed once the
cached snapshot is older than ``CACHE_TTL_HOURS``: a repeat call within that
window reuses it, and a fetch failure falls back to that same snapshot (marked
stale in the report) rather than losing the signal. A failed fetch is never
written to cache, and any structural mismatch that would make the *figures*
untrustworthy raises rather than returning a half-parsed table — the routing
layer degrades the optional crypto_etf_flows category to a sentinel. An
unreadable issuer header is the one non-fatal structural fault: the figures
survive it, so the report keeps them and discloses which issuer labels are
placeholders.

Note that the time-based fetch throttle is enforced only by that cache file
existing. A non-persistent ``data_cache_dir`` (tmpfs, a container with no
volume, CI) turns every call into a live fetch.

Backtest reach: each call serves the live farside.co.uk table (reflecting the
present, not curr_date) and then filters to rows on or before curr_date.
So a historical curr_date returns real rows only as deep as the live table
still lists — unlike a date-ranged API. The inflow/outflow streak is likewise
bounded by how much history that live table carries.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import requests
from parsel import Selector

from .config import get_config
from .errors import VendorError
from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

FARSIDE_BASE = "https://farside.co.uk"

# Per-asset page path on farside.co.uk.
ASSET_PATHS = {"BTC": "/btc/", "ETH": "/eth/"}

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Cloudflare serves a challenge/403 to non-browser clients; a normal desktop
# User-Agent gets the static table HTML.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Default trailing window when the caller does not specify one; a month captures
# the recent flow regime. It bounds the cumulative figure and the rendered table
# only — the streak is always computed over all available history — so changing
# this does not change streak sensitivity.
DEFAULT_LOOKBACK_DAYS = 30

# Row cap for the rendered recent-flows table, mirroring fred.MAX_ROWS so a long
# history does not flood the agent's context.
MAX_ROWS = 40

# Issuer columns to show in the latest-day breakdown (largest |flow| first).
TOP_ISSUERS = 5

# Maximum age (calendar days since the last successful fetch) a stale cached
# snapshot may be served for. Beyond this, a persistent fetch failure degrades to
# the router sentinel rather than presenting weeks-old flows as merely "STALE".
MAX_STALE_DAYS = 14

# How long a successfully-fetched snapshot may be reused before a fresh fetch is
# forced. Keyed on hours, not the UTC calendar day: Farside publishes day D's
# flows during US evening (~01:00-04:00 UTC on D+1), so a day-keyed cache pinned
# by the first cycle after 00:00 UTC would serve the pre-publication table for
# the whole day. On the 4-hour analyst cycle, 6h means most cycles reuse but the
# table is re-pulled within a cycle or two of the overnight print, without
# hammering farside.co.uk.
CACHE_TTL_HOURS = 6

# Maximum lag (days between the newest parsed row and curr_date) before the report
# flags the *data* as behind — a separate question from whether the *fetch*
# succeeded. farside.co.uk can serve a perfectly parseable page that simply has
# not been updated for days, which the stale-cache machinery above never sees.
# Farside posts on US trading days, so a weekend plus a holiday legitimately
# reaches 4 days; beyond that the feed itself has stalled.
MAX_DATA_LAG_DAYS = 4

# The parser assumes "last column = daily Total". To catch a trailing-column
# layout change (e.g. Farside appending a Cumulative column, which would silently
# shift Total into an issuer slot) we verify each row's last cell equals the sum
# of the issuer cells within rounding noise. Farside rounds every cell to 0.1, so
# a handful of issuers drifts ~1 at most; a wrong column is off by orders of
# magnitude. The relative slice keys off the trusted issuer sum, not the (possibly
# wrong) Total, so a huge bad Total can't widen its own tolerance.
_TOTAL_ABS_TOL = 2.0
_TOTAL_REL_TOL = 0.02

# Locale-independent English month lookup. Farside always renders English month
# abbreviations; parsing them by hand avoids strptime's %b, which resolves against
# the process-wide LC_TIME locale and would break every row if any other component
# changed the locale.
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}

# A cell text matching a Farside daily-flow date, e.g. "06 Jul 2026". Used to
# separate data rows from header / summary (Total, Average, Maximum, Minimum) rows.
_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")

# An issuer ticker header cell, e.g. "IBIT", "GBTC", "BTC".
_TICKER_RE = re.compile(r"^[A-Z]{2,6}$")

# Cells Farside uses for "no flow" / missing data.
_BLANK_CELLS = {"", "-", "–", "—"}


class FarsideError(VendorError):
    """Farside was unreachable, blocked, or its table structure changed.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing layer
    reacts by behaviour rather than by vendor, and the optional crypto_etf_flows
    category degrades to a sentinel instead of aborting the run.
    """


class _ParsedTable(NamedTuple):
    """A parsed Farside flow page."""

    records: list[dict]
    # True only when *every* issuer column resolved to a real ticker label. False
    # when the header row could not be identified at all, OR was found but is
    # missing one or more ticker cells — in either case the affected columns fall
    # back to ``unnamed col N`` placeholders. The flow numbers stay trustworthy
    # (the Total cross-check still guards them); only some *labels* are unknown,
    # and the report has to say so. A single table-level bool cannot distinguish
    # "all placeholders" from "some placeholders", but it does not need to: the
    # placeholder names are self-describing in the rendered breakdown, and the
    # caveat wording covers both.
    issuers_named: bool


class _FlowSnapshot(NamedTuple):
    """What ``_load_flows`` resolved for one asset.

    Named rather than a bare tuple so the ``str`` and ``bool`` fields cannot be
    swapped silently at the call site — a stray ``str`` in a flag slot is truthy
    and a stray ``bool`` in ``fetched_at`` degrades to "age unknown".
    """

    records: list[dict]
    fetched_at: str
    stale: bool
    issuers_named: bool


def _request_html(asset: str) -> str:
    """GET a Farside asset page with a browser User-Agent.

    ``raise_for_status`` turns a Cloudflare 403 (or any non-2xx) into a
    ``requests.HTTPError`` so the caller treats a WAF block like a network error.
    """
    response = requests.get(
        f"{FARSIDE_BASE}{ASSET_PATHS[asset]}",
        headers={"User-Agent": BROWSER_UA},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def _cell_text(cell) -> str:
    """Concatenate a table cell's text nodes (unwrapping the redFont negatives)."""
    return "".join(cell.css("::text").getall()).strip()


def _parse_flow_value(text: str) -> float:
    """Parse a Farside flow cell into US$m.

    Handles parenthesised negatives ``(44.5)`` -> -44.5, thousands separators
    ``1,119.9`` -> 1119.9, and blank / dash cells (no flow) -> 0.0. A non-blank
    cell that is not a number signals a structure change and raises FarsideError.
    """
    cleaned = text.strip()
    if cleaned in _BLANK_CELLS:
        return 0.0
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace(",", "").replace("$", "").strip()
    try:
        value = float(cleaned)
    except ValueError as e:
        raise FarsideError(
            f"Unparseable flow cell {text!r} (Farside table structure may have changed)"
        ) from e
    return -value if negative else value


def _parse_flow_date(text: str, asset: str) -> str:
    """Parse a 'DD Mon YYYY' Farside date to ISO (YYYY-MM-DD), locale-independently.

    Raises FarsideError on any malformed date (bad month token, impossible day)
    so a structure change degrades rather than silently mis-parsing.
    """
    parts = text.split()
    month = _MONTHS.get(parts[1].title()) if len(parts) == 3 else None
    if month is None:
        raise FarsideError(
            f"Unparseable date {text!r} in the {asset} table "
            f"(Farside table structure may have changed)"
        )
    try:
        return datetime(int(parts[2]), month, int(parts[0])).strftime("%Y-%m-%d")
    except ValueError as e:  # e.g. day out of range
        raise FarsideError(f"Unparseable date {text!r} in the {asset} table") from e


def _row_cells(tr) -> list[str]:
    return [_cell_text(c) for c in tr.css("td, th")]


def _table_has_data_rows(table) -> bool:
    return any(
        cells and _DATE_RE.match(cells[0]) for cells in (_row_cells(tr) for tr in table.css("tr"))
    )


def _find_issuer_header(
    rows: list[list[str]], first_data_idx: int, num_cols: int
) -> list[str] | None:
    """Return the issuer-ticker header row above the first data row, or None.

    The row whose middle cells (between the date and the Total column) are mostly
    ticker symbols — skips the logo/spanner row (empty text) and the Fee row
    (percentages). Returns None so the caller falls back to positional names.
    """
    for cells in rows[:first_data_idx]:
        if len(cells) != num_cols:
            continue
        middle = cells[1 : num_cols - 1]
        ticker_like = sum(1 for c in middle if _TICKER_RE.match(c))
        if middle and ticker_like >= max(2, len(middle) // 2):
            return cells
    return None


def _parse_flow_table(html: str, asset: str) -> _ParsedTable:
    """Parse the Farside flow table into per-day records (US$m).

    Returns the records — ``{"date": "YYYY-MM-DD", "issuers": {ticker: flow},
    "total": flow}`` sorted ascending by date — together with whether the issuer
    columns could be given real ticker names.

    Raises FarsideError on a structural mismatch that would make the *numbers*
    untrustworthy: no table carrying dated rows, fewer than 3 columns, a data row
    whose cell count disagrees with the first data row, an unparseable date or
    value, two rows sharing a date, or a last column that is not the sum of the
    issuer columns. The caller then degrades instead of receiving a half-parsed
    table.

    A missing (or partly-blank) issuer header is deliberately *not* fatal: the
    figures are still verified by the Total cross-check, so the affected columns
    fall back to ``unnamed col N`` names, the parser logs, and it reports
    ``issuers_named=False`` for the caller to disclose. Losing some labels is not
    worth discarding the whole flow signal.
    """
    sel = Selector(text=html)

    table = None
    for candidate in sel.css("table.etf"):
        if _table_has_data_rows(candidate):
            table = candidate
            break
    if table is None:
        # Fallback: the first table that actually contains dated flow rows, in
        # case the ``etf`` class is renamed but the structure survives.
        for candidate in sel.css("table"):
            if _table_has_data_rows(candidate):
                table = candidate
                break
    if table is None:
        raise FarsideError(f"No ETF flow table found on the {asset} page")

    rows = [_row_cells(tr) for tr in table.css("tr")]
    data_row_idxs = [i for i, cells in enumerate(rows) if cells and _DATE_RE.match(cells[0])]
    if not data_row_idxs:
        # Not reachable today: a table is only selected above when
        # _table_has_data_rows found a dated row using this same predicate. Kept
        # as a safety net so the two ever drifting apart fails loud here rather
        # than as an IndexError on rows[data_row_idxs[0]] below.
        raise FarsideError(f"No dated flow rows in the {asset} table")

    num_cols = len(rows[data_row_idxs[0]])
    if num_cols < 3:
        raise FarsideError(f"Unexpected column count {num_cols} in the {asset} table")

    header = _find_issuer_header(rows, data_row_idxs[0], num_cols)
    # Columns: 0 = date, last = Total, the rest = issuers. A column keeps its
    # header ticker only when that cell is present and non-empty; otherwise it
    # gets a self-describing ``unnamed col N`` placeholder (not a fake ``ETF{j}``
    # that reads as a real ticker). all_named is True only when every column
    # resolved a real label — a *found but partly-blank* header is not "named",
    # so the report's disclosure still fires.
    issuer_cols = list(range(1, num_cols - 1))
    issuer_names = []
    unnamed_cols = []
    for j in issuer_cols:
        label = header[j] if header else ""
        issuer_names.append(label or f"unnamed col {j}")
        if not label:
            unnamed_cols.append(j)
    all_named = not unnamed_cols
    if header is None:
        # Every other structural change in this parser raises; this one degrades,
        # so it must at least be loud in the logs. Otherwise a header-layout
        # change silently turns the issuer breakdown into meaningless placeholder
        # labels that read as real tickers to the agent.
        logger.warning(
            "Farside %s: could not identify the issuer-ticker header row; falling back to "
            "positional 'unnamed col N' names (the header layout may have changed)",
            asset,
        )
    elif unnamed_cols:
        # Header found but one or more ticker cells are blank: log which columns
        # fell back so a partial header change is diagnosable, not silent.
        logger.warning(
            "Farside %s: issuer header row found but columns %s have no ticker label; "
            "labelling them 'unnamed col N' (the header layout may have partially changed)",
            asset,
            unnamed_cols,
        )

    records = []
    for i in data_row_idxs:
        cells = rows[i]
        if len(cells) != num_cols:
            raise FarsideError(
                f"{asset} flow row {cells[0]!r} has {len(cells)} cells, expected "
                f"{num_cols} (table structure may have changed)"
            )
        day = _parse_flow_date(cells[0], asset)
        issuers = {
            name: _parse_flow_value(cells[j])
            for name, j in zip(issuer_names, issuer_cols, strict=True)
        }
        total = _parse_flow_value(cells[num_cols - 1])
        # Confirm the last column really is the daily Total (sum of the issuer
        # columns); a mismatch means the assumed column layout has changed, so
        # degrade rather than reporting a wrong "net flow" as authoritative.
        issuer_sum = sum(issuers.values())
        if abs(total - issuer_sum) > max(_TOTAL_ABS_TOL, _TOTAL_REL_TOL * abs(issuer_sum)):
            raise FarsideError(
                f"{asset} row {cells[0]!r}: last column {total:+.1f} is not the daily "
                f"Total (issuer columns sum to {issuer_sum:+.1f}; Farside may have "
                f"added a trailing column)"
            )
        records.append({"date": day, "issuers": issuers, "total": total})

    records.sort(key=lambda rec: rec["date"])
    # One record per date is assumed downstream: `visible[-1]` is "the latest
    # day", `cumulative` is a plain sum, and the streak counts sessions. A table
    # carrying two rows for one date (a revision row, or any HTML anomaly matching
    # _DATE_RE twice) would silently double-count that day, so fail loud like
    # every other structural mismatch rather than serve inflated figures.
    seen_dates = set()
    for rec in records:
        if rec["date"] in seen_dates:
            raise FarsideError(
                f"{asset} table has multiple rows for {rec['date']} (table structure may "
                f"have changed); refusing to double-count the day"
            )
        seen_dates.add(rec["date"])
    return _ParsedTable(records, all_named)


def _utc_now() -> datetime:
    """The single UTC clock source (tests patch this one function)."""
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _iso_now() -> str:
    """Current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` for the cache fetched_at.

    Carries the time-of-day (unlike the old date-only stamp) so ``_cache_age_hours``
    can enforce the hourly TTL instead of pinning a whole UTC calendar day.
    """
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_age_hours(fetched_at: str) -> float | None:
    """Hours since ``fetched_at``, or None if the stamp cannot be parsed.

    Accepts both the full ``...T..:..:..Z`` stamp and a legacy date-only stamp
    (a cache written by an older build), so a version bump never silently treats
    an old file as fresh. None (unparseable) is left to the caller to treat as
    "not fresh", forcing a re-fetch.
    """
    if not fetched_at:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            fetched = datetime.strptime(fetched_at, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (_utc_now() - fetched).total_seconds() / 3600.0
    return None


def _days_stale(fetched_at: str) -> int | None:
    """Calendar days between a snapshot's fetched_at and the current UTC day.

    Returns None when fetched_at is missing or unparseable, so an unknown age is
    handled explicitly rather than assumed fresh or ancient.
    """
    if not fetched_at:
        return None
    try:
        fetched_day = datetime.strptime(fetched_at[:10], "%Y-%m-%d")
    except ValueError:
        return None
    today = datetime.strptime(_utc_today(), "%Y-%m-%d")
    return (today - fetched_day).days


def _cache_dir() -> str:
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _cache_path(asset: str) -> str:
    """Path of the single rolling snapshot for asset.

    One file per asset rather than one per UTC day: only the newest snapshot is
    ever eligible to be served (anything past MAX_STALE_DAYS is refused), so a
    file-per-day scheme only accumulates files that are unreachable by
    construction. Freshness is read from the payload's ``fetched_at``.

    ``asset`` is validated to BTC/ETH, so the filename is never caller-controlled.
    """
    return os.path.join(_cache_dir(), f"farside_{asset.lower()}.json")


def _read_cache(path: str) -> dict | None:
    """Return a fully-validated cached payload, or None if it cannot be trusted.

    Every rejection is logged: a silently-disabled cache (say a future rename of
    a payload key) should be diagnosable rather than showing up as an unexplained
    permanent miss. A rejected cache is simply re-fetched, so refusing to serve a
    questionable file costs one request and never bad data.
    """
    def _reject(reason: str) -> None:
        # One uniform format, but each rejection keeps its own distinct reason:
        # a permanently-missing cache should say *why* it is being skipped.
        logger.warning("Ignoring Farside cache %s: %s", path, reason)
        return None

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # A corrupt/unreadable cache is treated as a miss (never serve it), but
        # logged so a disk/writer fault is distinguishable from "no cache yet".
        return _reject(f"unreadable ({e})")
    if not isinstance(payload, dict):
        return _reject(f"top-level JSON is a {type(payload).__name__}, expected an object")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return _reject("'rows' is missing, not a list, or empty")
    # Each row must be a full record. A non-empty list of non-dicts (a poisoned
    # file like ``"rows": [1, 2, 3]``) or a row missing a field would pass the
    # list check above but crash later on ``r["date"]`` / ``r["total"]`` /
    # ``latest["issuers"]``. Treat as a miss so a poisoned file is never served.
    # bool is excluded explicitly because it is an int subclass.
    if not all(
        isinstance(r, dict)
        and isinstance(r.get("date"), str)
        and isinstance(r.get("total"), (int, float))
        and not isinstance(r.get("total"), bool)
        and isinstance(r.get("issuers"), dict)
        for r in rows
    ):
        return _reject("'rows' contains a malformed record")
    # fetched_at drives both the within-TTL hit and the staleness cap, and
    # issuers_named drives a report caveat, so neither may be absent or the wrong
    # type — a missing fetched_at would otherwise read as "age unknown" forever.
    if not isinstance(payload.get("fetched_at"), str) or not payload["fetched_at"]:
        return _reject("'fetched_at' is missing or not a non-empty string")
    if not isinstance(payload.get("issuers_named"), bool):
        return _reject("'issuers_named' is missing or not a boolean")
    return payload


def _load_flows(asset: str) -> _FlowSnapshot:
    """Return the flow snapshot for asset.

    A cache younger than ``CACHE_TTL_HOURS`` is served as-is (throttling repeat
    calls). Otherwise fetch + parse + overwrite the rolling cache file. On a fetch
    or parse failure, fall back to the cached snapshot (``stale=True``); if there
    is none, or it is beyond the staleness cap, raise FarsideError so the router
    degrades. A failed fetch is never written to cache.
    """
    path = _cache_path(asset)
    cached = _read_cache(path)
    if cached:
        age_h = _cache_age_hours(cached["fetched_at"])
        # 0 <= age guards against a future-dated stamp (clock skew / a tampered
        # file) being treated as perpetually fresh; such a snapshot is re-fetched.
        if age_h is not None and 0 <= age_h < CACHE_TTL_HOURS:
            return _FlowSnapshot(
                records=cached["rows"],
                fetched_at=cached["fetched_at"],
                stale=False,
                issuers_named=cached["issuers_named"],
            )

    try:
        parsed = _parse_flow_table(_request_html(asset), asset)
    except (requests.RequestException, FarsideError) as e:
        if cached:
            fetched_at = cached["fetched_at"]
            age = _days_stale(fetched_at)
            # Refuse to serve a snapshot older than the cap: weeks-old flows
            # presented as the "latest day" are misleading, so degrade to the
            # router sentinel instead. An unknown age (unparseable fetched_at) is
            # treated as beyond the cap — the case where an unbounded-age serve is
            # most likely — rather than served with an "age unknown" caveat.
            if age is None or age > MAX_STALE_DAYS:
                stale_desc = (
                    "has an unparseable fetch date" if age is None else f"is {age} days stale"
                )
                raise FarsideError(
                    f"Farside {asset} fetch failed and the newest cache {stale_desc} "
                    f"(> {MAX_STALE_DAYS}-day cap): {e}"
                ) from e
            # A structural FarsideError means the scraper itself is broken (a real
            # code fix needed), not a transient outage — log it at ERROR with a
            # traceback so it is escalated immediately instead of hiding among
            # network-blip warnings for up to the stale cap. A RequestException is
            # an ordinary outage and stays at warning. (age is non-None here: the
            # None/over-cap cases already raised above.)
            if isinstance(e, FarsideError):
                logger.error(
                    "Farside %s parse failed structurally (%s); serving stale cache "
                    "(%s days old) — the scraper likely needs a fix",
                    asset,
                    e,
                    age,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "Farside %s fetch failed (%s); using stale cache (%s days old)",
                    asset,
                    e,
                    age,
                )
            return _FlowSnapshot(
                records=cached["rows"],
                fetched_at=fetched_at,
                stale=True,
                issuers_named=cached["issuers_named"],
            )
        raise FarsideError(f"Farside {asset} unavailable and no cache exists: {e}") from e

    # Stamp the fetch instant from _iso_now() (the same clock _cache_age_hours and
    # _days_stale read) so the TTL, cache freshness, and the staleness cap all key
    # off one source.
    fetched_at = _iso_now()
    payload = {
        "asset": asset,
        "fetched_at": fetched_at,
        "issuers_named": parsed.issuers_named,
        "rows": parsed.records,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        # Say what the failure costs, not just that it happened: with no file on
        # disk the within-TTL hit above can never fire, so every call re-fetches
        # farside.co.uk until a write succeeds.
        logger.warning(
            "Could not write Farside cache %s: %s — the fetch throttle stays disabled "
            "until a write succeeds, so further calls will each re-fetch",
            path,
            e,
        )
    return _FlowSnapshot(
        records=parsed.records,
        fetched_at=fetched_at,
        stale=False,
        issuers_named=parsed.issuers_named,
    )


def _normalize_asset(asset: str) -> str | None:
    """Map a caller symbol to a Farside asset key (BTC/ETH), or None if unsupported.

    Delegates to the shared ``normalize_symbol`` so quote-suffix stripping
    (USD/USDT/USDC) and crypto-base recognition match the rest of the system:
    ``BTC``, ``ETH-USD``, ``ETHUSD``, ``ETHUSDT``, ``BTC/USD`` all resolve to
    their base, while look-alikes (``ETHW``, ``WETH``, ``BTCB``) and non-BTC/ETH
    coins return None so the caller serves the BTC market-wide proxy. Slash pair
    forms are converted to the dash form first (``normalize_symbol`` only strips
    dashes).
    """
    base = normalize_symbol((asset or "").replace("/", "-")).split("-")[0]
    return base if base in ASSET_PATHS else None


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _plural_days(count: int) -> str:
    return "day" if count == 1 else "days"


def get_etf_flow_data(
    asset: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch US spot-ETF daily net flows for a crypto asset as a markdown report.

    Args:
        asset: "BTC" or "ETH" (also accepts pair forms like "BTC-USD"). A crypto
            asset with no spot ETF of its own (e.g. SOL) has no asset-specific
            flow signal, so BTC flows are served instead as a market-wide proxy.
        curr_date: End of the window (yyyy-mm-dd); rows dated after it are
            dropped so a past date never leaks future flows.
        look_back_days: Trailing window length; ``None`` uses DEFAULT_LOOKBACK_DAYS.
            Bounds the cumulative figure and the rendered table. The inflow/outflow
            streak is reported over all available history (labelled as such), not
            just the window.

    Returns:
        A markdown report: the latest day's net flow, the window's cumulative net
        flow, the consecutive inflow/outflow streak, the latest day's issuer
        breakdown, and a recent daily-flow table (US$m). Zero/blank-total days
        neither break the streak nor count as flow sessions.
    """
    # A None or nonsensical negative window (e.g. a hallucinated tool argument)
    # falls back to the default rather than producing a self-contradictory report.
    if look_back_days is None or look_back_days < 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Normalise curr_date BEFORE any lexical date comparison below. strptime
    # accepts non-zero-padded input ("2026-6-5"), which then compares wrong
    # against the canonical ISO record dates — '2026-12-31' <= '2026-6-5' is True
    # — silently admitting future rows and defeating the lookahead guard. Parse
    # (rejecting genuine garbage) and re-derive the canonical form to compare on.
    # (fear_greed.get_fear_greed_data carries the same guard.)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_dt.strftime("%Y-%m-%d")

    asset_key = _normalize_asset(asset)
    # A crypto asset with no spot ETF of its own has no asset-specific signal;
    # BTC spot-ETF flows are the market's risk-on/off proxy, so serve those
    # (flagged as market-wide, not asset-specific) rather than nothing.
    market_proxy = asset_key is None
    if market_proxy:
        asset_key = "BTC"

    snapshot = _load_flows(asset_key)
    visible = [r for r in snapshot.records if r["date"] <= curr_date]
    latest = visible[-1] if visible else None

    if market_proxy:
        # The requested asset belongs in the heading, not only in the caveat
        # below. This report is re-summarised by the downstream research / trader
        # / risk agents, and a heading byte-identical to a real BTC report is
        # exactly what survives that hop with the proxy framing stripped off.
        header_lines = [
            f"## Spot ETF Flows — {asset_key} (market-wide proxy for '{asset}', Farside, "
            f"net US$m)",
            f"_No spot ETF exists for '{asset}'; showing {asset_key} spot-ETF flows as a "
            f"market-wide crypto risk-on/off proxy, not an '{asset}'-specific signal._",
        ]
    else:
        header_lines = [f"## Spot ETF Flows — {asset_key} (Farside, net US$m)"]
    if snapshot.stale:
        # age is non-None here: _load_flows raises rather than returning
        # stale=True when the age is unknown or beyond the cap.
        age = _days_stale(snapshot.fetched_at)
        # "refresh failed", not "fetch failed": stale=True is reached on BOTH a
        # network error (fetch really failed) and a structural parse break (the
        # fetch SUCCEEDED but the page could not be parsed). Asserting the fetch
        # failed would be false in the parse case — the mirror of the data-lag
        # honesty fix below.
        header_lines.append(
            f"_STALE by {age} {_plural_days(age)}: live refresh failed (network error or a "
            f"parser break); showing the last cached snapshot (fetched {snapshot.fetched_at}). "
            f"Treat with caution._"
        )
    if not snapshot.issuers_named:
        header_lines.append(
            "_Issuer names incomplete: one or more issuer columns could not be labelled from "
            "Farside's header row, so they appear below as 'unnamed col N' placeholders rather "
            "than ticker symbols. The flow figures are still cross-checked; those particular "
            "labels carry no meaning._"
        )
    if latest is not None:
        # Data recency is a separate question from fetch success: farside.co.uk
        # can serve a perfectly parseable page that simply has not been updated
        # in days. The stale-cache machinery above never sees that case, so
        # without this the report would present week-old flows as current.
        lag_days = (curr_dt - datetime.strptime(latest["date"], "%Y-%m-%d")).days
        if lag_days > MAX_DATA_LAG_DAYS:
            # Why the data is behind depends on whether *this* call reached the
            # site. A stale serve already said "live refresh failed" above, so
            # asserting "the fetch succeeded" here would make the report
            # contradict itself — and MAX_STALE_DAYS > MAX_DATA_LAG_DAYS means
            # both caveats co-occur for any ordinary multi-day outage.
            cause = (
                "the cached snapshot above is itself that old"
                if snapshot.stale
                else "the fetch succeeded — farside.co.uk itself has posted nothing since"
            )
            header_lines.append(
                f"_Data lag: the newest published row is {latest['date']}, {lag_days} "
                f"{_plural_days(lag_days)} before {curr_date} ({cause}), so treat the "
                f"figures below as {lag_days} {_plural_days(lag_days)} old._"
            )
    header_lines.append(
        f"- Source: farside.co.uk{ASSET_PATHS[asset_key]} | Window ending {curr_date}"
    )
    # Blank line between entries: consecutive italic caveats would otherwise be
    # joined into one rendered paragraph, running distinct warnings together.
    header = "\n\n".join(header_lines) + "\n"

    if latest is None:
        return (
            header + f"\nNo ETF flow rows on or before {curr_date}. "
            "Report this as no ETF-flow data for the date; do not fabricate values."
        )

    window_start = (curr_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    window = [r for r in visible if r["date"] >= window_start]
    cumulative = sum(r["total"] for r in window)
    # A zero/blank-total day (a genuine $0 net day or a not-yet-posted row) is not
    # a flow session.
    flow_sessions = sum(1 for r in window if r["total"] != 0)

    # Consecutive inflow/outflow streak over all available history, ending at the
    # most recent non-zero-flow day. Zero/blank-total days are transparent: they
    # neither break the streak nor count toward it.
    flow_days = [r for r in visible if r["total"] != 0]
    streak_sign = _sign(flow_days[-1]["total"]) if flow_days else 0
    streak = 0
    for r in reversed(flow_days):
        if _sign(r["total"]) == streak_sign:
            streak += 1
        else:
            break
    # Say "session", not "day". The streak is counted over flow_days (zero/blank
    # rows removed), so a 7-session run can span 11 calendar days across a holiday
    # week — and an LLM weighing "persistence of demand" reads "7-day" literally.
    # Printing the span makes the gap visible instead of implicit.
    if streak_sign == 0:
        streak_line = "**Streak:** no reported flow sessions on record\n"
    else:
        streak_word = "inflow" if streak_sign > 0 else "outflow"
        streak_line = (
            f"**Streak:** {streak}-session {streak_word} "
            f"({flow_days[-streak]['date']} → {flow_days[-1]['date']}) — consecutive "
            f"sessions with reported flow over all available history, not calendar days\n"
        )

    breakdown = sorted(
        ((name, flow) for name, flow in latest["issuers"].items() if flow),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )[:TOP_ISSUERS]
    breakdown_str = (
        ", ".join(f"{name} {flow:+.1f}" for name, flow in breakdown) or "no per-issuer flow"
    )

    if window:
        cumulative_line = (
            f"**{look_back_days}d cumulative net flow:** {cumulative:+.1f} "
            f"({flow_sessions} flow sessions in the window)\n"
        )
    else:
        # The freshest available row predates the window (e.g. a small
        # look_back_days over a weekend/holiday publishing gap). A window sum of
        # +0.0 next to a real "Latest" line reads as "flat"; state the gap
        # instead so the report is not self-contradictory.
        cumulative_line = (
            f"**{look_back_days}d cumulative net flow:** no flow rows within the "
            f"{look_back_days}-day window (latest available is {latest['date']})\n"
        )

    # A row Farside has created but not yet populated parses to an all-blank,
    # all-zero record. That is not a "$0 net flow" day: rendering it as "+0.0 net"
    # directly above a multi-session inflow streak reads as "flows just stopped",
    # and which of the two versions the analyst sees would depend only on what
    # time of day the cycle happened to fire.
    latest_unposted = latest["total"] == 0 and not any(latest["issuers"].values())
    if latest_unposted:
        # Blank and genuine-zero cells are indistinguishable in Farside's HTML, so
        # state the ambiguity instead of picking one. What matters is not printing
        # a confident "+0.0 net" that reads as "demand stopped".
        latest_line = (
            f"\n**Latest ({latest['date']}):** no flow reported — every issuer cell for "
            f"this date is blank or zero, which Farside shows both for a row it has not "
            f"populated yet and for a genuine zero-flow day\n"
        )
    else:
        latest_line = f"\n**Latest ({latest['date']}):** {latest['total']:+.1f} net\n"

    summary = (
        latest_line + cumulative_line + streak_line + f"**Latest-day leaders:** {breakdown_str}\n"
    )

    # If the freshest row predates the window, show the latest available row(s)
    # with a caveat (mirroring fear_greed) so the table matches the Latest line
    # above rather than rendering empty.
    shown = window or visible[-1:]
    note = ""
    if len(window) > MAX_ROWS:
        shown = window[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(window)} days in the window)_\n"
    elif not window:
        # The cumulative line above already states that the window is empty and
        # names the latest available date; repeating it here just said the same
        # sentence twice. Keep only what the table itself needs to explain.
        note = "\n_(table shows the latest available row)_\n"
    # Keep the table's Net Flow cell consistent with the Latest line above: an
    # unpopulated latest row must not reappear as a confident "+0.0" in the densest
    # part of the report (the row downstream agents most often re-quote).
    def _net_cell(r: dict) -> str:
        if latest_unposted and r["date"] == latest["date"]:
            return "not yet posted"
        return f"{r['total']:+.1f}"

    table = (
        "\n| Date | Net Flow |\n| --- | --- |\n"
        + "\n".join(f"| {r['date']} | {_net_cell(r)} |" for r in shown)
        + "\n"
    )

    return header + summary + note + table
