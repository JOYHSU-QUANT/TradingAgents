import json
import logging
import os
import threading
import time
from typing import Annotated

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFDataException, YFException, YFRateLimitError

from .config import get_config
from .errors import VendorRateLimitError, VendorUnavailableError
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
            # "Answered" is the honest claim, not "served": yf_fetch_unhidden
            # hands back the caller's hidden answer (an empty frame, dict or
            # list) for the failures it restores — a 404, the library's own
            # expected conditions — and that arrives here indistinguishable
            # from data. Clearing on it
            # is wrong only about cost — the next tool call re-discovers the
            # throttle and pays one ladder — never about the verdict a caller
            # gets, so it does not buy a way to tell the two apart.
            reset_yf_throttle_latch()
            return result


# Serializes yf_fetch_unhidden's flip/fetch/restore of the process-global
# hidden-exceptions flag. ToolNode runs the tool calls of one model message on
# a thread pool, and the fundamentals analyst binds the three statement tools
# together — unsynchronized, an interleaved backup capture could restore the
# flag mid-fetch (re-swallowing the very throttle this exists to surface) or
# leave it stuck False process-wide. The retry sleeps in yf_retry sit outside
# the locked window, so a throttled call does not hold the lock while backing
# off.
_UNHIDE_LOCK = threading.Lock()


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status an ``HTTPError`` carries, or ``None`` for any other error.

    Both transport libraries attach the response: curl_cffi's
    ``raise_for_status`` builds ``HTTPError(msg, 0, response)`` and requests'
    sets ``.response`` the same way (measured, curl_cffi 0.15.0).
    """
    return getattr(getattr(exc, "response", None), "status_code", None)


def yf_fetch_unhidden(func, *, hidden_answer):
    """Run one yfinance call with the library's exception swallow switched off.

    While ``YfConfig.debug.hide_exceptions`` is on (the default) yfinance's
    scrapers catch their own failures and answer with something empty: the
    statement properties and ``Ticker.history`` an empty frame, ``info`` a
    stub dict, ``insider_transactions`` an empty frame, ``get_news`` an empty
    list (verified on yfinance 1.4.1, the pinned floor). That swallow hid two
    things this boundary exists to surface — a throttle (#67) and a transport
    failure (#116), which read as "no data" or "no filings" and never reached
    :func:`yf_retry`'s mapping or the router's fallback. The backup/restore
    shape mirrors the library's own ``multi._download_one`` — but NOT its
    flag: that code flips ``network.hide_exceptions``, which nothing in
    yfinance 1.4.1 reads; ``debug.hide_exceptions`` is the one the scrapers
    consult, so flipping the network one would silently reinstate the swallow.

    What comes out of the window, in order:

    * ``YFRateLimitError`` — for :func:`yf_retry`'s mapping.
    * ``VendorUnavailableError``, mapped from two things Yahoo answers that
      are not data: its "Will be right back" page (yfinance's own
      ``YFDataException``, raised regardless of the flag) and a body that is
      not JSON. yfinance parses the body before it looks at the status, so a
      5xx HTML page on ``history``, ``get_news``, ``Search`` or ``info``'s
      second (fundamentals-timeseries) fetch arrives as a ``JSONDecodeError``
      rather than the ``OSError`` below; Yahoo answers JSON for every "no
      data" case (a ``chart.error``, a quoteSummary 404), so an unparsable
      body is never that. Restored to the empty answer it read as "No news
      found" or the no-data sentinel; let out raw, the getters' broad handler
      made prose of it (#136).
    * An ``OSError`` that is NOT an HTTP 404 — a reset, a timeout, a 5xx,
      and a 401/403 (Yahoo refusing this client over a crumb or an IP block).
      A 404 is Yahoo's verdict on the symbol, not a failure of the wire:
      quoteSummary answers it for an unknown or delisted symbol (measured),
      and ``history`` reaches that same 404 through its timezone lookup for
      the first symbols a process asks about. Under the swallow that became
      the empty answer that reaches the no-data lane, and it still does.

    Everything else is restored to ``hidden_answer()`` — the value the
    library would have answered with, so a delisted symbol's
    ``YFTzMissingError`` reaches the no-data lane as the empty frame it always
    was — and logged here, since the library's own error line is skipped once
    its swallow no longer runs. ``hidden_answer`` is required so every call
    site names the library's empty form for its property.
    """
    from yfinance.config import YfConfig

    def _call():
        with _UNHIDE_LOCK:
            backup = YfConfig.debug.hide_exceptions
            YfConfig.debug.hide_exceptions = False
            try:
                return func()
            except YFRateLimitError:
                raise
            except (YFDataException, json.JSONDecodeError) as e:
                # Yahoo answered, but not with data — see the docstring. Mapped
                # here rather than let out: the getters re-raise VendorError
                # and nothing else non-OSError, so raw these reached their
                # broad handler and came back as prose (#136).
                raise VendorUnavailableError(f"Yahoo Finance answered without data: {e}") from e
            except OSError as e:
                if _http_status(e) == 404:
                    # 404 alone: a 401/403 is Yahoo refusing this client (a
                    # crumb or an IP block, the case _make_request's cookie
                    # switch exists for), which must reach the fallback chain
                    # like a 5xx rather than read as "symbol not covered".
                    logger.info("yfinance answered HTTP 404: %s", e)
                    return hidden_answer()
                # Under the swallow this became the empty answer, which the
                # statement lane turned into NoMarketDataError and the router's
                # no-data sentinel then ranked above the recorded failure — so
                # the agent read "symbol not covered" and the fallback vendor
                # was never tried (#116).
                raise
            except Exception as e:
                # A traceback for a library bug; one line for the library's own
                # expected conditions (a symbol with no rows in range, no
                # timezone), which yfinance itself logs without one.
                logger.warning(
                    "yfinance fetch failed: %s", e, exc_info=not isinstance(e, YFException)
                )
                return hidden_answer()
            finally:
                YfConfig.debug.hide_exceptions = backup

    return yf_retry(_call)


def yf_fetch_statement(func):
    """Fetch a yfinance statement frame through :func:`yf_fetch_unhidden`.

    Kept as its own name because the statement getters' seam is pinned by
    name in the tests: a statement fetched through plain ``yf_retry`` would
    let the scraper swallow a throttle into an empty frame again (#67).
    """
    return yf_fetch_unhidden(func, hidden_answer=pd.DataFrame)


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
        # throttle, and is the fetch get_YFin_data_online already uses. Its
        # own swallow of everything else is switched off for the call so a
        # transport failure surfaces rather than reading as "no rows" (#116).
        ticker_obj = yf.Ticker(canonical)
        downloaded = yf_fetch_unhidden(
            lambda: ticker_obj.history(
                start=start_str, end=end_str, auto_adjust=True, actions=False
            ),
            hidden_answer=pd.DataFrame,
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


def coerce_period_labels(labels) -> tuple[list, bool]:
    """Parse date-ish labels ONE AT A TIME into zone-free timestamps.

    Returns one value per label — a tz-naive ``Timestamp``, or ``NaT`` for
    anything that is not a single date — plus whether any label carried a zone.

    Every statement-side reader of these labels shares this so they agree on
    what a label means: the look-ahead filter, the "did any column carry a
    fiscal period at all" measurement, and the freshness note. Handling them as
    one index instead produced two distinct failures on a tz-aware frame, both
    measured on pandas 2.3.3, both of which reached the getters' broad except
    and came back as ``"Error retrieving balance sheet for AAPL: ..."`` — a
    string ``route_to_vendor`` reads as a successful statement report (#110):

    * a tz-aware index will not compare against a naive cutoff —
      ``TypeError: Invalid comparison between dtype=datetime64[ns, UTC] and
      Timestamp``;
    * coercing a mixed set can raise ``ValueError: Cannot mix tz-aware with
      tz-naive values`` before any comparison happens. Which mixtures do that is
      narrower and less predictable than "aware plus naive": a naive
      ``datetime.date``, ``datetime64`` or ``int`` beside an aware timestamp
      raises in either order, a naive ``str`` raises only when the aware value
      comes first, and naive ``datetime``/``Timestamp`` values do not raise at
      all — those coerce to one zone with the odd entries as ``NaT``, which is
      the narrow case the vectorised call was first written for.

    Per label the mixing question does not arise: each parses on its own terms
    and an unparseable one is ``NaT``, which compares False against any cutoff
    exactly as the vectorised ``errors="coerce"`` did.

    The zone is DROPPED rather than converted to UTC, and for midnight-anchored
    labels the two differ exactly where it matters: measured across offsets
    -12..+14 with 11 label days and 21 cutoffs, every WESTWARD offset disagrees
    on one cutoff — the label's own date, which converting pushes past that
    day's midnight and out of the window (a period ending 2020-06-30 in
    ``America/New_York`` becomes 04:00 UTC on 2020-06-30 and fails a 2020-06-30
    cutoff) — while every offset at or east of UTC agrees on all of them. A
    fiscal period ends on the day its label names, so dropping is the reading
    that keeps it.

    A label the parser reads but cannot make a date of — ``None``, a non-date
    string, a value it coerces to an Index rather than a scalar — becomes
    ``NaT``. A label whose TYPE it refuses outright raises, and BOTH exception
    families are reachable as column labels: an iterator or a nested tuple
    raises ``TypeError``, a dict-like raises ``ValueError``, and a label need
    not be hashable to get there (``df.columns = pd.Index([...], dtype=object)``
    accepts either). Both readers guard for both families: the statement lane
    types the raise (``y_finance._statement_report``) and the freshness note
    degrades to silence on it (``y_finance._dates_lag_note``).
    """
    periods, dropped_a_zone = [], False
    for label in labels:
        parsed = pd.to_datetime(label, errors="coerce")
        if not isinstance(parsed, pd.Timestamp):
            # NaT, None, or a container label that coerced to an Index.
            periods.append(pd.NaT)
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.tz_localize(None)
            dropped_a_zone = True
        periods.append(parsed)
    return periods, dropped_a_zone


def filter_financials_by_date(
    data: pd.DataFrame, curr_date: str | None, coerced: tuple[list, bool] | None = None
) -> pd.DataFrame:
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
    production and stands as the contract for a direct caller. It is
    unconditional: the check sits ABOVE the empty-frame short circuit, so an
    empty frame with a broken bound refuses too — nothing to leak there, but
    the sentence this docstring makes should not hold only sometimes (#117).

    ``coerced`` lets a caller that has already run the columns through
    :func:`coerce_period_labels` hand the result over instead of paying for —
    and warning about — a second pass on the same labels: the statement lane
    measures "did any column carry a fiscal period" on them first (#112).

    The raise, not the normalisation below it, is what makes the ``is None``
    test safe. Testing falsiness (what this did before) sent ``""`` down the
    no-bound lane, so the frame came back whole — a look-ahead leak, but a
    visible one. Testing ``is None`` without the raise would be worse: ``""``
    would reach ``pd.Timestamp("")``, which is ``NaT``, and a ``NaT`` cutoff
    compares False against every column, so the frame would come back EMPTY with
    nothing said (measured, pandas 2.3.3). Past the raise the value is already
    ``strptime``-valid, so building the cutoff from the normalised form rather
    than the raw string is only a canonical-form convention.

    Column labels go through :func:`coerce_period_labels`, and the surviving
    ones are relabelled to what was compared whenever a zone was dropped. That
    relabelling keeps the rendered CSV header from depending on the vendor
    build, and it is what lets the freshness note downstream read the served
    labels back. It is skipped when no label carried a zone, so an ordinary
    naive frame's labels are passed through untouched — and a date-less call
    returns above without reaching any of it.
    """
    if curr_date is None:
        return data
    normalized = normalize_iso_date(curr_date)
    if normalized is None:
        raise ValueError(
            f"yfinance financials: curr_date {curr_date!r} is not a valid "
            f"YYYY-MM-DD date; refusing to serve statements unfiltered (look-ahead guard)"
        )
    if data.empty:
        return data
    cutoff = pd.Timestamp(normalized)
    periods, dropped_a_zone = coerced if coerced is not None else coerce_period_labels(data.columns)
    if len(periods) != len(data.columns):
        # The mask below is applied positionally, so labels coerced from a
        # different frame would silently keep the wrong columns.
        raise ValueError(
            f"yfinance financials: {len(periods)} coerced labels handed in for "
            f"{len(data.columns)} columns; coerce the frame being filtered"
        )
    mask = [not pd.isna(p) and p <= cutoff for p in periods]
    kept = data.loc[:, mask]
    if dropped_a_zone:
        kept = kept.set_axis([p for p, keep in zip(periods, mask, strict=True) if keep], axis=1)
    return kept


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
