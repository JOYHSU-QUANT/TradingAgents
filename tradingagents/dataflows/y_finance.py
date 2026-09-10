import logging
from datetime import datetime
from typing import Annotated

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta

from .errors import UnsupportedIndicatorError
from .symbol_utils import NoMarketDataError, normalize_symbol

# The insider-filing bound lives in utils so the Alpha Vantage vendor serving
# the same routed tool shares the single definition (#69).
from .utils import (
    INDICATOR_DESCRIPTIONS,
    MAX_INSIDER_LAG_DAYS,
    MAX_UNTRUSTED_CHARS,
    data_lag_note,
    date_range_refusal,
    date_refusal,
    echo_argument,
    live_snapshot_note,
    no_insider_transactions,
    sanitize_untrusted,
    statement_lag_bound,
    unsupported_indicator,
)
from .yfinance_common import (
    _assert_ohlcv_not_stale,
    coerce_period_labels,
    filter_financials_by_date,
    load_ohlcv,
    yf_fetch_statement,
    yf_fetch_unhidden,
)

logger = logging.getLogger(__name__)

# No impl in this module handles what it meets outside the vendor-error
# taxonomy and outside transport: a stockstats or pandas bug on a frame
# yfinance did serve leaves raw, and the router reads it as this vendor's
# library failing, routes past it and, when no vendor serves, renders one
# line of report text (#187, #219). Six of the getters here — every
# registered impl but get_YFin_data_online — used to wrap their fetch in a
# ``with`` block that did the conversion at the getter (nine across the
# repo), a per-getter decision that left their Alpha Vantage siblings
# aborting the run over the identical bug, and left get_YFin_data_online's
# own silence looking like a choice when it was an omission. It is a
# category's decision now (``interface.LOUD_LIBRARY_CATEGORIES``), and
# OHLCV — this module's get_YFin_data_online — is the category whose
# library failure is never rendered as text.
#
# What a getter runs BEFORE it asks yfinance anything is not the vendor's
# library, and says so with ``wiring_gap`` rather than by sitting above the
# block that used to be here (#111, #200).


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
    # kind="point", NOT "disclosure": this lane's curr_date genuinely BOUNDS
    # the data (filter_financials_by_date drops periods after it), so its
    # refusal must keep claiming a bound — only the live OVERVIEW lane below
    # is disclosure-only (#144/#140 review). omitted_ok stays: the date-less
    # #73 lane is legal, it just must not be advertised as a remedy.
    if (
        refusal := date_refusal(curr_date, what="fundamentals", kind="point", omitted_ok=True)
    ) is not None:
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
        # Through the SAME per-label rule the filter uses, and measured BEFORE
        # filtering. Read as one index this disagreed with the filter on a
        # tz-aware frame: the coercion raised, the count fell back to zero, and
        # a frame whose periods merely all postdate curr_date was then reported
        # as a vendor schema break — the opposite of what this measurement
        # exists to separate (measured, pandas 2.3.3). The coerced labels are
        # handed to the filter rather than re-derived there: each pass can
        # emit pandas' "Could not infer format" warning, and two passes named
        # this one site twice (#112).
        coerced = coerce_period_labels(data.columns)
        datable = sum(not pd.isna(p) for p in coerced[0])
        data = filter_financials_by_date(data, curr_date, coerced=coerced)
    except (TypeError, ValueError) as e:
        # The per-label parse covers both measured ways a tz-aware statement
        # frame used to reach here (#110); a label whose TYPE the parser refuses
        # outright still raises, and both statements above are inside this guard
        # so it leaves as a typed vendor failure the router can fall back from
        # rather than reaching the router's untyped lane and coming back as an
        # "Error retrieving ..." report line for a vendor that answered.
        # BOTH exception types, because pandas picks by label type and the two
        # families are equally reachable: an iterator or nested tuple raises
        # TypeError, a dict-like raises ValueError, and a column label need not
        # be hashable — ``df.columns = pd.Index([...], dtype=object)`` takes
        # either (measured, pandas 2.3.3). The filter's own two guards — an
        # unusable curr_date, labels coerced from another frame — are not a
        # second meaning to worry about here: they raise WiringGapError, which
        # this clause does not catch, so our own breakage is not filed as
        # something the vendor did (#219). The shared sentinel above answers
        # the curr_date case before anything is filtered anyway (#89), and
        # this is the only production caller of that filter.
        raise NoMarketDataError(
            ticker,
            canonical,
            # No mention of curr_date: this also fires on the date-less lane
            # (#73), which asked for no bound at all, and the detail is spliced
            # into the router's agent-facing sentinel.
            f"{noun} column labels could not be read as fiscal periods: {e}",
        ) from e
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

    # ``title`` is this module's own noun; the other two are the caller's —
    # the alias table's answer for the symbol it asked about, and the
    # frequency it named, which is read for one spelling ("quarterly") and
    # otherwise echoed as given. Both are flattened and capped on the way back
    # into a heading line the model reads (#233).
    header = f"# {title} data for {echo_argument(canonical)} ({echo_argument(freq)})\n"
    header += _statement_lag_note(data, curr_date, freq, f"{noun} period")
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + data.to_csv()


def _dates_lag_note(values, curr_date: str | None, max_lag_days: int, what: str) -> str:
    """Data-lag note line (``"# …\\n"`` or ``""``) for a set of date-ish values.

    Shared by the statement and insider paths: parse, take the newest, and
    compare it against the reference date. Parsing goes label by label through
    :func:`coerce_period_labels` for the reason given there — read as one index,
    a mixed tz-aware/naive set can raise, and an annotation must degrade rather
    than replace the report it decorates with an error string. Reading the whole
    index also put the ``max()`` OUTSIDE the guard, so a set that coerced to
    mixed offsets without raising failed there instead ("Cannot compare tz-naive
    and tz-aware timestamps", measured on ``[naive str, aware Timestamp]``,
    pandas 2.3.3) and reached the router's untyped lane; per label, every value
    handed to ``max`` is zone-free. The remaining guard is for a label whose
    TYPE the parser refuses outright, which stays a silent no-note here because
    an annotation must not be the thing that fails a report.
    """
    if curr_date is None:  # neither caller can reach this; kept as the contract (#89)
        return ""
    try:
        periods = [p for p in coerce_period_labels(values)[0] if not pd.isna(p)]
    except (TypeError, ValueError):
        return ""
    if not periods:
        return ""
    note = data_lag_note(max(periods), curr_date, max_lag_days, what)
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
    # Unusable dates are refused before any request, in the shared voice (#111).
    if (refusal := date_range_refusal(start_date, end_date, what="stock price data")) is not None:
        return refusal
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Resolve broker/forex symbols to Yahoo's convention (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)
    ticker = yf.Ticker(canonical)

    # yfinance treats ``end`` as EXCLUSIVE, so it would drop the requested
    # end_date row (and the current day when end_date is today). Request one day
    # past end_date so the requested range is actually inclusive (#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")
    # Un-hidden so a transport failure surfaces instead of being swallowed
    # into the empty frame the no-data check below would read as an unknown
    # symbol (#116); a genuinely missing symbol still answers that frame.
    data = yf_fetch_unhidden(
        lambda: ticker.history(start=start_date, end=end_inclusive),
        hidden_answer=pd.DataFrame,
    )

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
    # Both spellings are the caller's own, echoed flattened and capped into a
    # heading line the model reads (#233). The comparison stays on the RAW
    # pair: it asks whether the alias table changed the symbol, which is not a
    # question the flattening may answer.
    label = echo_argument(canonical)
    if canonical != symbol.upper():
        label += f" (from {echo_argument(symbol)})"
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

    # The shared cross-vendor table (utils.INDICATOR_DESCRIPTIONS) is this
    # vendor's whole supported set: every indicator here is computed from OHLCV
    # by stockstats, so a described indicator IS a servable one. It used to be
    # a function-local copy of the same sentences — two tables for one
    # agent-facing text, editable apart, and invisible to module-level drift
    # tests (#137).
    if indicator not in INDICATOR_DESCRIPTIONS:
        # The shared definition, so this refusal and the Alpha Vantage
        # sibling's cannot differ in which guard the rejected name takes: they
        # are two vendors of one routed tool, and guarding only this side would
        # leave them ending alike on a clean spelling and differently on a
        # hostile one (#219, #233). The menu stays this vendor's own.
        raise UnsupportedIndicatorError(unsupported_indicator(indicator, INDICATOR_DESCRIPTIONS))

    # Unusable dates are refused before any request, in the shared voice (#111).
    refusal = date_refusal(curr_date, what="indicator values", kind="point")
    if refusal is not None:
        return refusal
    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # One fetch for the whole window. A stockstats or pandas failure here is
    # deterministic — after the taxonomy (#67) and transport (#116) lanes
    # nothing the router reads as a library failure is transient — so it is not
    # re-run: this used to fall back to a per-day loop that performed the
    # identical fetch and calculation once per day of the window and
    # rendered a column of blanks under a successful-looking header (#137).
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

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        # Indexed, not .get() with a placeholder: membership was checked
        # against this same dict above, so a fallback string here could only
        # ever hide a later split of "supported" from "described" (#117).
        + INDICATOR_DESCRIPTIONS[indicator]
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
    ticker_obj = yf.Ticker(canonical)
    # Un-hidden: the quote scraper swallows a non-429 HTTP failure into a
    # None its own parser then trips over, which the router would render
    # as "Error retrieving fundamentals ..." report text (#116). The
    # stub dict is what an unknown symbol's 404 answered before, and the
    # "no fields" check below is what turns it into no-data.
    info = yf_fetch_unhidden(lambda: ticker_obj.info, hidden_answer=dict)

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

    # Every value above is read straight out of yfinance ``info``, a vendor
    # document of free-form JSON, and the report it lands in is served to the
    # agent verbatim. Name, Sector and Industry are prose by nature, so those
    # were the live forgery sites (#233) — but the flattening is applied to
    # the WHOLE list rather than to those three: a number comes through
    # ``str`` byte for byte here, so covering the numeric fields costs nothing
    # and leaves no field whose safety rests on the vendor sending the type we
    # expect. Each line starts with this module's own label, so the vendor's
    # share cannot open the line even before flattening; what flattening
    # closes is the line BREAK inside a value, which would otherwise start a
    # line the vendor writes in full.
    # The emptiness test is on the RENDERED value, not the raw one. A field of
    # pure markdown ("***") is a real, non-empty value that flattens to
    # nothing, so a raw test let it past and printed a label with nothing after
    # it — the same line the ``is not None`` test above exists to prevent, and
    # the same raw-versus-rendered mismatch ``polymarket._rendered_text``
    # closes for that report's fields (#233).
    lines = []
    for label, value in fields:
        if value is None:
            continue
        shown = sanitize_untrusted(value, limit=MAX_UNTRUSTED_CHARS)
        if shown:
            lines.append(f"{label}: {shown}")

    # yfinance returns a stub dict (e.g. {"trailingPegRatio": None}) for
    # unknown symbols, so `info` is truthy but every field is empty. Treat
    # "no usable fields" as no data rather than emitting a bare header the
    # agent might fabricate around.
    if not lines:
        raise NoMarketDataError(ticker, canonical, "no fundamental fields returned")

    # Refused at the same depth as the Alpha Vantage overview path, whose
    # docstring gives the reasoning: with no usable analysis date neither
    # vendor can tell a backtest from live trading (#89).
    if (
        refusal := date_refusal(
            curr_date, what="fundamentals", kind="disclosure", omitted_ok=True
        )
    ) is not None:
        return refusal

    header = f"# Company Fundamentals for {echo_argument(canonical)}\n"
    # yfinance ``info`` is a live current-state snapshot with no
    # historical form; when the analysis date sits behind the wall clock
    # (a backtest), say so or today's ratios read as that date's (#30).
    if curr_date is not None:
        snapshot_note = live_snapshot_note(curr_date, "these fundamentals are")
        if snapshot_note:
            header += f"# {snapshot_note}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + "\n".join(lines)


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get balance sheet data from yfinance."""
    canonical = normalize_symbol(ticker)
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


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get cash flow data from yfinance."""
    canonical = normalize_symbol(ticker)
    ticker_obj = yf.Ticker(canonical)

    # See get_balance_sheet for why these go through yf_fetch_statement.
    if freq.lower() == "quarterly":
        data = yf_fetch_statement(lambda: ticker_obj.quarterly_cashflow)
    else:
        data = yf_fetch_statement(lambda: ticker_obj.cashflow)

    return _statement_report(data, ticker, canonical, curr_date, freq, "cash flow", "Cash Flow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str | None, "current date in YYYY-MM-DD format"] = None,
):
    """Get income statement data from yfinance."""
    canonical = normalize_symbol(ticker)
    ticker_obj = yf.Ticker(canonical)

    # See get_balance_sheet for why these go through yf_fetch_statement.
    if freq.lower() == "quarterly":
        data = yf_fetch_statement(lambda: ticker_obj.quarterly_income_stmt)
    else:
        data = yf_fetch_statement(lambda: ticker_obj.income_stmt)

    return _statement_report(
        data, ticker, canonical, curr_date, freq, "income statement", "Income Statement"
    )


def get_insider_transactions(ticker: Annotated[str, "ticker symbol of the company"]):
    """Get insider transactions data from yfinance."""
    canonical = normalize_symbol(ticker)
    ticker_obj = yf.Ticker(canonical)
    # Un-hidden: the holders scraper swallows a non-429 HTTP failure into
    # an empty frame, which the "no filings" sentence below would then
    # claim as coverage (#116).
    data = yf_fetch_unhidden(
        lambda: ticker_obj.insider_transactions, hidden_answer=pd.DataFrame
    )

    # Empty is normal here (many valid symbols have no insider filings),
    # so report it plainly rather than treating the symbol as invalid.
    if data is None or data.empty:
        # The shared definition, so this sentence and the Alpha Vantage
        # sibling's cannot drift in wording or in which guard the symbol
        # takes (#219, #233). This vendor names the canonical spelling — the
        # one it actually queried.
        return no_insider_transactions(canonical)

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
    header = f"# Insider Transactions data for {echo_argument(canonical)}\n"
    header += lag_line
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string
