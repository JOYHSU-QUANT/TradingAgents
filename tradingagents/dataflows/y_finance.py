import logging
from datetime import datetime
from typing import Annotated

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta

from .errors import VendorError
from .stockstats_utils import (
    StockstatsUtils,
    _assert_ohlcv_not_stale,
    filter_financials_by_date,
    load_ohlcv,
    yf_fetch_statement,
    yf_retry,
)
from .symbol_utils import NoMarketDataError, normalize_symbol

# The insider-filing bound lives in utils so the Alpha Vantage vendor serving
# the same routed tool shares the single definition (#69).
from .utils import (
    MAX_INSIDER_LAG_DAYS,
    curr_date_refusal,
    data_lag_note,
    live_snapshot_note,
    statement_lag_bound,
)

logger = logging.getLogger(__name__)


def _statement_report(data, ticker, canonical, curr_date, freq, noun: str, title: str) -> str:
    """Judge, filter and render one fetched statement frame.

    The three statement getters differ only in which yfinance property they
    fetch and what that statement is called, so everything downstream of the
    fetch lives here — this lane's ordering rule was already edited in three
    places once (#89) and should not be again. ``title`` is passed rather than
    derived from ``noun``: the agent reads it, and ``str.title()`` would mis-case
    the first acronym anyone adds.

    Emptiness is judged before the analysis date, the order the Alpha Vantage
    path uses: "this symbol has nothing" is true regardless of curr_date, so an
    unknown symbol reaches the router's no-data lane through either vendor
    rather than one of them answering about the date instead.
    """
    if data.empty:
        raise NoMarketDataError(ticker, canonical, f"no {noun} data")
    if (refusal := curr_date_refusal(curr_date)) is not None:
        return refusal

    # Measured before filtering, because a frame can also empty by having no
    # date-like columns at all — yfinance renaming or nulling them coerces every
    # label to NaT, which compares False against any cutoff. That is a vendor
    # schema break, and reporting it as "nothing on or before your date" would
    # describe correct point-in-time behaviour instead. The Alpha Vantage side
    # separates the same two cases, and logs only this one, for the same reason:
    # a schema break otherwise reports every ticker as an uncovered symbol.
    columns = len(data.columns)
    try:
        datable = int(pd.to_datetime(data.columns, errors="coerce").notna().sum())
    except (TypeError, ValueError):
        # Guarded for the same reason _dates_lag_note guards, and NOT redundant
        # with the coercion inside filter_financials_by_date: on the date-less
        # lane that one never runs, because it early-returns before coercing.
        # No input has been found that makes errors="coerce" raise on the pinned
        # pandas, so this is defence, not a live path; treating an index we
        # cannot read as carrying no usable period is the conservative reading.
        datable = 0

    data = filter_financials_by_date(data, curr_date)
    if data.empty:
        if not datable:
            logger.warning(
                "yfinance %s for %s: none of the %d columns carried a usable fiscal period",
                noun,
                ticker,
                columns,
            )
            raise NoMarketDataError(
                ticker, canonical, f"all {columns} {noun} columns carried no usable fiscal period"
            )
        raise NoMarketDataError(ticker, canonical, f"no {noun} data on or before {curr_date}")

    header = f"# {title} data for {canonical} ({freq})\n"
    header += _statement_lag_note(data, curr_date, freq, f"{noun} period")
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + data.to_csv()


def _dates_lag_note(values, curr_date: str | None, max_lag_days: int, what: str) -> str:
    """Data-lag note line (``"# …\\n"`` or ``""``) for a set of date-ish values.

    Shared by the statement and insider paths: coerce, take the newest, and
    compare it against the reference date. The coercion is guarded because an
    annotation must degrade, never replace the report it decorates with an error
    string. The example this once cited — mixed tz-aware and naive timestamps on
    pandas >= 2 — does NOT raise on the pinned pandas: it yields a single-tz
    index with the mismatched entries as ``NaT``, and which side is mismatched
    follows the first element's tz-awareness. No input has been found that makes
    ``errors="coerce"`` raise there, so treat the guard as defence whose trigger
    is unproven rather than as evidence that one exists.
    """
    if curr_date is None:  # neither caller can reach this; kept as the contract (#89)
        return ""
    try:
        dates = pd.to_datetime(values, errors="coerce").dropna()
    except (TypeError, ValueError):
        return ""
    if not len(dates):
        return ""
    note = data_lag_note(dates.max(), curr_date, max_lag_days, what)
    return f"# {note}\n" if note else ""


def _statement_lag_note(data: pd.DataFrame, curr_date: str | None, freq: str, what: str) -> str:
    """Data-lag note line for a financial-statement frame, or ``""``.

    The newest column left after :func:`filter_financials_by_date` is the
    newest fiscal period the agent will see; compare it against the date being
    analysed with a freq-appropriate bound (an annual statement is ~a year old
    by definition). The bound — and the unknown-freq fallback — come from
    :func:`statement_lag_bound` so the Alpha Vantage statement path flags the
    same gap (#58).

    A missing curr_date (the model omitted it) falls back to the wall clock
    rather than switching the note off with the look-ahead filter (#73): the
    filter genuinely needs a point-in-time bound, but the disclosure only needs
    a reference date. The degraded, unfiltered mode is logged because both
    protections used to vanish silently.

    Only ``None`` is that case. A supplied-but-unusable curr_date never reaches
    here — the getters answer the shared ``INVALID_CURR_DATE`` sentinel first
    (#89) — so this tests for it rather than for falsiness, which used to route
    an empty string into the omitted-argument lane.
    """
    if data.empty:
        return ""
    if curr_date is None:
        logger.warning(
            "yfinance %s served without curr_date: look-ahead filtering is "
            "off; freshness is judged against today instead",
            what,
        )
        curr_date = datetime.now().strftime("%Y-%m-%d")
    return _dates_lag_note(data.columns, curr_date, statement_lag_bound(freq), what)


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):

    datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Resolve broker/forex symbols to Yahoo's convention (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)
    ticker = yf.Ticker(canonical)

    # yfinance treats ``end`` as EXCLUSIVE, so it would drop the requested
    # end_date row (and the current day when end_date is today). Request one day
    # past end_date so the requested range is actually inclusive (#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")
    data = yf_retry(lambda: ticker.history(start=start_date, end=end_inclusive))

    # Empty result means the symbol is unknown/delisted. Raise a typed error
    # instead of returning prose: the routing layer turns it into a single
    # unambiguous "no data" signal so the agent never fabricates a price.
    if data.empty:
        raise NoMarketDataError(symbol, canonical, f"no rows between {start_date} and {end_date}")

    # Remove timezone info from index for cleaner output
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    # Reject a stale frame (e.g. a year-old partial response) before it is
    # formatted into the report. Raises NoMarketDataError, which the router
    # turns into one clear unavailable signal (#1021).
    _assert_ohlcv_not_stale(data, end_date, symbol, canonical)

    # Round numerical values to 2 decimal places for cleaner display
    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in data.columns:
            data[col] = data[col].round(2)

    # Convert DataFrame to CSV string
    csv_string = data.to_csv()

    # Add header information; note the resolved symbol when it differs so the
    # agent (and user) can see which instrument was actually priced.
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:

    best_ind_params = {
        # Moving Averages
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD Related
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # Momentum Indicators
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # Volatility Indicators
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # Volume-Based Indicators
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # Optimized: Get stock data once and calculate indicators for all dates
    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, curr_date)

        # Generate the date range we need
        current_dt = curr_date_dt
        date_values = []

        while current_dt >= before:
            date_str = current_dt.strftime("%Y-%m-%d")

            # Look up the indicator value for this date
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
            else:
                # Honest wording: a missing date may be a weekend/holiday OR a
                # trading day whose row failed integrity cleaning (#38).
                indicator_value = "N/A: no usable OHLCV row for this date (non-trading day, or the vendor row failed integrity checks)"

            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        # Build the result string
        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"

    except VendorError:
        # Caught as the taxonomy's base type, not one leaf at a time (#67):
        # no-data keeps its sentinel lane, and a rate limit now reaches the
        # router's rate-limit lane instead of the broad handler below — whose
        # per-day fallback loop re-runs the same throttled fetch and then
        # renders prose the router reads as a successful answer.
        raise
    except Exception as e:
        print(f"Error getting bulk stockstats data: {e}")
        # Fallback to original implementation if bulk method fails
        ind_string = ""
        curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        while curr_date_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, curr_date_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{curr_date_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            curr_date_dt = curr_date_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )

    return result_str


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    curr_date: Annotated[str, "current date for reference"],
) -> dict:
    """
    Optimized bulk calculation of stock stats indicators.
    Fetches data once and calculates indicator for all available dates.
    Returns dict mapping date strings to indicator values.
    """
    from stockstats import wrap

    data = load_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    # Calculate the indicator for all rows at once
    df[indicator]  # This triggers stockstats to calculate the indicator

    # Create a dictionary mapping date strings to indicator values
    result_dict = {}
    for _, row in df.iterrows():
        date_str = row["Date"]
        indicator_value = row[indicator]

        # Handle NaN/None values
        if pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)

    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
) -> str:

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_date_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = StockstatsUtils.get_stock_stats(
            symbol,
            indicator,
            curr_date,
        )
    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        print(
            f"Error getting stockstats indicator data for indicator {indicator} on {curr_date}: {e}"
        )
        return ""

    return str(indicator_value)


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[
        str | None,
        "analysis date in yyyy-mm-dd format; yfinance serves only live values, "
        "so this triggers a disclosure when it trails today",
    ] = None,
):
    """Get company fundamentals overview from yfinance."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)
        info = yf_retry(lambda: ticker_obj.info)

        if not info:
            raise NoMarketDataError(ticker, canonical, "no fundamentals returned")

        fields = [
            ("Name", info.get("longName")),
            ("Sector", info.get("sector")),
            ("Industry", info.get("industry")),
            ("Market Cap", info.get("marketCap")),
            ("PE Ratio (TTM)", info.get("trailingPE")),
            ("Forward PE", info.get("forwardPE")),
            ("PEG Ratio", info.get("pegRatio")),
            ("Price to Book", info.get("priceToBook")),
            ("EPS (TTM)", info.get("trailingEps")),
            ("Forward EPS", info.get("forwardEps")),
            ("Dividend Yield", info.get("dividendYield")),
            ("Beta", info.get("beta")),
            ("52 Week High", info.get("fiftyTwoWeekHigh")),
            ("52 Week Low", info.get("fiftyTwoWeekLow")),
            ("50 Day Average", info.get("fiftyDayAverage")),
            ("200 Day Average", info.get("twoHundredDayAverage")),
            ("Revenue (TTM)", info.get("totalRevenue")),
            ("Gross Profit", info.get("grossProfits")),
            ("EBITDA", info.get("ebitda")),
            ("Net Income", info.get("netIncomeToCommon")),
            ("Profit Margin", info.get("profitMargins")),
            ("Operating Margin", info.get("operatingMargins")),
            ("Return on Equity", info.get("returnOnEquity")),
            ("Return on Assets", info.get("returnOnAssets")),
            ("Debt to Equity", info.get("debtToEquity")),
            ("Current Ratio", info.get("currentRatio")),
            ("Book Value", info.get("bookValue")),
            ("Free Cash Flow", info.get("freeCashflow")),
        ]

        lines = []
        for label, value in fields:
            if value is not None:
                lines.append(f"{label}: {value}")

        # yfinance returns a stub dict (e.g. {"trailingPegRatio": None}) for
        # unknown symbols, so `info` is truthy but every field is empty. Treat
        # "no usable fields" as no data rather than emitting a bare header the
        # agent might fabricate around.
        if not lines:
            raise NoMarketDataError(ticker, canonical, "no fundamental fields returned")

        # Refused at the same depth as the Alpha Vantage overview path, whose
        # docstring gives the reasoning: with no usable analysis date neither
        # vendor can tell a backtest from live trading (#89).
        if (refusal := curr_date_refusal(curr_date)) is not None:
            return refusal

        header = f"# Company Fundamentals for {canonical}\n"
        # yfinance ``info`` is a live current-state snapshot with no
        # historical form; when the analysis date sits behind the wall clock
        # (a backtest), say so or today's ratios read as that date's (#30).
        if curr_date is not None:
            snapshot_note = live_snapshot_note(curr_date, "these fundamentals are")
            if snapshot_note:
                header += f"# {snapshot_note}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + "\n".join(lines)

    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        return f"Error retrieving fundamentals for {ticker}: {str(e)}"


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get balance sheet data from yfinance."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        # yf_fetch_statement, not plain yf_retry: the statement properties
        # swallow a 429 into an empty frame under yfinance's default hidden-
        # exception mode, and "no data" must not be the verdict for a throttle (#67).
        if freq.lower() == "quarterly":
            data = yf_fetch_statement(lambda: ticker_obj.quarterly_balance_sheet)
        else:
            data = yf_fetch_statement(lambda: ticker_obj.balance_sheet)

        return _statement_report(
            data, ticker, canonical, curr_date, freq, "balance sheet", "Balance Sheet"
        )

    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        return f"Error retrieving balance sheet for {ticker}: {str(e)}"


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get cash flow data from yfinance."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        # See get_balance_sheet for why these go through yf_fetch_statement.
        if freq.lower() == "quarterly":
            data = yf_fetch_statement(lambda: ticker_obj.quarterly_cashflow)
        else:
            data = yf_fetch_statement(lambda: ticker_obj.cashflow)

        return _statement_report(data, ticker, canonical, curr_date, freq, "cash flow", "Cash Flow")

    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        return f"Error retrieving cash flow for {ticker}: {str(e)}"


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get income statement data from yfinance."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        # See get_balance_sheet for why these go through yf_fetch_statement.
        if freq.lower() == "quarterly":
            data = yf_fetch_statement(lambda: ticker_obj.quarterly_income_stmt)
        else:
            data = yf_fetch_statement(lambda: ticker_obj.income_stmt)

        return _statement_report(
            data, ticker, canonical, curr_date, freq, "income statement", "Income Statement"
        )

    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        return f"Error retrieving income statement for {ticker}: {str(e)}"


def get_insider_transactions(ticker: Annotated[str, "ticker symbol of the company"]):
    """Get insider transactions data from yfinance."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)
        data = yf_retry(lambda: ticker_obj.insider_transactions)

        # Empty is normal here (many valid symbols have no insider filings),
        # so report it plainly rather than treating the symbol as invalid.
        if data is None or data.empty:
            return f"No insider transactions reported for symbol '{canonical}'"

        # Convert to CSV string for consistency with other functions
        csv_string = data.to_csv()

        # Freshness: relative to the wall clock (no curr_date reaches this
        # path). The bound is generous because sparse filings are normal —
        # this flags a long-dead stream, not a quiet quarter (#30). No
        # recognizable date column just skips the note (degrade, never raise);
        # a duplicated column label would select a DataFrame, so only a
        # genuine Series is inspected.
        lag_line = ""
        date_col = next(
            (c for c in data.columns if isinstance(c, str) and "date" in c.lower()), None
        )
        if date_col is not None:
            col = data[date_col]
            if isinstance(col, pd.Series):
                lag_line = _dates_lag_note(
                    col,
                    datetime.now().strftime("%Y-%m-%d"),
                    MAX_INSIDER_LAG_DAYS,
                    "insider filing",
                )

        # Add header information
        header = f"# Insider Transactions data for {canonical}\n"
        header += lag_line
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + csv_string

    except VendorError:
        raise  # Typed vendor failures take their router lanes (#67)
    except Exception as e:
        return f"Error retrieving insider transactions for {ticker}: {str(e)}"
