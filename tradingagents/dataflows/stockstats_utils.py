import logging
import os
import threading
import time
from typing import Annotated

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFRateLimitError

from .config import get_config
from .errors import VendorRateLimitError
from .symbol_utils import NoMarketDataError, normalize_symbol

# The staleness bound lives in utils (stdlib-only) so the pure-requests Alpha
# Vantage vendor shares the same single definition (#70).
from .utils import MAX_OHLCV_STALE_DAYS, normalize_iso_date, safe_ticker_component

logger = logging.getLogger(__name__)


# How long an exhausted throttle keeps every other yfinance call away from
# Yahoo. The design treats a 429 as a fact about this client's standing with
# Yahoo rather than about one endpoint: once a call has paid the full backoff
# ladder and still been refused, the tools queued behind it have nothing new to
# learn by each re-discovering the same refusal, sleeping through a ladder of
# their own and adding requests to a host already turning this client away
# (#86). The window is a judgement call, not a measured property of Yahoo's
# throttling: long enough to cover the tool calls of one decision cycle, and
# far shorter than the perp scheduler's CYCLE_INTERVAL (hours), so the next
# cycle always re-probes Yahoo rather than inheriting a latch.
_THROTTLE_LATCH_TTL_S = 300.0

# Guards _throttle_latched_until. ToolNode runs the tool calls of one model
# message on a thread pool, so arming and reading race without it.
_THROTTLE_LATCH_LOCK = threading.Lock()

# monotonic deadline until which yfinance is presumed throttled; None when not
# latched. Process-global on purpose — the throttle is a property of this
# client's relationship with Yahoo, not of any one caller.
_throttle_latched_until: float | None = None


def reset_yf_throttle_latch() -> None:
    """Forget any recorded throttle, so the next call contacts Yahoo again.

    Called by ``yf_retry`` whenever a request is served, and public for tests:
    the latch is process-global, so a test that exhausts ``yf_retry`` would
    otherwise send every later test in the same process down the fast-fail
    path. The ``tests/`` conftest calls this around every test.
    """
    global _throttle_latched_until
    with _THROTTLE_LATCH_LOCK:
        _throttle_latched_until = None


def _throttle_latch_remaining_s() -> float | None:
    """Seconds left on the latch, or None when yfinance may be contacted."""
    with _THROTTLE_LATCH_LOCK:
        if _throttle_latched_until is None:
            return None
        remaining = _throttle_latched_until - time.monotonic()
    return remaining if remaining > 0 else None


def _arm_throttle_latch() -> None:
    global _throttle_latched_until
    with _THROTTLE_LATCH_LOCK:
        _throttle_latched_until = time.monotonic() + _THROTTLE_LATCH_TTL_S


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.

    A throttle that survives every retry is re-raised as the taxonomy's
    ``VendorRateLimitError``: this wrapper is the one boundary every yfinance
    network call goes through, and yfinance's own ``YFRateLimitError`` is not a
    type the routing layer knows, so leaving it unmapped sent a 429 into each
    caller's broad ``except`` and came back as a successful-looking error
    string the router never fell back on (#67).

    That same "one boundary" property is what lets an exhausted throttle arm a
    short-lived latch (:data:`_THROTTLE_LATCH_TTL_S`): while it holds, calls
    raise the taxonomy error immediately instead of sleeping through a ladder
    of their own (#86). A call that comes back with an answer clears it.
    Nothing but an exhausted throttle arms it, so the un-throttled path is
    unchanged.
    """
    remaining = _throttle_latch_remaining_s()
    if remaining is not None:
        raise VendorRateLimitError(
            f"Yahoo Finance rate limited a recent request; skipping this one "
            f"without contacting the vendor for another {remaining:.0f}s"
        )

    for attempt in range(max_retries + 1):
        try:
            result = func()
        except YFRateLimitError as e:
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
            else:
                _arm_throttle_latch()
                raise VendorRateLimitError(
                    f"Yahoo Finance rate limited the request and {max_retries} "
                    f"retries did not clear it: {e}"
                ) from e
        else:
            # Yahoo answered without refusing, so drop the deadline rather than
            # leaving a stale one to reason about later. Unconditional on
            # purpose: usually there is nothing to clear (a live latch raises
            # above, and a throttle these retries cleared never armed one), but
            # a sibling thread can arm one while this call is in flight, and an
            # answer just received is the fresher evidence.
            #
            # "Answered" is the honest claim, not "served": yf_fetch_statement
            # hands back an empty frame when it swallows a NON-throttle failure,
            # and that arrives here indistinguishable from data. Clearing on it
            # is wrong only about cost — the next tool call re-discovers the
            # throttle and pays one ladder — never about the verdict a caller
            # gets, so it does not buy a way to tell the two apart.
            reset_yf_throttle_latch()
            return result


# Serializes yf_fetch_statement's flip/fetch/restore of the process-global
# hidden-exceptions flag. ToolNode runs the tool calls of one model message on
# a thread pool, and the fundamentals analyst binds the three statement tools
# together — unsynchronized, an interleaved backup capture could restore the
# flag mid-fetch (re-swallowing the very throttle this exists to surface) or
# leave it stuck False process-wide. The retry sleeps in yf_retry sit outside
# the locked window, so a throttled statement does not hold the lock while
# backing off.
_STATEMENT_FLAG_LOCK = threading.Lock()


def yf_fetch_statement(func):
    """Fetch a yfinance statement frame with throttles made visible.

    The statement properties (``balance_sheet``/``cashflow``/``income_stmt``
    and their quarterly forms) swallow ``YFRateLimitError`` inside yfinance's
    fundamentals scraper while ``YfConfig.debug.hide_exceptions`` is on (the
    default), answering with an empty frame — so a throttle would read as
    "no data" and never reach :func:`yf_retry`'s mapping (#67; verified
    empirically on yfinance 1.4.1, the pinned floor). The backup/restore shape
    mirrors the library's own ``multi._download_one`` — but NOT its flag:
    that code flips ``network.hide_exceptions``, which nothing in yfinance
    1.4.1 reads; ``debug.hide_exceptions`` is the one the scrapers consult,
    so flipping the network one would silently reinstate the swallow. Hidden
    mode is switched off for the call, ONLY the rate limit is re-raised, and
    every other exception is restored to the swallowed-empty frame the
    library would have answered with (logged here, since the library's own
    error log line is skipped once its swallow no longer runs).
    """
    from yfinance.config import YfConfig

    def _call():
        with _STATEMENT_FLAG_LOCK:
            backup = YfConfig.debug.hide_exceptions
            YfConfig.debug.hide_exceptions = False
            try:
                return func()
            except YFRateLimitError:
                raise
            except Exception as e:
                logger.warning("yfinance statement fetch failed: %s", e, exc_info=True)
                return pd.DataFrame()
            finally:
                YfConfig.debug.hide_exceptions = backup

    return yf_retry(_call)


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows.

    Honesty guard (#38): a row missing any of Open/High/Low/Close is DROPPED,
    never forward/back-filled — the old fill fabricated prices that fed
    indicators and were then rendered as "verified". Rows with an impossible
    OHLC ordering (low must bound the open/close body from below, high from
    above) or non-positive prices are dropped for the same reason; a missing
    Volume stays NaN so downstream rendering shows N/A instead of a made-up
    number.
    """
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")

    ohlc = [c for c in ("Open", "High", "Low", "Close") if c in data.columns]
    complete = data.dropna(subset=ohlc)
    dropped_incomplete = len(data) - len(complete)
    data = complete

    dropped_disordered = 0
    if ohlc and not data.empty:
        # Positivity applies to whichever OHLC columns exist; the body/range
        # ordering check additionally needs all four columns present.
        valid = (data[ohlc] > 0).all(axis=1)
        if len(ohlc) == 4:
            body_low = data[["Open", "Close"]].min(axis=1)
            body_high = data[["Open", "Close"]].max(axis=1)
            valid &= (data["Low"] <= body_low) & (body_high <= data["High"])
        dropped_disordered = int((~valid).sum())
        data = data[valid]

    if dropped_incomplete or dropped_disordered:
        logger.warning(
            "Dropped %d OHLCV row(s) with missing fields and %d with impossible "
            "OHLC ordering or non-positive prices instead of fabricating values",
            dropped_incomplete,
            dropped_disordered,
        )

    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    curr_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than curr_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # Cache uses a fixed window (5y to today) so one file per symbol.
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when curr_date is the current day (#986). Look-ahead is still prevented by
    # the curr_date filter below.
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        # Fetched through Ticker.history, NOT yf.download: download's
        # per-ticker worker swallows YFRateLimitError and answers with an
        # empty frame (verified empirically on yfinance 1.4.1, the pinned
        # floor), so a throttle would read as "no rows" and yf_retry's
        # rate-limit mapping would never fire (#67). history re-raises the
        # throttle, and is the fetch get_YFin_data_online already uses.
        ticker_obj = yf.Ticker(canonical)
        downloaded = yf_retry(
            lambda: ticker_obj.history(
                start=start_str, end=end_str, auto_adjust=True, actions=False
            )
        )
        # history keeps a tz-aware index; strip it (like get_YFin_data_online)
        # so the Date column compares cleanly against the naive curr_date cutoff.
        if isinstance(downloaded.index, pd.DatetimeIndex) and downloaded.index.tz is not None:
            downloaded.index = downloaded.index.tz_localize(None)
        downloaded = _ensure_date_column(downloaded.reset_index())
        # Only cache real data — never persist an empty frame.
        if downloaded.empty or "Close" not in downloaded.columns:
            raise NoMarketDataError(symbol, canonical, "Yahoo Finance returned no rows")
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    # Integrity cleaning plus the look-ahead cutoff can leave nothing usable.
    # Raise the classified error instead of returning an empty frame that
    # downstream date loops would mislabel as "not a trading day" (#38).
    if data.empty:
        raise NoMarketDataError(
            symbol,
            canonical,
            "no usable OHLCV rows on or before the requested date after integrity cleaning",
        )

    # Reject a stale frame (latest row far older than curr_date) rather than
    # feeding year-old prices into indicators (#1021).
    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str | None) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.

    ``None`` — the model omitted the argument — means no point-in-time bound was
    requested and the frame is served whole (#73). A present-but-unusable
    curr_date RAISES instead, mirroring the Alpha Vantage side's
    ``_filter_reports_by_date``: this backs a core fundamentals tool, so a broken
    bound must fail loud rather than silently leak future periods. The getters
    answer the shared sentinel first (#89), so that raise is unreachable in
    production and stands as the contract for a direct caller.

    The raise, not the normalisation below it, is what makes the ``is None``
    test safe. Testing falsiness (what this did before) sent ``""`` down the
    no-bound lane, so the frame came back whole — a look-ahead leak, but a
    visible one. Testing ``is None`` without the raise would be worse: ``""``
    would reach ``pd.Timestamp("")``, which is ``NaT``, and a ``NaT`` cutoff
    compares False against every column, so the frame would come back EMPTY with
    nothing said (measured, pandas 2.3.3). Past the raise the value is already
    ``strptime``-valid, so building the cutoff from the normalised form rather
    than the raw string is only a canonical-form convention.
    """
    if curr_date is None or data.empty:
        return data
    normalized = normalize_iso_date(curr_date)
    if normalized is None:
        raise ValueError(
            f"yfinance financials: curr_date {curr_date!r} is not a valid "
            f"YYYY-MM-DD date; refusing to serve statements unfiltered (look-ahead guard)"
        )
    cutoff = pd.Timestamp(normalized)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[str, "curr date for retrieving stock price data, YYYY-mm-dd"],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            # Honest wording: the date may be a weekend/holiday, but it may
            # also be a trading day whose row failed integrity cleaning —
            # don't assert "not a trading day" as fact (#38).
            return "N/A: no usable OHLCV row for this date (non-trading day, or the vendor row failed integrity checks)"
