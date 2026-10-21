from __future__ import annotations

import math
import re
from datetime import date
from typing import TYPE_CHECKING, Annotated

# Upstream's file: the ticker guard and ``get_current_date``. The fork's
# additions moved to ``dates``, ``untrusted``, ``lag_notes`` and ``failures``
# on 2026-10-21 (dataflows refactor plan 2026-10-16, D4) and are re-exported
# here, the same object, because the upstream modules and the agents layer
# import from this path and are not edited. What is still defined below fits
# none of the four.
from .dates import (
    DATE_ARGUMENT_TAGS,
    DateKind,
    date_range_refusal,
    date_refusal,
    date_sentinel_note,
    invalid_date_sentinel,
    normalize_iso_date,
    refuse_date,
)
from .failures import (
    failure_account,
    generic_failure_words,
    http_status,
    is_unreached,
    is_vendor_outage,
    json_body_or_outage,
    json_bytes_or_outage,
    raise_for_http_status,
    wiring_gap,
)
from .lag_notes import (
    MAX_INSIDER_LAG_DAYS,
    MAX_LIVE_SNAPSHOT_BEHIND_DAYS,
    MAX_OHLCV_STALE_DAYS,
    MAX_STATEMENT_LAG_DAYS,
    data_lag_note,
    live_snapshot_note,
    parse_day,
    parse_day as _parse_day,  # noqa: F401 - upstream's alpha_vantage_common reads it so
    plural_days,
    statement_lag_bound,
)
from .untrusted import (
    EMPHASIS_UNDERSCORE,
    MARKDOWN_CONTROL,
    MAX_UNTRUSTED_CHARS,
    echo_argument,
    quote_argument,
    sanitize_untrusted,
)

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    # defined here
    "INDICATOR_DESCRIPTIONS",
    "SavePathType",
    "finite_float",
    "get_current_date",
    "is_finite_number",
    "no_insider_transactions",
    "no_news_in_window",
    "resolved_clause",
    "safe_ticker_component",
    "save_output",
    "unsupported_indicator",
    # dates
    "DATE_ARGUMENT_TAGS",
    "DateKind",
    "date_range_refusal",
    "date_refusal",
    "date_sentinel_note",
    "invalid_date_sentinel",
    "normalize_iso_date",
    "refuse_date",
    # failures
    "failure_account",
    "generic_failure_words",
    "http_status",
    "is_unreached",
    "is_vendor_outage",
    "json_body_or_outage",
    "json_bytes_or_outage",
    "raise_for_http_status",
    "wiring_gap",
    # lag_notes
    "MAX_INSIDER_LAG_DAYS",
    "MAX_LIVE_SNAPSHOT_BEHIND_DAYS",
    "MAX_OHLCV_STALE_DAYS",
    "MAX_STATEMENT_LAG_DAYS",
    "data_lag_note",
    "live_snapshot_note",
    "parse_day",
    "plural_days",
    "statement_lag_bound",
    # untrusted
    "EMPHASIS_UNDERSCORE",
    "MARKDOWN_CONTROL",
    "MAX_UNTRUSTED_CHARS",
    "echo_argument",
    "quote_argument",
    "sanitize_untrusted",
]


SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(f"ticker contains characters not allowed in a filesystem path: {value!r}")
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


# The description each indicator report ends with — agent-facing prompt text,
# so the sentence an analyst reads must not depend on which vendor
# ``data_vendors`` selected. One table for every reader (#137, #187): the
# yfinance getter serves it whole, the Alpha Vantage vendor derives its
# registry from the slice it supports, and the market analyst's selection
# menu is rendered from it by ``agents.utils.indicator_menu`` — so editing a
# description edits both report lanes AND the prompt the analyst chooses
# from. The section comments below are for the reader only: the menu's
# grouping and order are ``INDICATOR_MENU``'s, and an indicator added here
# has to be placed there (or in ``INDICATOR_MENU_OMITS``) to pass that
# module's import-time partition check. The grouping lives up in the agent
# layer because it is a fact about one prompt, not about the data (#219);
# the sentences stay here (stdlib-only) rather than beside either vendor so the
# pure-requests Alpha Vantage module does not import the yfinance stack for
# a dict of strings (#70).
INDICATOR_DESCRIPTIONS = {
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

def finite_float(value: object, *, allow_str: bool = False) -> float | None:
    """A figure about to be used in ARITHMETIC, as a finite float, or None.

    The bool-rejecting question four vendor modules each used to ask with
    their own copy. It is not :func:`is_finite_number` below, which asks
    whether a vendor's raw report CELL is a number before it is rendered and
    deliberately lets ``True`` through as 1.0; here the value is about to be
    summed, compared or weighted, so a JSON ``true`` passing as 1 would be a
    figure nobody sent.

    Returns the value rather than a verdict, because half the callers need it:
    a predicate forces every one of them to convert again, and a second
    conversion is a second place for the rule to differ.

    ``NaN``/``Infinity`` are refused along with the unparseable: ``json``
    decodes both, every comparison with NaN is False (which silently defeats a
    cross-check), and either renders as a literal "nan"/"inf" figure.

    ``math.isfinite`` RAISES on an int too large to convert to a float, and a
    JSON integer literal has no bound, so ``json.loads`` can hand back an
    arbitrary-precision int that makes a naive predicate throw rather than
    answer - inverting its contract at one of the input classes it exists to
    turn away. Two of the four copies caught that; the Farside one did not,
    which is the kind of divergence sharing this removes.

    ``allow_str`` is opt-in because it is a property of the VENDOR, not of the
    question: Hyperliquid sends every number as a string, so its figures
    arrive as ``"-2110.66337"``, while a string reaching the others means the
    payload is not the shape they parsed. Off by default so adopting this
    cannot silently widen what a caller accepts.
    """
    if isinstance(value, str):
        if not allow_str:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def is_finite_number(value) -> bool:
    """Whether a vendor's raw report cell IS a number, before any flattening.

    Asked of the RAW spelling and of the RENDERED one by every getter that puts
    a vendor's number into text the model reads: flattening turns "4.1|" into
    "4.1", so a cell checked only after :func:`sanitize_untrusted` is a figure
    the vendor never sent, standing where a conspicuous failure used to be
    (#233). ``nan`` and ``inf`` are refused with the unparseable ones —
    ``float()`` accepts both, and either poisons any delta computed from the
    series.

    One definition because ``fred`` and the Alpha Vantage indicator getter ask
    the same question about the same kind of value. :func:`finite_float` above
    asks the DIFFERENT one the vendor modules ask - it rejects ``bool``,
    because there the value is about to be arithmetic rather than rendered -
    and every one of them now reads it rather than keeping a copy.
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        # ``OverflowError`` is not hypothetical: ``json.loads`` keeps arbitrary
        # precision, so a 400-digit integer literal raises here rather than in
        # ``float()``'s usual lanes, and this guard exists precisely because
        # the payload is not to be trusted.
        return False


# The sentences the vendors of one routed tool must agree on, defined once
# each. Each used to be a literal in the yfinance module and a second literal
# in the Alpha Vantage one, each under a comment promising the other copy said
# the same thing — and the promise was pinned only by a cross-vendor equality
# test spelling one canonical symbol, so the two could diverge the moment a
# value stopped being canonical. Same reasoning as ``dates._OMIT_CLAUSE``: one
# literal, so one vendor cannot start saying something the other stopped
# saying (#140 review, #219, #233).
#
# What belongs here is a sentence carrying a value the two vendors have to
# guard the SAME WAY. "No global news found between {start} and {curr}" is a
# twin of the same shape and is deliberately NOT here: every value in it is a
# date the caller's refusal already vetted, so there is no guard for the two
# to disagree about and moving it would only add a hop.
#
# The ECHO happens in here rather than at the call sites, which is the half
# that matters most: the two vendors have to agree on WHICH guard the value
# takes, not merely on the words around it. A caller reaching for
# ``echo_argument`` where its sibling reached for ``quote_argument`` would
# leave the twins identical on a clean spelling and different on a hostile
# one — exactly the divergence a cross-vendor equality test on a clean symbol
# cannot see.


def no_insider_transactions(symbol: object) -> str:
    """The empty-insider-stream answer, in the voice both vendors share.

    An empty stream is normal here — many valid symbols have no filings — so
    both vendors say so in prose rather than one answering raw empty JSON the
    agent might hedge over (#90). Each names the spelling IT queried: yfinance
    the canonical symbol it resolved to, Alpha Vantage the raw one it sent,
    because echoing a spelling a vendor never used would misattribute the
    emptiness. The sentence quotes the symbol, so the echo is
    ``quote_argument`` (see there for why the quotes are repr's and not the
    f-string's).
    """
    return f"No insider transactions reported for symbol {quote_argument(symbol)}"


def resolved_clause(raw: object, canonical: object) -> str:
    """The " (resolved to X)" aside, or nothing when the alias table changed nothing.

    One definition because the clause is appended to sentences in more than one
    module and the ECHO has to be the same at each: a copy reaching for
    :func:`sanitize_untrusted` instead would strip an edge marker and quote a
    spelling back that the caller never wrote.

    The comparison stays on the RAW pair. It asks whether the alias table
    changed the symbol, which is not a question the flattening may answer: two
    spellings differing only in characters the flattening collapses are still
    two spellings, and hiding the aside would tell the model its argument was
    served as written when it was not.

    Bare, not quoted — the callers name the symbol in running prose. A quoted
    sentence wants its own clause built on :func:`quote_argument`, as the
    router's sentinel does.
    """
    return "" if canonical == raw else f" (resolved to {echo_argument(canonical)})"


def no_news_in_window(
    ticker: object, start_date: str, end_date: str, *, canonical: object = None
) -> str:
    """The nothing-in-the-window answer, in the voice both news vendors share.

    Alpha Vantage filters ``NEWS_SENTIMENT`` server-side by ``time_from`` /
    ``time_to``, so its empty feed asserts only "nothing in the window you
    asked for" — the same claim the yfinance getter makes when articles exist
    but none fall inside the window. The two reach the sentence from different
    sides on purpose; what has to match is the sentence the agent reads, since
    which vendor served the call is not something the agent can see.

    ``canonical`` is the spelling the vendor actually queried, RAW: this
    function builds the resolution aside and does the echoing itself, so a
    vendor cannot hand over a clause built with the wrong guard — the sibling
    below takes its symbol raw for the same reason, and a helper that guards
    one argument while trusting another is a contract only its docstring
    states. A vendor that queries the spelling it was handed passes nothing.

    The dates are the caller's too, and reach here having already been refused
    if they were not usable (#111), so they are not echoed.
    """
    aside = resolved_clause(ticker, ticker if canonical is None else canonical)
    return f"No news found for {echo_argument(ticker)}{aside} between {start_date} and {end_date}"


def unsupported_indicator(indicator: object, supported) -> str:
    """The unknown-indicator refusal, in the voice both indicator vendors share.

    #117 renders this message as one line of report text, so the rejected name
    is the caller's own argument re-entering text the model reads and takes
    :func:`echo_argument` — bare, in running prose.

    One definition for the same reason the two sentences above have one: the
    yfinance and Alpha Vantage indicator getters are two vendors of ONE routed
    tool, so guarding the echo on one side alone would leave them ending alike
    on a clean spelling and differently on a hostile one (#219). ``supported``
    is each vendor's OWN menu — the two genuinely cover different indicators,
    and that difference is the vendor's to state; which guard the caller's
    value takes is not.
    """
    return (
        f"Indicator {echo_argument(indicator)} is not supported. "
        f"Please choose from: {list(supported)}"
    )


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")
