"""SoSoValue spot-ETF flow vendor.

Fetches BTC/ETH US spot-ETF daily net flows from SoSoValue's official OpenAPI
(https://sosovalue.gitbook.io/soso-value-api-doc). Primary vendor for the
``crypto_etf_flows`` category since farside.co.uk went behind a Cloudflare JS
challenge; the report mirrors the Farside one (same core sections, same US$m
units, same-shaped shared caveats whose cause clauses name each vendor's own
failure modes), with SoSoValue-only additions layered on top — a since-launch
cumulative, a fund-breadth line, and revision, out-of-window restatement, and
aggregate-vs-fund-sum reconciliation caveats — so the analyst sees the same
shape whichever vendor served the call.

A free Demo API key (sosovalue.com developer dashboard) is read from
``SOSOVALUE_API_KEY``; if it is unset the vendor raises
``SoSoValueNotConfiguredError`` so the routing layer degrades to the next
vendor in the chain. The key check runs before the cache is consulted, so
removing the key on a deployed box stops this vendor on the very next call —
that is the documented emergency-disable mechanism, and a cached snapshot must
not keep it alive past the flip. The key is whitespace-stripped and validated
as header-safe before use — ``get_api_key`` explains why that matters.

Requests are deliberately date-parameter-free: the Demo plan serves only the
most recent ~30 days and rejects an out-of-range ``start_date`` with an HTTP
403 (live-verified), so instead of guessing the server's window we fetch
everything it will serve and filter to ``date <= curr_date`` client-side, like
the Farside vendor does with its live table.

One refresh is 2 + N requests (aggregate summary, fund list, then one history
per fund — N is 13 for BTC today) against a 20 req/min plan limit. A single
asset refresh fits; two assets refreshing in the same minute can trip the
limit for the trailing fund histories, which is why per-fund failures are
non-fatal: the aggregate summary alone decides vendor success, and a partial
(or absent) issuer breakdown is disclosed in the report rather than failing
the call. Only an aggregate failure (or a rejected key on any request) raises,
letting the router fall through to the next vendor. A hanging network gets the
one exception to trying every fund: after ``MAX_CONSECUTIVE_NETWORK_FAILURES``
back-to-back transport failures the remaining histories are skipped straight
into the same disclosed-incomplete path — the outcome is identical, and the
call stops burning a full ``sosovalue_common.REQUEST_TIMEOUT`` per dead fund. A
snapshot whose breakdown
came back incomplete is cached under the shorter ``INCOMPLETE_CACHE_TTL_HOURS``
so the missing funds are re-tried on the next call past that window instead of
persisting for the full refresh interval.

Fetched flows go into one rolling cache file per asset, refreshed once the
snapshot is older than ``CACHE_TTL_HOURS``: a repeat call within that window
reuses it, and a fetch failure falls back to that same snapshot (marked STALE
in the report) up to ``MAX_STALE_DAYS``. A failed fetch is never written to
cache, and a cache file that fails read-side validation — per-field shape or
the cross-field invariants ``_fetch_all`` always writes — is discarded and
re-fetched rather than served.

Daily figures are provisional while US issuers file their reports (evening US
time): the aggregate either omits a pending day or publishes it as a
zero-total placeholder (both live-verified), and a fund history carries the
pending day with a ``null`` net_inflow — why neither an aggregate zero nor a
``null`` may be read as a confident $0. On each refresh the new summary is diffed
against the cached one, and every changed day still visible on or before
curr_date — whether or not the report's look-back window shows the row —
is disclosed as a revision; the common case is yesterday's
provisional figure firming up after a newer day has already become the
latest, so the analyst can tell reporting catch-up from a new flow event.
The since-launch cumulative gets the matching guard: when the earliest
overlapping day's cumulative moved beyond what that day's own flow change
explains, the API restated (or backfilled) history older than the visible
window, and the report discloses the shift instead of letting the
since-launch figure drift silently between calls.

Backtest reach: each call serves the API's current ~30-day window (reflecting
the present, not curr_date) and then filters to rows on or before curr_date.
A historical curr_date returns real rows only as deep as that window still
reaches — the report says so instead of pretending the history is complete.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote

import requests

# SoSoValueNotConfiguredError and get_api_key are re-exported (redundant-alias
# form): this module is the family's public face, and its tests and callers
# address the key check and the config-breakage type through it even though the
# shared load skeleton is what raises them now.
from .sosovalue_common import (
    SoSoValueError,
    SoSoValueNotConfiguredError as SoSoValueNotConfiguredError,
    SoSoValueRateLimitError,
    _cache_dir,
    _is_finite_number,
    _is_iso_date,
    _is_valid_ticker,
    _plural,
    _plural_days,
    _request,
    _sanitize,
    _sign,
    _stale_caveat,
    get_api_key as get_api_key,
    load_rolling_snapshot,
)
from .symbol_utils import classify_crypto_asset

logger = logging.getLogger(__name__)

# Assets with their own US spot ETF on SoSoValue that we serve. The API itself
# covers more symbols (DOGE ETFs exist and return 200), but the analyst-facing
# support surface deliberately matches the Farside vendor: BTC/ETH native,
# recognized risk assets proxied to BTC, everything else a no-signal note.
SUPPORTED_ASSETS = {"BTC", "ETH"}

# Only US-listed spot ETFs; the API also serves HK, out of scope here.
COUNTRY_CODE = "US"

# Consecutive transport-level failures (requests.RequestException: timeouts,
# connection errors) in the fund-history loop before the remaining histories
# are skipped into ``funds_failed`` unattempted. Without it, a network that
# hangs instead of failing fast turns one BTC refresh into up to 13 sequential
# 30-second timeouts (~7 minutes) inside a single analyst tool call — and the
# post-breaker outcome (disclosed-incomplete breakdown on the short TTL) is
# identical to riding the brownout out. API-level failures (429s, structural
# breaks) neither trip nor survive the counter: the server answered, so the
# next history is still worth its own try.
MAX_CONSECUTIVE_NETWORK_FAILURES = 3

# The documented per-request row cap. The Demo plan's ~30-day window holds at
# most ~23 trading days, so this returns the entire servable window regardless
# of what the server-side default limit happens to be.
API_MAX_LIMIT = 300

# What the Demo plan serves, for report wording only (the fetch does not send
# dates, so a plan-side change degrades to wording drift, not a breakage).
PLAN_WINDOW_DAYS = 30

# Default trailing window when the caller does not specify one; mirrors the
# Farside vendor so a vendor switch does not change the report's window.
DEFAULT_LOOKBACK_DAYS = 30

# Row cap for the rendered recent-flows table, mirroring farside.MAX_ROWS.
MAX_ROWS = 40

# Fund columns to show in the latest-day breakdown (largest |flow| first),
# mirroring farside.TOP_ISSUERS.
TOP_ISSUERS = 5

# Maximum age (calendar days since the last successful fetch) a stale cached
# snapshot may be served for; beyond it a persistent failure degrades to the
# router chain instead. Mirrors the Farside vendor.
MAX_STALE_DAYS = 14

# How long a successfully-fetched snapshot is reused before a fresh fetch.
# Same rationale as the Farside vendor: US issuers file during the US evening
# (~01:00-04:00 UTC), so an hour-keyed TTL re-pulls within a cycle or two of
# the overnight print without hammering the 100k req/month quota.
CACHE_TTL_HOURS = 6

# TTL for a snapshot whose issuer breakdown came back incomplete (failed fund
# histories, a failed fund list, or a listing that came back with no funds at
# all — usually a transient blip or a mid-burst 429). Retrying sooner heals
# the leaders/breadth lines on the next call past
# this window instead of serving the gap for the full interval, while still
# throttling rapid repeated calls (e.g. a backtest loop) if a fund endpoint is
# genuinely broken. Unusable listing entries do not shorten the TTL — a
# re-fetch cannot heal what the listing itself mislabels.
INCOMPLETE_CACHE_TTL_HOURS = 1

# Maximum lag (days between the newest fetched row and curr_date) before the
# report flags the data as behind. SoSoValue omits a day until issuers report,
# so a weekend plus a holiday legitimately reaches 4 days; beyond that the
# feed itself has stalled. Mirrors the Farside vendor.
MAX_DATA_LAG_DAYS = 4

# Aggregate-vs-breakdown reconciliation, checked only on a day where every
# listed fund filed: the two figures come from separate endpoints, so a
# material gap means the /etfs listing itself is missing a fund (e.g. a newly
# launched ETF already counted in the aggregate) — the one universe gap
# funds_failed/funds_unusable cannot disclose. Tolerances mirror the Farside
# layout check; the relative slice keys off the breakdown's own gross so a
# wrong aggregate cannot widen its own tolerance.
RECONCILE_ABS_TOL_USD_M = 2.0
RECONCILE_REL_TOL = 0.02

# The API reports flows in USD; the report renders US$m to stay unit-identical
# with the Farside vendor.
_USD_PER_MILLION = 1e6

# Bound on the listing's free-text fund name, the one server-controlled string
# stored verbatim (for diagnosability — it is never rendered or interpolated).
# Unbounded, an oversized value would ride into the per-asset cache file and
# bloat every subsequent load until the next overwrite. Enforced at the parse
# boundary and re-checked read-side, mirroring the ticker filter's two sites.
MAX_FUND_NAME_CHARS = 120


def _valid_summary_row(row: object) -> bool:
    """One aggregate row's shape: canonical date + finite flow figures.

    The single predicate shared by the strict live parser (raises) and the
    lenient cache validator (rejects + logs), so the two trust boundaries
    cannot silently drift apart when the row shape evolves.
    """
    return (
        isinstance(row, dict)
        and _is_iso_date(row.get("date"))
        and _is_finite_number(row.get("total_net_inflow"))
        and _is_finite_number(row.get("cum_net_inflow"))
    )


def _valid_fund_row(row: object) -> bool:
    """One fund-history row's shape: canonical date + finite-or-null flow.

    Shared by the live parser and the cache validator like
    ``_valid_summary_row``. ``None`` is a legal flow (the pending day).
    """
    return (
        isinstance(row, dict)
        and _is_iso_date(row.get("date"))
        and (row.get("net_inflow") is None or _is_finite_number(row.get("net_inflow")))
    )


def _parse_summary_rows(data: list, asset: str) -> list[dict]:
    """Validate and normalize aggregate summary-history rows.

    Keeps only the fields the report uses (``date``, ``total_net_inflow``,
    ``cum_net_inflow``), sorted ascending by date. Raises ``SoSoValueError``
    on anything that would make the figures untrustworthy — an empty window,
    a malformed row, or two rows sharing a date (which would double-count the
    day downstream) — so the router degrades instead of receiving half-parsed
    aggregates. The aggregate is the vendor-success criterion, so this parser
    is strict where the per-fund one below is lenient.
    """
    if not data:
        raise SoSoValueError(f"SoSoValue returned no summary rows for {asset}")
    rows = []
    for raw in data:
        if not _valid_summary_row(raw):
            raise SoSoValueError(
                f"Malformed {asset} summary row {_sanitize(repr(raw), limit=200)} "
                f"(the API contract may have changed)"
            )
        rows.append(
            {
                "date": raw["date"],
                "total_net_inflow": raw["total_net_inflow"],
                "cum_net_inflow": raw["cum_net_inflow"],
            }
        )
    rows.sort(key=lambda r: r["date"])
    seen = set()
    for row in rows:
        if row["date"] in seen:
            raise SoSoValueError(
                f"SoSoValue {asset} summary has multiple rows for {row['date']}; "
                f"refusing to double-count the day"
            )
        seen.add(row["date"])
    return rows


def _parse_etf_list(data: list, asset: str) -> tuple[list[tuple[str, str]], int]:
    """Validate the /etfs listing into ``((ticker, name) pairs, unusable count)``.

    A listing entry whose ticker does not look like a US ETF ticker is
    dropped with a warning rather than interpolated into a request path —
    the listing is server-controlled data, not something to build URLs from
    unchecked. The drops are *counted* and returned so the report can
    disclose them: a filtered entry must shrink the disclosed universe
    visibly, not silently vanish from the denominator. Duplicate tickers
    keep the first entry (not a drop: the fund is still represented) — one
    history request per ticker, and the per-date fund flow map is keyed by
    ticker anyway.
    """
    funds: list[tuple[str, str]] = []
    seen: set[str] = set()
    unusable = 0
    for raw in data:
        ticker = raw.get("ticker") if isinstance(raw, dict) else None
        if not _is_valid_ticker(ticker):
            unusable += 1
            logger.warning(
                "SoSoValue %s fund list entry %.120r has no usable ticker; skipping it",
                asset,
                raw,
            )
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        name = raw.get("name")
        # Bounded here at the trust boundary so an oversized server value can
        # never ride into the cache file — MAX_FUND_NAME_CHARS explains.
        funds.append((ticker, name[:MAX_FUND_NAME_CHARS] if isinstance(name, str) else ""))
    return funds, unusable


def _parse_fund_rows(data: list, ticker: str) -> list[dict]:
    """Validate one fund's history into ``{"date", "net_inflow"}`` rows.

    ``net_inflow`` may legitimately be ``None``: the API carries the pending
    day (issuer has not filed yet) as an explicit null — live-verified — and
    reading that as $0 would fabricate a flow figure. Anything else malformed
    raises so the caller counts this fund as failed (per-fund failures are
    non-fatal by design); ascending sort, last-wins on a duplicated date
    (a fund duplicate cannot double-count the aggregate, which comes from a
    separate endpoint). An empty ``data`` raises like the summary parser's
    empty-window guard: treating it as a valid-but-empty history would let a
    transient 200-with-``[]`` glitch silently wipe the fund's cached rows —
    no ``funds_failed`` entry, no caveat, no log — and permanently disable
    the aggregate-vs-fund-sum reconciliation check, which requires every
    listed fund to have flows. A genuinely just-launched fund with no history
    yet earns the incomplete-breakdown caveat until its first filing, which
    is honest.
    """
    if not data:
        raise SoSoValueError(f"SoSoValue returned no history rows for {ticker}")
    rows = {}
    for raw in data:
        if not _valid_fund_row(raw):
            # Two messages for diagnosability: a bad value on a well-formed
            # row names the date; anything else shows the raw row.
            if isinstance(raw, dict) and _is_iso_date(raw.get("date")):
                # The rejected value is by construction NOT a finite number —
                # it can be any JSON blob — so bound it like the raw row below.
                raise SoSoValueError(
                    f"Non-finite {ticker} net_inflow on {raw['date']}: "
                    f"{_sanitize(repr(raw.get('net_inflow')), limit=200)}"
                )
            raise SoSoValueError(
                f"Malformed {ticker} history row {_sanitize(repr(raw), limit=200)}"
            )
        rows[raw["date"]] = {"date": raw["date"], "net_inflow": raw.get("net_inflow")}
    return [rows[d] for d in sorted(rows)]


class _FlowSnapshot(NamedTuple):
    """What ``_load_snapshot`` resolved for one asset.

    ``funds`` maps ticker -> {"name": str, "rows": [{"date", "net_inflow"}]}.
    ``funds_total`` counts the usable funds the /etfs listing returned,
    ``funds_unusable`` the listing entries dropped for carrying no usable
    ticker, and ``list_fetched`` whether the listing request itself succeeded
    — so ``funds_total == 0`` can honestly distinguish "unknown universe"
    (fetch failed) from "listing empty / all entries unusable".
    ``funds_failed`` holds the tickers whose history fetch failed, and
    ``revisions`` maps a date to its ``[old, new]`` US$m figures from the
    refresh diff. ``cum_restated`` is the refresh diff's out-of-window
    restatement marker: ``None``, or ``{"date", "old", "new"}`` when the
    earliest overlapping day's cumulative moved (US$m) beyond what that
    day's own flow change explains — history older than the visible window
    changed.
    """

    summary_rows: list[dict]
    funds: dict[str, dict]
    funds_total: int
    funds_failed: list[str]
    funds_unusable: int
    list_fetched: bool
    revisions: dict[str, list[float]]
    cum_restated: dict | None
    fetched_at: str
    stale: bool


def _cache_path(asset: str) -> str:
    """Path of the single rolling snapshot for asset.

    One rolling file per asset (a snapshot past MAX_STALE_DAYS can never be
    served, so per-day files would only accumulate unreachable data).
    ``asset`` is validated against SUPPORTED_ASSETS before this is called, so
    the filename is never caller-controlled.
    """
    return os.path.join(_cache_dir(), f"sosovalue_{asset.lower()}.json")


def _valid_summary_rows(rows: object) -> bool:
    return isinstance(rows, list) and bool(rows) and all(_valid_summary_row(r) for r in rows)


def _valid_funds(funds: object) -> bool:
    # Tickers must pass the same plausibility filter as the live listing
    # (_parse_etf_list): the cache is a lower trust tier than the API, not a
    # higher one, and its tickers land verbatim in the report text. The name
    # bound mirrors the writer's truncation for the same reason — a bloated
    # name in a hand-edited (or pre-bound) cache file must cost one refetch,
    # not persist. Rows must be non-empty: _parse_fund_rows raises on an
    # empty history (the fund lands in funds_failed instead), so _fetch_all
    # never writes one, and an empty-rows fund would silently thin the
    # leaders/breadth lines while disabling the reconciliation check's
    # every-fund-filed gate.
    return isinstance(funds, dict) and all(
        _is_valid_ticker(t)
        and isinstance(f, dict)
        and isinstance(f.get("name"), str)
        and len(f["name"]) <= MAX_FUND_NAME_CHARS
        and isinstance(f.get("rows"), list)
        and f["rows"]
        and all(_valid_fund_row(r) for r in f["rows"])
        # One row per date, mirroring _parse_fund_rows' date-keyed dict. This
        # is what _read_cache's order exemption below rests on: _fund_flows_on
        # stops at the FIRST row matching the date with a non-null flow, so the
        # lookup is order-insensitive only while dates are unique. Two rows for
        # one date and a fund's leaders-line figure becomes whichever copy the
        # file happens to list first.
        and len({r["date"] for r in f["rows"]}) == len(f["rows"])
        for t, f in funds.items()
    )


def _valid_revisions(revisions: object) -> bool:
    return isinstance(revisions, dict) and all(
        _is_iso_date(d)
        and isinstance(pair, list)
        and len(pair) == 2
        and all(_is_finite_number(v) for v in pair)
        for d, pair in revisions.items()
    )


def _read_cache(path: str, asset: str) -> dict | None:
    """Return a fully-validated cached payload for asset, or None if untrusted.

    Every rejection is logged with its own reason so a permanently-missing
    cache is diagnosable; a rejected cache costs one re-fetch and never bad
    data. Same boundary philosophy as the Farside vendor: a poisoned field
    must fail here, not as a TypeError deep inside the report render. Beyond
    per-field shape, the cross-field relationships ``_fetch_all`` always
    writes are re-checked too — each field of a violating payload looks
    individually healthy, but the render would be self-contradictory (a
    "complete" breakdown with missing leaders, a revision caveat disagreeing
    with the table, another asset's flows under this asset's heading).
    """

    def _reject(reason: str) -> None:
        logger.warning("Ignoring SoSoValue cache %s: %s", path, reason)
        return None

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return _reject(f"unreadable ({e})")
    if not isinstance(payload, dict):
        return _reject(f"top-level JSON is a {type(payload).__name__}, expected an object")
    if payload.get("asset") != asset:
        # A copied/renamed cache file would otherwise serve another asset's
        # flows under this asset's heading with no caveat anywhere.
        return _reject(f"'asset' is {str(payload.get('asset'))[:40]!r}, expected {asset!r}")
    if not _valid_summary_rows(payload.get("summary_rows")):
        return _reject("'summary_rows' is missing, empty, or contains a malformed record")
    dates = [r["date"] for r in payload["summary_rows"]]
    if any(a >= b for a, b in zip(dates, dates[1:], strict=False)):
        # The render trusts producer-side ordering: visible[-1] is "the
        # latest day", the cumulative is a plain sum, and a duplicated date
        # would double-count exactly what _parse_summary_rows refuses live.
        return _reject("'summary_rows' dates are not strictly ascending")
    if not _valid_funds(payload.get("funds")):
        return _reject("'funds' is missing or contains a malformed fund entry")
    funds_total = payload.get("funds_total")
    if not isinstance(funds_total, int) or isinstance(funds_total, bool) or funds_total < 0:
        return _reject("'funds_total' is missing or not a non-negative integer")
    failed = payload.get("funds_failed")
    if not isinstance(failed, list) or not all(_is_valid_ticker(t) for t in failed):
        return _reject("'funds_failed' is missing or not a list of plausible tickers")
    unusable = payload.get("funds_unusable")
    if not isinstance(unusable, int) or isinstance(unusable, bool) or unusable < 0:
        return _reject("'funds_unusable' is missing or not a non-negative integer")
    if not isinstance(payload.get("list_fetched"), bool):
        return _reject("'list_fetched' is missing or not a boolean")
    # Listing bookkeeping: every listed fund lands in exactly one of
    # funds/funds_failed exactly once, and a failed listing leaves all four
    # breakdown fields at their empty initializers. Violations render "-1/2
    # funds" style disclosures or leaders lines the header says were omitted.
    if payload["list_fetched"]:
        if funds_total != len(payload["funds"]) + len(failed):
            return _reject(
                f"'funds_total' ({funds_total}) does not equal fetched "
                f"({len(payload['funds'])}) plus failed ({len(failed)}) funds"
            )
        failed_set = set(failed)
        if len(failed_set) != len(failed) or failed_set & payload["funds"].keys():
            return _reject("'funds_failed' repeats a ticker or overlaps 'funds'")
    elif payload["funds"] or failed or funds_total or unusable:
        return _reject("breakdown fields are populated although 'list_fetched' is false")
    if not _valid_revisions(payload.get("revisions")):
        return _reject("'revisions' is missing or malformed")
    # A revision pair must agree with the summary it rides on: _diff_revisions
    # only ever writes [old, new] for a date in the new summary, with "new"
    # equal to that row's total at report granularity and "old" actually
    # different. A disagreeing pair would render a revision caveat directly
    # contradicting the table two paragraphs below it.
    totals = {r["date"]: _usd_m(r["total_net_inflow"]) for r in payload["summary_rows"]}
    for date, (old, new) in payload["revisions"].items():
        # totals.get also covers a date absent from summary_rows: "new" is
        # finite by _valid_revisions, so it can never equal the None default.
        if totals.get(date) != new or old == new:
            return _reject(f"revision for {date} does not match the summary rows")
    # Same agreement rule for the out-of-window restatement marker: its date
    # must name a summary row with a matching "new" cumulative, and old must
    # actually differ — or the caveat would contradict the figures it rides
    # on. A revision on the same date is legal: the marker fires on the
    # residual the day's own flow change does not explain, and the residual
    # itself needs the prior snapshot, which a read-side check cannot see.
    if "cum_restated" not in payload:
        return _reject("'cum_restated' is missing")
    cr = payload["cum_restated"]
    if cr is not None:
        # The marker's date must name a summary row (their dates are already
        # canonical, so matching one implies a well-formed date too).
        row = (
            next((r for r in payload["summary_rows"] if r["date"] == cr.get("date")), None)
            if isinstance(cr, dict)
            else None
        )
        if not (
            row is not None
            and _is_finite_number(cr.get("old"))
            and _is_finite_number(cr.get("new"))
            and cr["old"] != cr["new"]
            and _usd_m(row["cum_net_inflow"]) == cr["new"]
        ):
            return _reject("'cum_restated' is malformed or contradicts the summary rows")
    if not isinstance(payload.get("fetched_at"), str) or not payload["fetched_at"]:
        return _reject("'fetched_at' is missing or not a non-empty string")
    # Deliberately unchecked: per-fund row order (the flow lookup is
    # order-insensitive, which holds because _valid_funds enforces one row per
    # date — that uniqueness is the premise this exemption rests on) and the
    # fetched_at format (an unparseable stamp already fails every freshness
    # check downstream, before any render).
    return payload


def _usd_m(value: float) -> float:
    """USD -> US$m, at the 0.1 granularity the report renders.

    ``+ 0.0`` normalizes the negative zero a sub-tick outflow rounds to
    (``round(-0.04, 1)`` is ``-0.0``), which would otherwise render as a
    confusing "-0.0" figure.
    """
    return round(value / _USD_PER_MILLION, 1) + 0.0


def _is_material(value: float) -> bool:
    """A flow visible at the report's own 0.1 US$m render granularity.

    Classification (flow sessions, streaks, leaders, breadth) must judge a
    flow at the same granularity the figures render at — the discipline
    ``_diff_revisions`` already applies — or a sub-$50k flow would be counted
    as a session / streak member / flow leader while the very same report
    renders it as ``+0.0``, contradicting itself line to line. Farside's
    source data arrives at this granularity natively, so this also keeps the
    two vendors' session semantics aligned.
    """
    return _usd_m(value) != 0.0


def _diff_revisions(cached: dict | None, summary_rows: list[dict]) -> dict:
    """Dates whose net flow changed since the previous snapshot -> [old, new] US$m.

    Compared at the report's own 0.1 US$m granularity so only revisions the
    analyst could actually see get flagged — float noise on an unchanged day
    does not. Computed against the immediately-prior snapshot only: the point
    is "this figure moved since you last saw it", not a revision ledger.
    """
    if not cached:
        return {}
    prior = {r["date"]: r["total_net_inflow"] for r in cached["summary_rows"]}
    revisions = {}
    for row in summary_rows:
        old = prior.get(row["date"])
        if old is None:
            continue
        old_m, new_m = _usd_m(old), _usd_m(row["total_net_inflow"])
        if old_m != new_m:
            revisions[row["date"]] = [old_m, new_m]
    return revisions


def _diff_cum_restatement(cached: dict | None, summary_rows: list[dict]) -> dict | None:
    """Out-of-window restatement marker: earliest overlapping day's cum moved
    beyond what its own flow change explains.

    ``_diff_revisions`` covers every visible day's flow, and the mirror-file
    cache drops rows that fall out of the API's ~30-day window — so a
    restatement of an *older* day is invisible to both, yet still moves every
    ``cum_net_inflow`` and with it the report's since-launch figure. The
    earliest overlapping day pins it down: subtracting its own flow change
    (the part a disclosed daily revision already explains) from its
    cumulative change leaves a residual that can only come from days before
    it — outside what either snapshot can display. The residual test matters:
    a date-membership test against ``revisions`` would let an in-window
    revision mask a simultaneous out-of-window restatement in the same
    refresh, permanently (the next refresh baselines it). No marker when the
    residual is immaterial at the report's 0.1 US$m granularity, or when the
    two rendered cums agree anyway (offsetting changes leave the rendered
    since-launch figure unmoved — nothing visible to disclose).
    """
    if not cached:
        return None
    prior = {r["date"]: r for r in cached["summary_rows"]}
    # summary_rows are sorted ascending, so the first match is the earliest.
    first = next((r for r in summary_rows if r["date"] in prior), None)
    if first is None:
        return None
    prev = prior[first["date"]]
    residual = (first["cum_net_inflow"] - prev["cum_net_inflow"]) - (
        first["total_net_inflow"] - prev["total_net_inflow"]
    )
    old_m, new_m = _usd_m(prev["cum_net_inflow"]), _usd_m(first["cum_net_inflow"])
    if _usd_m(residual) == 0.0 or old_m == new_m:
        return None
    return {"date": first["date"], "old": old_m, "new": new_m}


def _fetch_one_fund(asset: str, ticker: str, name: str) -> dict | None:
    """Fetch and parse one fund's history; ``None`` on a non-fatal failure.

    Catches only non-config failures (network, rate limit, contract break):
    a rejected key (``SoSoValueNotConfiguredError``) matches neither caught
    type and propagates, so a mid-batch 401 can never be absorbed as a fund
    failure. A structural break is logged at ERROR with a traceback — the
    breakdown would otherwise stay silently incomplete refresh after refresh
    — while a transient failure stays a warning. A transport-level failure is
    logged here like any other transient, then re-raised so the caller's
    consecutive-failure breaker can count the streak.
    """
    try:
        rows = _parse_fund_rows(
            _request(f"/etfs/{quote(ticker, safe='')}/history", {"limit": API_MAX_LIMIT}),
            ticker,
        )
        return {"name": name, "rows": rows}
    except (requests.RequestException, SoSoValueRateLimitError, SoSoValueError) as e:
        if isinstance(e, SoSoValueError):
            logger.error(
                "SoSoValue %s fund %s history failed structurally "
                "(breakdown disclosed as incomplete) — the client "
                "likely needs a fix: %s",
                asset,
                ticker,
                e,
                exc_info=True,
            )
        else:
            logger.warning(
                "SoSoValue %s fund %s history failed (breakdown will be "
                "disclosed as incomplete): %s",
                asset,
                ticker,
                e,
            )
        if isinstance(e, requests.RequestException):
            raise
        return None


def _fetch_all(asset: str, cached: dict | None) -> dict:
    """One full refresh: aggregate summary, fund list, per-fund histories.

    Returns a cache payload (without ``fetched_at``). Only two things raise
    out of here: an aggregate failure (aggregate success == vendor success,
    by decision) and a rejected key (``SoSoValueNotConfiguredError``) from
    ANY request — config breakage must reach the router even mid-batch, or a
    payload fetched with a since-revoked key would be cached and served as
    fresh, defeating the emergency-disable flip. The fund list and each fund
    history otherwise degrade to a disclosed-incomplete breakdown, because a
    partial breakdown is still a served signal while a missing aggregate is
    not. A structural ``SoSoValueError`` (contract/parse break) in those
    degraded paths is logged at ERROR with a traceback — the breakdown would
    otherwise stay silently incomplete refresh after refresh on a mere
    warning — while transient failures stay warnings. Transport-level history
    failures additionally feed a consecutive-failure breaker
    (``MAX_CONSECUTIVE_NETWORK_FAILURES``) so a hanging network cannot turn
    the fund loop into minutes of sequential timeouts.
    """
    summary_rows = _parse_summary_rows(
        _request(
            "/etfs/summary-history",
            {"symbol": asset, "country_code": COUNTRY_CODE, "limit": API_MAX_LIMIT},
        ),
        asset,
    )

    funds: dict = {}
    funds_total = 0
    funds_unusable = 0
    funds_failed: list[str] = []
    list_fetched = False
    # The try covers only the listing request — the fund loop lives in the
    # else — so this handler's "fund list failed" label can never absorb a
    # failure from the loop, whose sole transport handler is the breaker.
    try:
        listing, funds_unusable = _parse_etf_list(
            _request("/etfs", {"symbol": asset, "country_code": COUNTRY_CODE}), asset
        )
    except (requests.RequestException, SoSoValueRateLimitError, SoSoValueError) as e:
        # Deliberately NOT the broad VendorError: a rejected key
        # (SoSoValueNotConfiguredError) matches none of these, so config
        # breakage structurally cannot be absorbed as a breakdown failure —
        # it propagates to the router, and a payload fetched with a revoked
        # key is never cached as fresh.
        if isinstance(e, SoSoValueError):
            logger.error(
                "SoSoValue %s fund list failed structurally (report will omit "
                "the issuer breakdown; aggregate figures are unaffected) — the "
                "client likely needs a fix: %s",
                asset,
                e,
                exc_info=True,
            )
        else:
            logger.warning(
                "SoSoValue %s fund list failed (report will omit the issuer "
                "breakdown; aggregate figures are unaffected): %s",
                asset,
                e,
            )
    else:
        list_fetched = True
        funds_total = len(listing)
        consecutive_network = 0
        remaining = iter(listing)
        for ticker, name in remaining:
            try:
                fund = _fetch_one_fund(asset, ticker, name)
            except requests.RequestException:
                # Already logged by _fetch_one_fund, which re-raises so this
                # loop — the only layer that can see a failure streak — can
                # count it.
                funds_failed.append(ticker)
                consecutive_network += 1
                if consecutive_network >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                    # A dead network costs a full timeout per fund; stop
                    # burning them and disclose the rest as failed like any
                    # other incomplete breakdown (short TTL retries soon).
                    # The part-consumed loop iterator is exactly the
                    # unvisited tail.
                    skipped = [t for t, _ in remaining]
                    if skipped:
                        funds_failed.extend(skipped)
                        logger.warning(
                            "SoSoValue %s: %d consecutive network failures; "
                            "skipping the remaining %d fund histories "
                            "(disclosed as incomplete, retried on the short TTL)",
                            asset,
                            consecutive_network,
                            len(skipped),
                        )
                    break
                continue
            # Any completed request resets the breaker — including a mid-burst
            # 429 or a structural break (fund is None): the server answered,
            # so each remaining history is still tried and a transient blip
            # loses one fund, not the tail of the list.
            consecutive_network = 0
            if fund is None:
                funds_failed.append(ticker)
            else:
                funds[ticker] = fund

    revisions = _diff_revisions(cached, summary_rows)
    return {
        "asset": asset,
        "summary_rows": summary_rows,
        "funds": funds,
        "funds_total": funds_total,
        "funds_failed": funds_failed,
        "funds_unusable": funds_unusable,
        "list_fetched": list_fetched,
        "revisions": revisions,
        "cum_restated": _diff_cum_restatement(cached, summary_rows),
    }


def _snapshot_from(payload: dict, fetched_at: str, stale: bool) -> _FlowSnapshot:
    return _FlowSnapshot(
        summary_rows=payload["summary_rows"],
        funds=payload["funds"],
        funds_total=payload["funds_total"],
        funds_failed=payload["funds_failed"],
        funds_unusable=payload["funds_unusable"],
        list_fetched=payload["list_fetched"],
        revisions=payload["revisions"],
        cum_restated=payload["cum_restated"],
        fetched_at=fetched_at,
        stale=stale,
    )


def _cache_ttl_hours(cached: dict) -> int:
    """The module's TTL policy for one cached payload.

    An incomplete breakdown (failed fund histories, a failed fund list,
    or a listing that came back empty) re-tries on the short TTL: the
    gap is usually a transient blip/429, and honouring the full window
    would leave the leaders/breadth lines undercounting — or, for an
    empty listing that overwrote a good snapshot, absent — for cycles.
    Unusable listing entries alone do not shorten it — a re-fetch
    cannot heal those, so the empty-listing clause requires
    funds_unusable == 0 (an all-unusable listing is "empty" only
    because every entry was dropped, and stays on the full TTL).
    """
    return (
        INCOMPLETE_CACHE_TTL_HOURS
        if (
            cached["funds_failed"]
            or not cached["list_fetched"]
            or (not cached["funds_total"] and not cached["funds_unusable"])
        )
        else CACHE_TTL_HOURS
    )


def _load_snapshot(asset: str) -> _FlowSnapshot:
    """Return the flow snapshot for asset, via the family cache/stale discipline.

    ``load_rolling_snapshot`` owns the shared skeleton (key first, TTL-fresh
    serve, fetch + overwrite, stale fallback up to MAX_STALE_DAYS, config
    breakage never absorbed); this module supplies the TTL policy
    (``_cache_ttl_hours``) and the payload shape.
    """
    payload, fetched_at, stale = load_rolling_snapshot(
        path=_cache_path(asset),
        read_cache=lambda path: _read_cache(path, asset),
        fetch_all=lambda cached: _fetch_all(asset, cached),
        ttl_hours=_cache_ttl_hours,
        label=asset,
        cache_name="SoSoValue cache",
        max_stale_days=MAX_STALE_DAYS,
        log=logger,
    )
    return _snapshot_from(payload, fetched_at, stale)


def _classify_asset(asset: str) -> tuple[str | None, bool]:
    """Classify a caller symbol for ETF-flow reporting: ``(asset_key, is_proxy)``.

    Mirrors the Farside vendor exactly so switching vendors never changes
    which assets get a native report, a BTC market-wide proxy (recognized
    risk assets without their own spot ETF), or a no-signal note (stablecoins
    and unrecognized symbols).
    """
    return classify_crypto_asset(asset, SUPPORTED_ASSETS)


def _fund_flows_on(funds: dict, date: str) -> dict[str, float]:
    """Reported (non-null) per-fund net flows for one date, in USD.

    A fund whose row for the date is missing or null has simply not filed
    yet; it is excluded rather than counted as $0.
    """
    flows = {}
    for ticker, fund in funds.items():
        for row in fund["rows"]:
            if row["date"] == date and row["net_inflow"] is not None:
                flows[ticker] = row["net_inflow"]
                break
    return flows


def _is_unreported_day(row: dict, fund_flows: dict[str, float]) -> bool:
    """True for an aggregate row with not a single fund filing that day.

    An exactly-zero aggregate with zero fund reports is a placeholder the API
    published before issuers filed (live-verified on thin assets). An
    explicit per-fund ``$0`` counts as a report — fund-level zero and null
    ARE distinguishable, unlike the aggregate — so a day whose only filings
    are flat is a genuine zero-flow day, not an unposted one. With no fund
    data at all (breakdown failed) posted and unposted are indistinguishable,
    and the disclosure wording owns that ambiguity.

    Deliberately EXACT zero, not the ``_is_material`` render granularity the
    session/leaders classification uses: the API's placeholder is a literal
    0, and rounding here would relabel a real (posted) sub-$50k day as "not
    yet posted". Known consequence: a day firming up from placeholder 0 to a
    sub-tick flow flips this predicate (table cell "not yet posted" →
    "+0.0") with no revision caveat, since ``_diff_revisions`` sees no change
    at 0.1 US$m — accepted, as both renders honestly describe a no-material-
    flow day.
    """
    return row["total_net_inflow"] == 0 and not fund_flows


def get_etf_flow_data(
    asset: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch US spot-ETF daily net flows for a crypto asset as a markdown report.

    Args:
        asset: "BTC" or "ETH" (also accepts pair forms like "BTC-USD"). A
            recognized crypto risk asset with no spot ETF of its own (e.g.
            SOL, XRP) is served BTC flows as a market-wide risk-on/off proxy;
            a stablecoin or unrecognized symbol gets a no-signal message.
        curr_date: End of the window (yyyy-mm-dd); rows dated after it are
            dropped so a past date never leaks future flows.
        look_back_days: Trailing window length; ``None`` uses
            DEFAULT_LOOKBACK_DAYS. Bounds the cumulative figure and the
            rendered table; the streak spans every session visible to
            ``curr_date`` within the API's ~30-day window and is labelled
            "≥N" when it reaches that window's edge, because the true streak
            may extend further back.

    Returns:
        A markdown report shaped like the Farside vendor's (Farside-shaped
        core sections, same US$m units): latest day's net flow, window
        cumulative, since-launch cumulative, streak, latest-day fund leaders,
        a breadth line (how many funds moved together and, when any moved,
        how concentrated the flow was), and a recent daily-flow table.
    """
    if look_back_days is None or look_back_days <= 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Normalise curr_date BEFORE any lexical date comparison: strptime accepts
    # non-zero-padded input ("2026-6-5"), which compares wrong against
    # canonical ISO row dates and would silently admit future rows.
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_dt.strftime("%Y-%m-%d")

    asset_key, market_proxy = _classify_asset(asset)
    if asset_key is None:
        # Same no-signal statement as the Farside vendor: a stablecoin has no
        # risk-on/off flow character to proxy, so serving BTC flows for it
        # would be a misleading signal, and a specific message beats the
        # generic degraded-category sentinel.
        return (
            f"There is no spot-ETF flow signal for '{asset}': it has no US spot ETF of "
            f"its own and is not a recognized crypto risk asset for which BTC flows serve "
            f"as a market-wide proxy (e.g. a stablecoin or an unrecognized symbol). Do "
            f"not substitute BTC or ETH flows."
        )

    snapshot = _load_snapshot(asset_key)
    visible = [r for r in snapshot.summary_rows if r["date"] <= curr_date]
    latest = visible[-1] if visible else None
    latest_flows = _fund_flows_on(snapshot.funds, latest["date"]) if latest else {}

    if market_proxy:
        # The proxy marker must live in the heading, not only the caveat: the
        # report is re-summarised downstream and the heading is what survives.
        header_lines = [
            f"## Spot ETF Flows — {asset_key} (market-wide proxy for '{asset}', SoSoValue, "
            f"net US$m)",
            f"_No spot ETF exists for '{asset}'; showing {asset_key} spot-ETF flows as a "
            f"market-wide crypto risk-on/off proxy, not an '{asset}'-specific signal._",
        ]
    else:
        header_lines = [f"## Spot ETF Flows — {asset_key} (SoSoValue, net US$m)"]

    if snapshot.stale:
        header_lines.append(_stale_caveat(snapshot.fetched_at, "Treat with caution."))

    # Both breakdown caveats are guarded on ``latest`` like the window-clamp
    # one: with no visible rows the report below is the no-rows message, and
    # a caveat about "the leaders and breadth lines" or "the aggregate
    # figures" would describe sections that do not exist.
    if latest is not None and (not snapshot.list_fetched or snapshot.funds_total == 0):
        if not snapshot.list_fetched:
            reason = "the fund list could not be fetched"
        else:
            # The listing request SUCCEEDED but yielded nothing usable —
            # saying "could not be fetched" would blame the wrong failure.
            n = snapshot.funds_unusable
            unusable_note = (
                f" ({n} listing {_plural(n, 'entry', 'entries')} had no usable ticker)" if n else ""
            )
            reason = f"the fund list returned no usable funds{unusable_note}"
        header_lines.append(
            f"_Issuer breakdown unavailable: {reason}, so the per-fund leaders and "
            f"breadth lines are omitted. The aggregate figures come from a separate "
            f"endpoint and are unaffected._"
        )
    elif latest is not None and (snapshot.funds_failed or snapshot.funds_unusable):
        # Derived from the same value the body branches on (snapshot.funds);
        # the reader/writer bookkeeping invariant makes it equal to
        # funds_total - len(funds_failed), and sharing the body's source keeps
        # this caveat and the rendered lines mechanically in step.
        fetched = len(snapshot.funds)
        problems = []
        if snapshot.funds_failed:
            problems.append(
                f"histories for {', '.join(sorted(snapshot.funds_failed))} could not be fetched"
            )
        if snapshot.funds_unusable:
            n = snapshot.funds_unusable
            problems.append(
                f"{n} listing {_plural(n, 'entry', 'entries')} with no usable ticker "
                f"{_plural(n, 'was', 'were')} skipped"
            )
        # The consequence clause must stay true whichever body branch renders:
        # with zero fetched funds the body omits the leaders/breadth lines
        # entirely, and claiming they "cover only" anything would describe
        # lines that do not exist.
        if fetched:
            consequence = (
                f"so the per-fund lines below cover only the {fetched} fetched "
                f"{_plural(fetched, 'fund', 'funds')}"
            )
        else:
            consequence = "so the per-fund leaders and breadth lines are omitted"
        header_lines.append(
            f"_Issuer breakdown incomplete ({fetched}/{snapshot.funds_total} funds): "
            f"{'; '.join(problems)}, {consequence}. The aggregate figures come from "
            f"a separate endpoint and are unaffected._"
        )

    # Revisions are disclosed for every changed day still visible in this
    # report (user decision): the common catch-up case is yesterday's
    # provisional figure firming up after a newer day has already become the
    # latest, which a latest-day-only check would silently skip. Filtering on
    # the visible dates keeps a revised not-yet-visible row from leaking
    # through the lookahead guard.
    visible_dates = {r["date"] for r in visible}
    # On a stale serve either refresh-diff disclosure below is as old as the
    # snapshot itself and would otherwise be restated verbatim on every call
    # of an outage (up to the 14-day cap), reading as recurring activity.
    stale_note = (
        " This disclosure dates from when the stale snapshot above was last "
        "refreshed, not from this call."
        if snapshot.stale
        else ""
    )
    revised = sorted((d, pair) for d, pair in snapshot.revisions.items() if d in visible_dates)
    if revised:
        rows_by_date = {r["date"]: r for r in visible}

        def _is_retraction(d: str, new_m: float) -> bool:
            # A day pulled back to the exact-zero placeholder with no fund
            # filing left renders as "not yet posted" in the table below, so
            # calling its revision "reporting catch-up" would contradict the
            # cell two paragraphs down — that entry is a retraction.
            row = rows_by_date.get(d)
            return (
                new_m == 0.0
                and row is not None
                and row["total_net_inflow"] == 0
                and _is_unreported_day(row, _fund_flows_on(snapshot.funds, d))
            )

        retracted = {d for d, (_old, new) in revised if _is_retraction(d, new)}
        changes = "; ".join(
            f"{d}: {old:+.1f} → retracted (now unposted)"
            if d in retracted
            else f"{d}: {old:+.1f} → {new:+.1f}"
            for d, (old, new) in revised
        )
        noun = "day's figure has" if len(revised) == 1 else "days' figures have"
        if len(retracted) == len(revised):
            # No "below": a revision is disclosed for any visible day, but the
            # table renders only the look-back window — the retracted row may
            # not be among the rows shown.
            verdict = (
                "The prior figure was withdrawn — the day now shows as not yet "
                "posted, not a new flow event."
                if len(retracted) == 1
                else "The prior figures were withdrawn — the days now show as "
                "not yet posted, not new flow events."
            )
        else:
            # Any non-retracted entry keeps the decided catch-up verdict; a
            # retracted entry in the mix is already labeled by its own arrow.
            verdict = "This is reporting catch-up on already-published days, not new flow events."
        header_lines.append(
            f"_Revision: {len(revised)} {noun} changed since the previous snapshot "
            f"({changes}, US$m) as issuers file their daily reports. {verdict}{stale_note}_"
        )

    # The revision caveat above covers visible days only, and the mirror-file
    # cache drops rows that age out of the API window — so a restated day
    # older than the window would silently move the Since-launch figure
    # between calls with no disclosure at all (user decision: disclose it).
    # Filtered on visible_dates like the revision caveat, so a backtest
    # curr_date older than the marker's day neither leaks a future date nor
    # flags a shift its own since-launch figure does not include.
    cr = snapshot.cum_restated
    if cr and cr["date"] in visible_dates:
        header_lines.append(
            f"_Restatement: the cumulative total as of {cr['date']} moved "
            f"{cr['old']:+.1f} → {cr['new']:+.1f} US$m since the previous snapshot — a "
            f"shift that day's own flow changes do not account for — so the API "
            f"restated or backfilled history older than the ~{PLAN_WINDOW_DAYS}-day "
            f"window this report can display, and the since-launch cumulative moved "
            f"for reasons the daily rows below cannot show.{stale_note}_"
        )

    window_start_dt = curr_dt - timedelta(days=look_back_days)
    window_start = window_start_dt.strftime("%Y-%m-%d")
    oldest_fetched = snapshot.summary_rows[0]["date"]
    missing_days = (datetime.strptime(oldest_fetched, "%Y-%m-%d") - window_start_dt).days
    if latest is not None and missing_days > MAX_DATA_LAG_DAYS:
        # The available history cannot fill the requested window (a look_back
        # beyond the plan's ~30 days, or a historical curr_date near the
        # window's far edge); say so rather than presenting a truncated
        # cumulative as the full window. The tolerance reuses the data-lag
        # one: a gap of up to a weekend + holiday is a trading-calendar
        # artifact (the oldest *trading* day sits a few calendar days inside
        # any window), not a clamp worth a caveat on every ordinary call.
        # Guarded on ``latest``: with no visible rows at all the report below
        # is the no-rows message, and a caveat about "the cumulative and table
        # below" would describe sections that do not exist.
        header_lines.append(
            f"_Window clamped: the requested {look_back_days}-day window would start "
            f"{window_start}, but the API plan serves only the most recent "
            f"~{PLAN_WINDOW_DAYS} days (oldest available row: {oldest_fetched}). The "
            f"cumulative and table below cover that shorter span._"
        )

    if latest is not None:
        # Data recency is a separate question from fetch success: the feed can
        # be reachable while issuers have not filed for days.
        lag_days = (curr_dt - datetime.strptime(latest["date"], "%Y-%m-%d")).days
        if lag_days > MAX_DATA_LAG_DAYS:
            # Neither clause claims what it cannot know (user decision;
            # farside's mirrored wording is a known follow-up): the stale
            # branch does not equate snapshot age with row lag (a feed stall
            # served hours-stale would contradict the STALE line above), and
            # the fresh branch claims visibility as of curr_date, not the
            # feed's frontier (a backtest date in a mid-window gap has newer
            # rows that the lookahead filter hides).
            cause = (
                "the stale snapshot above may itself be missing newer filings"
                if snapshot.stale
                else f"the fetch succeeded — no newer filing is visible as of {curr_date}"
            )
            header_lines.append(
                f"_Data lag: the newest published row is {latest['date']}, {lag_days} "
                f"{_plural_days(lag_days)} before {curr_date} ({cause}), so treat the "
                f"figures below as {lag_days} {_plural_days(lag_days)} old._"
            )

    reconcile_gap_disclosed = False
    reconcile_all_filed = False
    if (
        latest is not None
        and snapshot.list_fetched
        and snapshot.funds_total
        and not snapshot.funds_failed
        and not snapshot.funds_unusable
        and len(latest_flows) == snapshot.funds_total
    ):
        # Every listed fund filed for the latest day, so the fund sum should
        # match the aggregate within noise; a material gap means the /etfs
        # listing is missing a fund and the leaders/breadth lines
        # under-describe the day — undetectable from funds_failed/unusable,
        # which are both clean here.
        reconcile_all_filed = True
        fund_sum_m = _usd_m(sum(latest_flows.values()))
        total_m = _usd_m(latest["total_net_inflow"])
        gross_m = sum(abs(f) for f in latest_flows.values()) / _USD_PER_MILLION
        if abs(total_m - fund_sum_m) > max(RECONCILE_ABS_TOL_USD_M, RECONCILE_REL_TOL * gross_m):
            reconcile_gap_disclosed = True
            header_lines.append(
                f"_Reconciliation gap: the {snapshot.funds_total} fund filings for "
                f"{latest['date']} sum to {fund_sum_m:+.1f} US$m but the aggregate "
                f"reports {total_m:+.1f} ({total_m - fund_sum_m:+.1f} unaccounted). "
                f"The fund universe may be missing a fund (e.g. a newly listed "
                f"ETF), so the leaders and breadth lines below may not cover the "
                f"whole day's flow._"
            )

    header_lines.append(f"- Source: SoSoValue OpenAPI (US spot ETFs) | Window ending {curr_date}")
    # Blank line between entries so consecutive italic caveats stay separate
    # rendered paragraphs.
    header = "\n\n".join(header_lines) + "\n"

    if latest is None:
        return (
            header + f"\nNo ETF flow rows on or before {curr_date} (the API serves only "
            f"the most recent ~{PLAN_WINDOW_DAYS} days, so older backtest dates have no "
            f"reachable history). Report this as no ETF-flow data for the date; do not "
            f"fabricate values."
        )

    window = [r for r in visible if r["date"] >= window_start]
    latest_unreported = _is_unreported_day(latest, latest_flows)

    cumulative = sum(r["total_net_inflow"] for r in window)
    flow_sessions = sum(1 for r in window if _is_material(r["total_net_inflow"]))

    # Streak over every fetched session visible to curr_date. Days with no
    # material flow are transparent (placeholder vs genuine-zero ambiguity,
    # and sub-tick noise), mirroring the Farside vendor. Raw signs are safe
    # here: a material value rounds to a nonzero tick, which keeps its sign.
    flow_days = [r for r in visible if _is_material(r["total_net_inflow"])]
    streak_sign = _sign(flow_days[-1]["total_net_inflow"]) if flow_days else 0
    streak = 0
    for r in reversed(flow_days):
        if _sign(r["total_net_inflow"]) == streak_sign:
            streak += 1
        else:
            break
    if streak_sign == 0:
        streak_line = "**Streak:** no reported flow sessions in the available window\n"
    else:
        streak_word = "inflow" if streak_sign > 0 else "outflow"
        if streak == len(flow_days):
            # Every visible session is same-signed: the streak reaches the
            # edge of the servable window, so the true run may be longer and
            # an unqualified "N-session" would understate it as a fact.
            streak_line = (
                f"**Streak:** ≥{streak}-session {streak_word} "
                f"({flow_days[0]['date']} → {flow_days[-1]['date']}) — the streak spans "
                f"every session visible to {curr_date} within the API's "
                f"~{PLAN_WINDOW_DAYS}-day window and may extend further back; sessions, "
                f"not calendar days\n"
            )
        else:
            streak_line = (
                f"**Streak:** {streak}-session {streak_word} "
                f"({flow_days[-streak]['date']} → {flow_days[-1]['date']}) — consecutive "
                f"sessions with reported flow within the API's ~{PLAN_WINDOW_DAYS}-day "
                f"window, not calendar days\n"
            )

    # With failed histories in the mix, "no fund" would claim knowledge of
    # filings those histories never delivered. The Latest and leaders lines
    # share this one scope word so the two co-rendered claims cannot drift.
    fund_scope = "no fetched fund" if snapshot.funds_failed else "no fund"
    if latest_unreported:
        latest_line = (
            f"\n**Latest ({latest['date']}):** no flow reported yet — the aggregate for "
            f"this date is zero with {fund_scope} reporting a flow, which this API "
            f"publishes both for a day issuers have not filed yet and for a genuine "
            f"zero-flow day\n"
        )
    else:
        latest_line = (
            f"\n**Latest ({latest['date']}):** {_usd_m(latest['total_net_inflow']):+.1f} net\n"
        )

    if window:
        cumulative_line = (
            f"**{look_back_days}d cumulative net flow:** {_usd_m(cumulative):+.1f} "
            f"({flow_sessions} flow sessions in the window)\n"
        )
    else:
        cumulative_line = (
            f"**{look_back_days}d cumulative net flow:** no flow rows within the "
            f"{look_back_days}-day window (latest available is {latest['date']})\n"
        )
    since_launch_line = (
        f"**Since-launch cumulative:** {_usd_m(latest['cum_net_inflow']):+.1f} "
        f"(all {asset_key} US spot ETFs since inception, as of {latest['date']})\n"
    )

    # Latest-day fund leaders + breadth, from the per-fund histories. Both are
    # keyed strictly to the aggregate's latest visible date so they can never
    # describe a newer day than the headline figure. The breadth denominator
    # counts every fund that filed — an explicit $0 is a report (user
    # decision), or "X of Y reporting" would read as thin coverage on a
    # broadly-flat day. Concentration stays over the movers' gross, where a
    # flat filing contributes nothing.
    reported = len(latest_flows)
    directional = {t: f for t, f in latest_flows.items() if _is_material(f)}
    if directional:
        leaders = sorted(directional.items(), key=lambda kv: abs(kv[1]), reverse=True)
        leaders_str = ", ".join(f"{t} {_usd_m(f):+.1f}" for t, f in leaders[:TOP_ISSUERS])
        gross = sum(abs(f) for f in directional.values())
        inflow_count = sum(1 for f in directional.values() if f > 0)
        top3_share = sum(abs(f) for _, f in leaders[:3]) / gross * 100
        top_ticker, top_flow = leaders[0]
        flat = reported - len(directional)
        flat_note = f" ({flat} flat)" if flat else ""
        breadth_line = (
            f"**Breadth ({latest['date']}):** {inflow_count} of {reported} "
            f"reporting funds saw inflows{flat_note}; top-3 funds = {top3_share:.0f}% of "
            f"gross flow, largest single fund {top_ticker} = "
            f"{abs(top_flow) / gross * 100:.0f}% "
            f"— distinguishes broad moves from single-fund totals\n"
        )
        leaders_line = f"**Latest-day leaders:** {leaders_str}\n"
    elif reported:
        # Every filing for the latest day rounds to $0 at render granularity
        # (an explicit flat $0 or a sub-$50k trickle). Whether that makes the
        # DAY flat is the aggregate's call: the two come from separate
        # endpoints, and mid-filing the aggregate routinely publishes a
        # material figure while most issuers are still unfiled (null) — so a
        # "confirmed flat day" verdict is only honest when the aggregate
        # agrees, and must otherwise own the asynchrony instead of
        # contradicting the Latest line two paragraphs up. Concentration is
        # omitted either way — there is no directional gross to concentrate.
        leaders_line = (
            f"**Latest-day leaders:** none — all {reported} reporting "
            f"{_plural(reported, 'fund', 'funds')} filed flat "
            f"($0 at the 0.1 US$m granularity shown) for {latest['date']}\n"
        )
        if _is_material(latest["total_net_inflow"]):
            if reconcile_gap_disclosed:
                # Every listed fund has filed — that is the reconciliation
                # gate's premise — so "filings still posting" cannot explain
                # the material aggregate; the disclosed listing gap does.
                breadth_line = (
                    f"**Breadth ({latest['date']}):** 0 of {reported} reporting funds "
                    f"saw inflows ({reported} flat) — the aggregate above shows a "
                    f"material net flow that no fund filing reflects; every listed "
                    f"fund has filed, so this is the reconciliation gap flagged "
                    f"above, not filings still posting\n"
                )
            elif reconcile_all_filed:
                # Same premise as the disclosed-gap branch — every listed
                # fund has filed — but the aggregate-vs-sum residual stayed
                # within the reconciliation tolerance: "still posting" is
                # equally disproven, and a sub-tolerance residual is endpoint
                # noise, not a pending filing.
                breadth_line = (
                    f"**Breadth ({latest['date']}):** 0 of {reported} reporting funds "
                    f"saw inflows ({reported} flat) — the aggregate above shows a "
                    f"material net flow that no fund filing reflects; every listed "
                    f"fund has filed and the residual sits within the reconciliation "
                    f"tolerance, so treat it as endpoint noise, not filings still "
                    f"posting\n"
                )
            elif snapshot.funds_failed:
                # With failed histories in the mix, "still posting" is not the
                # only (or likeliest) explanation — the flow may simply sit
                # with the funds the breakdown could not fetch, which the
                # header caveat already discloses.
                breadth_line = (
                    f"**Breadth ({latest['date']}):** 0 of {reported} reporting funds "
                    f"saw inflows ({reported} flat) — the aggregate above shows a "
                    f"material net flow that no fetched fund's filing reflects; it "
                    f"may sit with the unfetched funds flagged above or in filings "
                    f"still posting\n"
                )
            else:
                breadth_line = (
                    f"**Breadth ({latest['date']}):** 0 of {reported} reporting funds "
                    f"saw inflows ({reported} flat) — the aggregate above shows a "
                    f"material net flow that no fund filing reflects, so issuer "
                    f"filings for the day are likely still posting\n"
                )
        else:
            breadth_line = (
                f"**Breadth ({latest['date']}):** 0 of {reported} reporting funds saw "
                f"inflows ({reported} flat) — a confirmed flat day, not an "
                f"unposted one\n"
            )
    elif snapshot.funds:
        leaders_line = (
            f"**Latest-day leaders:** {fund_scope} has filed a flow report for "
            f"{latest['date']} yet\n"
        )
        breadth_line = ""
    else:
        # Breakdown unavailable entirely; the header caveat already says why.
        leaders_line = ""
        breadth_line = ""

    summary = (
        latest_line
        + cumulative_line
        + since_launch_line
        + streak_line
        + leaders_line
        + breadth_line
    )

    shown = window or visible[-1:]
    note = ""
    if len(window) > MAX_ROWS:
        shown = window[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(window)} days in the window)_\n"
    elif not window:
        note = "\n_(table shows the latest available row)_\n"

    def _net_cell(r: dict) -> str:
        # The same placeholder honesty as the Latest line, applied to every
        # table row: an unreported day must not read as a confident "+0.0" in
        # the rows downstream agents most often re-quote. The cheap zero test
        # runs first so the funds scan only happens for the rare all-zero row
        # — it duplicates _is_unreported_day's exact-zero clause, so loosening
        # that predicate's granularity would be silently suppressed here.
        if r["total_net_inflow"] == 0 and _is_unreported_day(
            r, _fund_flows_on(snapshot.funds, r["date"])
        ):
            return "not yet posted"
        return f"{_usd_m(r['total_net_inflow']):+.1f}"

    table = (
        "\n| Date | Net Flow |\n| --- | --- |\n"
        + "\n".join(f"| {r['date']} | {_net_cell(r)} |" for r in shown)
        + "\n"
    )

    return header + summary + note + table
