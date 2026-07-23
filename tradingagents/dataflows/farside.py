"""Farside spot-ETF flow vendor.

Scrapes BTC/ETH US spot-ETF daily net flows from farside.co.uk's HTML tables
(the site has no API). Surfaced to the news analyst as a crypto-only demand /
positioning signal: persistent ETF inflows or outflows are a flows narrative
that complements price, macro, and news.

Keyless. The site sits behind Cloudflare, so requests carry a browser-style
User-Agent; a WAF block (403) or any network error is treated like unavailable
data. Parsed flows are cached per UTC day; a same-day repeat call reuses the
cache, and a fetch failure falls back to the most recent cached snapshot
(marked stale in the report) rather than losing the signal. A failed fetch is
never written to cache, and any structural mismatch raises rather than
returning a half-parsed table — the routing layer degrades the optional
crypto_etf_flows category to a sentinel.

Backtest reach: each call re-fetches the live farside.co.uk table (keyed to the
current UTC day, not curr_date) and then filters to rows on or before curr_date.
So a historical curr_date returns real rows only as deep as the live table
still lists — unlike a date-ranged API. The inflow/outflow streak is likewise
bounded by how much history that live table carries.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from parsel import Selector

from .config import get_config
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
# the recent flow regime and the streak.
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


class FarsideError(RuntimeError):
    """Farside was unreachable, blocked, or its table structure changed.

    A plain exception (caught by the router's generic handler) so the optional
    crypto_etf_flows category degrades to a sentinel instead of aborting the run.
    """


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


def _parse_flow_table(html: str, asset: str) -> list[dict]:
    """Parse the Farside flow table into per-day records (US$m).

    Returns a list of ``{"date": "YYYY-MM-DD", "issuers": {ticker: flow},
    "total": flow}`` sorted ascending by date. Raises FarsideError on any
    structural mismatch — missing table, no issuer header, no data rows, a row
    whose column count disagrees with the header, or an unparseable date/value —
    so the caller degrades rather than returning a half-parsed table.
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
        raise FarsideError(f"No dated flow rows in the {asset} table")

    num_cols = len(rows[data_row_idxs[0]])
    if num_cols < 3:
        raise FarsideError(f"Unexpected column count {num_cols} in the {asset} table")

    header = _find_issuer_header(rows, data_row_idxs[0], num_cols)
    # Columns: 0 = date, last = Total, the rest = issuers.
    issuer_cols = list(range(1, num_cols - 1))
    issuer_names = [(header[j] if header and header[j] else f"ETF{j}") for j in issuer_cols]

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
    return records


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def _cache_path(asset: str, utc_date: str) -> str:
    # asset is validated to BTC/ETH and utc_date is generated internally, so the
    # filename never contains caller-controlled path characters.
    return os.path.join(_cache_dir(), f"farside_{asset.lower()}_{utc_date}.json")


def _read_cache(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # A corrupt/unreadable cache is treated as a miss (never serve it), but
        # logged so a disk/writer fault is distinguishable from "no cache yet".
        logger.warning("Ignoring unreadable Farside cache %s: %s", path, e)
        return None
    # Treat a payload that is not a dict, or whose "rows" is missing / not a list /
    # empty, as a miss so a poisoned file is never served. Logged (like the
    # corruption branch above) so a silently-disabled cache — e.g. a future rename
    # of the "rows" key — is diagnosable instead of an unexplained permanent miss.
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        logger.warning("Ignoring structurally-invalid Farside cache %s", path)
        return None
    # Each row must be a full record. A non-empty list of non-dicts (a poisoned
    # file like ``"rows": [1, 2, 3]``) or a row missing a field would pass the
    # list check above but crash later on ``r["date"]`` / ``r["total"]`` /
    # ``latest["issuers"]`` — e.g. a future record-schema change reading back an
    # old same-UTC-day cache. Treat as a miss so a poisoned file is never served.
    if not all(
        isinstance(r, dict) and "date" in r and "total" in r and isinstance(r.get("issuers"), dict)
        for r in rows
    ):
        logger.warning("Ignoring Farside cache %s with malformed rows", path)
        return None
    return payload


def _newest_cached(asset: str) -> dict | None:
    """Return the most recent non-empty cached snapshot for asset, or None.

    Filenames embed the UTC fetch date, so a reverse lexical sort is newest-first.
    """
    cache_dir = get_config()["data_cache_dir"]
    if not os.path.isdir(cache_dir):
        return None
    prefix = f"farside_{asset.lower()}_"
    for name in sorted(
        (f for f in os.listdir(cache_dir) if f.startswith(prefix) and f.endswith(".json")),
        reverse=True,
    ):
        payload = _read_cache(os.path.join(cache_dir, name))
        if payload:
            return payload
    return None


def _load_flows(asset: str) -> tuple[list[dict], str, bool]:
    """Return ``(records, fetched_at, stale)`` for asset.

    Same-UTC-day cache hit returns immediately (throttles repeat calls). Otherwise
    fetch + parse + cache. On a fetch or parse failure, fall back to the most
    recent cached snapshot (``stale=True``); if there is none, raise FarsideError
    so the router degrades. A failed fetch is never written to cache.
    """
    today_path = _cache_path(asset, _utc_today())
    cached_today = _read_cache(today_path)
    if cached_today:
        return cached_today["rows"], cached_today.get("fetched_at", "cache"), False

    try:
        records = _parse_flow_table(_request_html(asset), asset)
    except (requests.RequestException, FarsideError) as e:
        stale_payload = _newest_cached(asset)
        if stale_payload:
            fetched_at = stale_payload.get("fetched_at", "unknown")
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
            return stale_payload["rows"], fetched_at, True
        raise FarsideError(f"Farside {asset} unavailable and no cache exists: {e}") from e

    # Stamp the fetch date from _utc_today() (the same source _days_stale reads)
    # so cache freshness and the staleness cap key off one clock.
    fetched_at = _utc_today()
    payload = {"asset": asset, "fetched_at": fetched_at, "rows": records}
    try:
        with open(today_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        logger.warning("Could not write Farside cache %s: %s", today_path, e)
    return records, fetched_at, False


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

    asset_key = _normalize_asset(asset)
    # A crypto asset with no spot ETF of its own has no asset-specific signal;
    # BTC spot-ETF flows are the market's risk-on/off proxy, so serve those
    # (flagged as market-wide, not asset-specific) rather than nothing.
    market_proxy = asset_key is None
    if market_proxy:
        asset_key = "BTC"

    records, fetched_at, stale = _load_flows(asset_key)
    visible = [r for r in records if r["date"] <= curr_date]

    header_lines = [f"## Spot ETF Flows — {asset_key} (Farside, net US$m)"]
    if market_proxy:
        header_lines.append(
            f"_No spot ETF exists for '{asset}'; showing {asset_key} spot-ETF flows as a "
            f"market-wide crypto risk-on/off proxy, not an '{asset}'-specific signal._"
        )
    if stale:
        age = _days_stale(fetched_at)
        age_str = f"STALE by {age} days" if age is not None else "STALE (age unknown)"
        header_lines.append(
            f"_{age_str}: live fetch failed; showing the last cached snapshot "
            f"(fetched {fetched_at}). Treat with caution._"
        )
    header_lines.append(
        f"- Source: farside.co.uk{ASSET_PATHS[asset_key]} | Window ending {curr_date}"
    )
    header = "\n".join(header_lines) + "\n"

    if not visible:
        return (
            header + f"\nNo ETF flow rows on or before {curr_date}. "
            "Report this as no ETF-flow data for the date; do not fabricate values."
        )

    latest = visible[-1]
    window_start = (
        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")
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
    streak_word = {1: "inflow", -1: "outflow", 0: "flat"}[streak_sign]

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

    summary = (
        f"\n**Latest ({latest['date']}):** {latest['total']:+.1f} net\n"
        + cumulative_line
        + f"**Streak:** {streak}-day {streak_word} (over all available history)\n"
        + f"**Latest-day leaders:** {breakdown_str}\n"
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
        note = (
            f"\n_(no flow rows within the {look_back_days}-day window; showing the "
            f"latest available)_\n"
        )
    table = (
        "\n| Date | Net Flow |\n| --- | --- |\n"
        + "\n".join(f"| {r['date']} | {r['total']:+.1f} |" for r in shown)
        + "\n"
    )

    return header + summary + note + table
