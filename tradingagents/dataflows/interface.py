import logging
import time
from typing import NamedTuple

from .alpha_vantage import (
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_global_news as get_alpha_vantage_global_news,
    get_income_statement as get_alpha_vantage_income_statement,
    get_indicator as get_alpha_vantage_indicator,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_stock as get_alpha_vantage_stock,
)
from .config import get_config
from .deribit import get_options_market_data as get_deribit_options_market
from .errors import (
    NoMarketDataError,
    UnsupportedIndicatorError,
    VendorError,
    VendorLibraryError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)
from .farside import get_etf_flow_data as get_farside_etf_flows
from .fear_greed import get_fear_greed_data as get_alternative_me_fear_greed
from .fred import get_macro_data as get_fred_macro_data
from .polymarket import get_prediction_markets as get_polymarket_prediction_markets
from .sosovalue import get_etf_flow_data as get_sosovalue_etf_flows
from .sosovalue_macro import get_economic_calendar_data as get_sosovalue_economic_calendar
from .sosovalue_treasuries import get_btc_treasury_data as get_sosovalue_btc_treasuries
from .throttle import VENDOR_THROTTLE_LATCH
from .utils import (
    MAX_UNTRUSTED_CHARS,
    failure_account,
    generic_failure_words,
    is_vendor_outage,
    quote_argument,
    sanitize_untrusted,
)
from .y_finance import (
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_fundamentals as get_yfinance_fundamentals,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
    get_stock_stats_indicators_window,
    get_YFin_data_online,
)
from .yfinance_news import get_global_news_yfinance, get_news_yfinance

logger = logging.getLogger(__name__)

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {"description": "OHLCV stock price data", "tools": ["get_stock_data"]},
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": ["get_indicators"],
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": ["get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"],
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ],
    },
    "macro_data": {
        "description": "Macroeconomic indicators (rates, inflation, labor, growth)",
        "tools": [
            "get_macro_indicators",
        ],
    },
    "prediction_markets": {
        "description": "Market-implied probabilities for forward-looking events",
        "tools": [
            "get_prediction_markets",
        ],
    },
    "crypto_etf_flows": {
        "description": "BTC/ETH US spot-ETF daily net flows (crypto)",
        "tools": [
            "get_etf_flows",
        ],
    },
    "crypto_sentiment": {
        "description": "Crypto Fear & Greed Index sentiment gauge",
        "tools": [
            "get_fear_greed",
        ],
    },
    "options_data": {
        "description": "Crypto options implied volatility: DVOL index and 25-delta skew",
        "tools": [
            "get_options_market",
        ],
    },
    "economic_calendar": {
        "description": "US macro economic calendar: scheduled events and releases vs forecast",
        "tools": [
            "get_economic_calendar",
        ],
    },
    "btc_treasuries": {
        "description": "Corporate BTC treasury holdings and disclosed changes (crypto)",
        "tools": [
            "get_btc_treasuries",
        ],
    },
}

# Configuring a category (or tool) to this sentinel switches it off entirely.
# Keyless vendors have no equivalent of FRED's "unset the API key" escape hatch,
# so "none" is the mechanism for stopping a misbehaving vendor without deleting
# its wiring.
#
# Reachability caveat: today this is settable only through the Python config
# (DEFAULT_CONFIG / set_config). The Hyperliquid perp deployment builds its
# engine config from a fixed key list and does not yet pipe ``data_vendors`` /
# ``tool_vendors`` through (nor is there an env override), so on that long-running
# box flipping a vendor to "none" still needs a code change + redeploy. Wiring
# data_vendors into the perp engine config (or an env override) is a follow-up;
# see the vendor-hygiene notes.
DISABLED_VENDOR = "none"

VENDOR_LIST = [
    "yfinance",
    "fred",
    "polymarket",
    "alpha_vantage",
    "sosovalue",
    "farside",
    "alternative_me",
    "deribit",
]

# Optional enrichment categories. These add macro/event/positioning context to
# the analysts but are not core to a decision, so a vendor failure here degrades
# to a sentinel instead of aborting the run (a bad LLM-supplied indicator, a
# missing key, or a network blip should not crash an analysis over flavour data).
# Core categories (prices, fundamentals, news) still raise so a broken primary is
# loud.
OPTIONAL_CATEGORIES = {
    "macro_data",
    "prediction_markets",
    "crypto_etf_flows",
    "crypto_sentiment",
    "options_data",
    "economic_calendar",
    "btc_treasuries",
}

# Categories whose vendor's own library failing is never rendered as the
# library-failure report line. Everywhere else an untyped failure below a
# vendor is read as its
# library's and reported as one line of text, so a stockstats bug costs one
# indicator rather than the run (#187). OHLCV is the exception: it is the
# market analyst's primary input and the frame every other price claim is
# checked against, so a report line where the prices should be would leave
# the run reasoning from nothing while looking answered. Which categories are
# loud used to be decided by which nine getters happened to carry a broad
# handler — the OHLCV getter's silence on the matter was inherited, not
# chosen, and its Alpha Vantage sibling aborted where the yfinance one
# reported (#219). Declared here so it is one auditable line, and mirrored by
# the test that holds each yfinance leaf to its ending.
#
# The other three core categories keep the report line, and that is a choice
# now rather than an inheritance: fundamentals and news reach the analyst as
# prose either way, so a line saying the vendor's parser broke reads as the
# absence it is, where a report line standing in for prices does not. Making
# them loud would also let one vendor's parser bug abort a paper cycle the
# sibling vendor could have served.
#
# "Never rendered as that line" rather than "always raises": a chain where
# another vendor reported clean no-data ends in the no-data sentinel, which
# is ranked ahead of the raise and was this ending before the declaration
# existed.
LOUD_LIBRARY_CATEGORIES = frozenset({"core_stock_apis"})

# A loud category must not also be optional. The optional ending is a
# sentinel, which outranks the raise below, so the two declarations would
# contradict each other silently: no exception, no log line, and no test
# failure for as long as the one loud category happens to be core. The
# behaviour itself is pinned over every member of the declaration, so this
# is the early word rather than the only guard — it fails at import, before
# a cycle runs, and it is stripped under ``python -O``.
assert LOUD_LIBRARY_CATEGORIES.isdisjoint(OPTIONAL_CATEGORIES), (
    f"categories cannot be both loud and optional: "
    f"{sorted(LOUD_LIBRARY_CATEGORIES & OPTIONAL_CATEGORIES)}"
)

# How a vendor's library failing is logged, shared by the two lanes that meet
# one: the boundary that raised the type itself and the conversion of an
# untyped failure. The same event either way, so the same sentence — the
# vendor, the subject, the tool and the whole uncapped message.
_LIBRARY_FAILURE_LOG = "Vendor %r failed in its own library retrieving %s for %s: %s"

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
    # technical_indicators
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
    # fundamental_data
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
    },
    # news_data
    "get_news": {
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
    "get_global_news": {
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # macro_data
    "get_macro_indicators": {
        "fred": get_fred_macro_data,
    },
    # prediction_markets
    "get_prediction_markets": {
        "polymarket": get_polymarket_prediction_markets,
    },
    # crypto_etf_flows
    "get_etf_flows": {
        "sosovalue": get_sosovalue_etf_flows,
        "farside": get_farside_etf_flows,
    },
    # crypto_sentiment
    "get_fear_greed": {
        "alternative_me": get_alternative_me_fear_greed,
    },
    # options_data
    "get_options_market": {
        "deribit": get_deribit_options_market,
    },
    # economic_calendar
    "get_economic_calendar": {
        "sosovalue": get_sosovalue_economic_calendar,
    },
    # btc_treasuries
    "get_btc_treasuries": {
        "sosovalue": get_sosovalue_btc_treasuries,
    },
}


# What a routed tool's library failure names, per method, read off the call's
# own arguments — "rsi values for AAPL", not "get_indicators failed". The
# subject used to be written at each getter that wrapped its fetch, which is
# why two vendors serving one routed tool had to be held to naming it alike
# by a test (#187); with the conversion here there is one row per tool and
# the vendor cannot enter into it. Every registered method has a row (a test
# derives the requirement from VENDOR_METHODS, so a new tool cannot ship
# without one), including the loud categories that never reach the
# conversion — a category's loudness is a decision that can be revisited,
# and the subject should not have to be invented on the day it is.
#
# Templates rather than code, and positional: every caller of route_to_vendor
# passes positionally, the parameter names differ between a tool's two vendors
# (symbol / ticker), and ``str.format`` ignores the surplus arguments — the
# trailing look_back_days and limit are optional at several tools and name
# nothing.
_LIBRARY_SUBJECTS = {
    "get_stock_data": "stock price data for {0}",
    "get_indicators": "{1} values for {0}",
    "get_fundamentals": "fundamentals for {0}",
    "get_balance_sheet": "balance sheet for {0}",
    "get_cashflow": "cash flow for {0}",
    "get_income_statement": "income statement for {0}",
    "get_news": "news for {0}",
    "get_global_news": "global news",
    "get_insider_transactions": "insider transactions for {0}",
    "get_macro_indicators": "macro series {0}",
    "get_prediction_markets": "prediction markets for {0}",
    "get_etf_flows": "ETF flows for {0}",
    "get_fear_greed": "fear and greed index",
    "get_options_market": "options market for {0}",
    "get_economic_calendar": "economic calendar",
    "get_btc_treasuries": "BTC treasuries for {0}",
}


def _library_subject(method: str, args: tuple) -> str:
    """The subject a library failure at ``method`` names, or the method itself.

    The fallback is the point: this runs while a failure is being classified,
    so a missing row or a call shape the row did not expect must not replace
    the vendor's failure with an ``IndexError`` from the naming of it. The
    method name is a poorer subject, not a wrong one, and the registry-derived
    test is what keeps the fallback from being reached in practice.
    """
    try:
        return _LIBRARY_SUBJECTS[method].format(*args)
    except Exception:  # noqa: BLE001 - naming a failure must not raise over it
        logger.warning("No usable library-failure subject for %s; naming the method", method)
        return method


def _as_library_failure(
    category: str, method: str, args: tuple, e: Exception
) -> VendorLibraryError | None:
    """The vendor's library failing, if that is what this untyped failure is.

    Read after the outage question, so what is left is a failure the vendor
    did not answer with and the wire did not cause. Four things are not the
    vendor's library and return None:

    * a loud category's failure (``LOUD_LIBRARY_CATEGORIES``) — it keeps the
      ending it has always had, a raise;
    * a ``VendorError`` — the taxonomy said what it was at the boundary. The
      lanes above name the five types the router reacts to individually, and
      a vendor-named subclass raised directly (``DeribitError``,
      ``SoSoValueError``) reaches this one instead — but it is still the
      vendor's own verdict, not its library tripping, and rewriting it here
      would break the promise that a thin subclass needs no new ``except``
      clause (#170 declined a bare ``VendorError`` lane for the same reason:
      a lane is a reaction, and this type has none of its own);
    * an ``OSError`` — a transport failure is not a report (#116): yfinance
      fetches through curl_cffi, and its request exceptions, like
      ``requests``', subclass it, while nothing in yfinance's own exception
      family does; the clause is wider than the wire on purpose, since the
      OHLCV cache raises ``OSError`` too and a cache the process cannot read
      is no more a report than a reset is;
    * a ``WiringGapError`` — this project's own breakage, raised by the
      prologue guards that used to sit above each getter's ``with`` block
      (#111, #200).

    Everything else is the library: a stockstats or pandas bug on a frame
    the vendor did serve, a parser tripping over a shape a scraper let
    through. The cause is attached rather than wrapped by a ``raise``,
    since the router is classifying the failure here, not raising it.
    """
    if category in LOUD_LIBRARY_CATEGORIES or isinstance(
        e, (VendorError, OSError, WiringGapError)
    ):
        return None
    failure = VendorLibraryError(_library_subject(method, args), str(e))
    failure.__cause__ = e
    return failure


def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")


def is_category_disabled(category: str, method: str = None) -> bool:
    """True when a category (or tool) is configured to the "none" sentinel.

    Lets a caller skip binding a tool altogether rather than binding one that
    could only ever return the disabled sentinel.
    """
    return any(
        v.strip().lower() == DISABLED_VENDOR for v in get_vendor(category, method).split(",")
    )


def _library_failure_prose(e: VendorLibraryError) -> str:
    """The one line a core chain ended by a vendor library's failure reports as.

    The words are ``failure_account``'s — the getter's subject and the
    library's message each flattened and capped on its own — so this slot
    and the optional category's sentinel render the type by one rule.
    """
    return f"Error retrieving {failure_account(e)}"


class _VendorFailure(NamedTuple):
    """A failure the chain met, with the vendor that met it.

    Every slot ``route_to_vendor`` keeps a failure in holds one of these, so
    the optional category's sentinel can name the vendor ahead of the words
    (#203) whichever slot the failure surfaces from.
    """

    vendor: str
    error: Exception


# The ranks of the ways a vendor never confirmed the symbol, lowest first: an
# outage outranks a throttle actually met, which outranks a latch skip,
# whatever the chain order (#142, #172). ``_note_unconfirmed`` keeps the lowest
# rank met, so the no-data verdict reads one slot instead of three in a
# hand-written order (#217).
_OUTAGE, _THROTTLE_MET, _LATCH_SKIP = range(3)
# Why the vendor's verdict is missing, by rank: the no-data sentinel's second
# clause, a function of the rank alone.
_WHY = {
    _OUTAGE: "was unavailable",
    _THROTTLE_MET: "was rate limited before it could answer",
    _LATCH_SKIP: "was not asked",
}


class _Unconfirmed(NamedTuple):
    """The vendor that never confirmed the symbol, by rank, and the words for it."""

    rank: int
    vendor: str
    # What happened, in the vendor's own words: the clause the no-data
    # sentinel swaps in ahead of ``_WHY[rank]``.
    state: str
    # The failure met. A throttle or a skip stands as the failure surfaced
    # when nothing else raised; an outage's is in ``first_error`` already
    # (``_met_outage`` records both), which precedes this slot there, so it
    # never surfaces from here.
    error: Exception


def _met_outage(
    first_error: _VendorFailure | None,
    unconfirmed: _Unconfirmed | None,
    vendor: str,
    error: Exception,
    words: str,
) -> tuple[_VendorFailure, _Unconfirmed]:
    """Both facts an outage lane records, together: the failure and the unconfirmed verdict.

    The failure joins ``first_error`` (first met stays), and the outage rank
    joins the slot with what the down vendor said, in the lane's words. One
    helper for both, so the guarantee the verdict relies on — an outage held
    in the slot has its failure in ``first_error`` already, and so never
    surfaces from the slot — cannot be broken by a lane that records one
    fact and forgets the other.
    """
    outage = _Unconfirmed(_OUTAGE, vendor, f"was unavailable ({words})", error)
    return first_error or _VendorFailure(vendor, error), _note_unconfirmed(unconfirmed, outage)


def _met_library_failure(
    first_library: _VendorFailure | None,
    first_error: _VendorFailure | None,
    category: str,
    vendor: str,
    error: VendorLibraryError,
) -> tuple[_VendorFailure | None, _VendorFailure | None]:
    """Both slots a vendor library's failure fills, together.

    ``first_library`` decides a core chain's ending, which must not depend on
    the chain order, so a loud category never fills it — the ending that
    declaration refuses is the report line, whoever named the failure
    (``_as_library_failure`` refuses the untyped half; this refuses a
    boundary that raised the type itself). ``first_error`` is the vendors'
    own slot, where first met wins: an optional category ends in one sentinel
    either way, and there a missing key ahead of a library bug is the
    standing misconfiguration the operator has to fix.

    One helper for both, as ``_met_outage`` is for the outage lane's pair, so
    a lane that records one fact cannot forget the other.
    """
    failed = _VendorFailure(vendor, error)
    if first_library is None and category not in LOUD_LIBRARY_CATEGORIES:
        first_library = failed
    return first_library, first_error or failed


def _note_unconfirmed(held: _Unconfirmed | None, met: _Unconfirmed) -> _Unconfirmed:
    """``met`` if it outranks what is ``held`` (a lower rank), else ``held``.

    The first of a rank stays, so among two outages the one met first is
    the one named, as it was when each rank had a slot of its own.
    """
    return met if held is None or met.rank < held.rank else held


def _optional_failure_words(e: Exception) -> str:
    """The words an optional category's sentinel quotes for the failure that surfaced.

    ``failure_account``'s, but for a ``VendorNotConfiguredError``: its
    message is the operator's remedy — the variable to set, a URL to get a
    key at — and quoting it wrote that URL into every cycle's report
    artifacts of a deployment without the key (#203). The model has no
    action to take on it, so it reads one fixed phrase, the way a category
    switched off reads "disabled by configuration"; the not-configured lane
    and the verdict's warning log the message whole for the operator.
    """
    if isinstance(e, VendorNotConfiguredError):
        return "vendor not configured"
    return failure_account(e)


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(",")]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())

    # The configured vendor list IS the chain: we do NOT silently fall back to
    # vendors the user did not choose (#988/#289) — that returned data from an
    # unexpected source and caused cross-vendor inconsistencies. For multi-vendor
    # fallback, list them in order, e.g. data_vendors="yfinance,alpha_vantage".
    # The "default" sentinel (no explicit config) uses all available vendors.
    explicit = [v for v in primary_vendors if v and v != "default"]
    # An explicit "none" switches the category off. Checked before the vendor
    # chain is resolved so a disabled category never opens a connection, and
    # handled here rather than via the loop's error paths so "deliberately off"
    # is never logged as a vendor failure.
    if any(v.lower() == DISABLED_VENDOR for v in explicit):
        if category in OPTIONAL_CATEGORIES:
            logger.info("Optional %s is disabled by configuration; skipping %s", category, method)
            return (
                f"DATA_UNAVAILABLE: optional {category} is disabled by configuration. "
                f"Proceed without it; do not fabricate values."
            )
        raise ValueError(
            f"Category '{category}' supplies core data for '{method}' and cannot be "
            f"disabled with '{DISABLED_VENDOR}'."
        )
    if explicit:
        vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]
        if not vendor_chain:
            raise ValueError(
                f"Configured vendor(s) {explicit} not available for '{method}'. "
                f"Available: {all_available_vendors}."
            )
        # A mis-typed name in a comma chain must not silently shrink it — the
        # all-unknown raise above cannot fire once any sibling survives.
        unknown = [v for v in explicit if v not in VENDOR_METHODS[method]]
        if unknown:
            logger.warning(
                "Configured vendor(s) %s not available for '%s'; using %s. Available: %s.",
                unknown,
                method,
                vendor_chain,
                all_available_vendors,
            )
    else:
        vendor_chain = all_available_vendors

    last_no_data: NoMarketDataError | None = None
    # Every failure kept below is kept with the vendor that met it: the
    # optional category's sentinel names the vendor ahead of the words
    # (#203), so a multi-vendor category's reader can tell which source
    # failed without the log, whichever slot the failure surfaces from.
    first_error: _VendorFailure | None = None
    # A caller's mistake (an indicator name no vendor computes) is kept apart
    # from the vendors' failures so it outranks them at the verdict (#137).
    first_caller_error: _VendorFailure | None = None
    # The first vendor that never confirmed the symbol — DOWN (answered with
    # an outage page or could not be reached), throttled (a 429 actually
    # met), or skipped on a latch without a request — and the words for it:
    # a fallback's "no data" is then unconfirmed by the source that would
    # normally serve the symbol, and the sentinel has to say so (#142,
    # #172). One ranked slot (#217): an outage outranks a throttle met,
    # which outranks a skip, whatever the chain order, so a lower rank
    # replaces what is held and the verdict reads one slot. A missing key
    # stays out — that is standing configuration the operator already sees
    # in the log, not a source that would normally have answered. Text
    # only, never the exception, where it travels into a sentinel the model
    # reads; the throttle and the skip keep theirs, since either stands as
    # the failure surfaced when nothing else raised.
    unconfirmed: _Unconfirmed | None = None
    # The first vendor library failure met (#187): kept apart from
    # ``first_error`` because it decides a different ending — one line of
    # report text rather than a raise — and must do so whatever its place in
    # the chain (a missing key met before it must not surface instead and
    # abort the call).
    first_library: _VendorFailure | None = None
    # The first wiring gap met, kept apart for the mirror-image reason: it is
    # this project's own breakage, so it must not be the thing a vendor's
    # library failure stands in front of. Before the conversion moved to the
    # router only nine getters could fill ``first_library``, so which chains
    # buried a wiring gap behind a report line depended on which vendor was
    # one of the nine; now every vendor can fill it, and a missing key met at
    # the first vendor would come back as the second vendor's parser bug for
    # the analyst to read as an answer (#219).
    first_wiring: _VendorFailure | None = None
    for vendor in vendor_chain:
        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        # A vendor that refused this client with a rate limit recently is
        # skipped in its turn without a request (#114) — this lane is the one
        # point every vendor's throttle passes through, so the memory lives
        # here once, keyed by vendor because a quota is spent per key, not per
        # endpoint. The chain goes on in its configured order, and the skip is
        # recorded as a fallback verdict, so a chain with nothing else to say
        # degrades the way it would have after contacting the vendor. A vendor
        # that stands off behind its own cache (yfinance) or answers a throttle
        # from cache (SoSoValue) is never latched here: its rate-limit type
        # says so (``latches_vendor``), because skipping it would refuse an
        # answer it had.
        remaining = VENDOR_THROTTLE_LATCH.remaining_s(vendor)
        if remaining is not None:
            logger.info(
                "Vendor %r rate limited a recent request; skipping it for %s without "
                "contacting it (%.0fs left).",
                vendor,
                method,
                remaining,
            )
            # The remaining stand-off is the one fact that tells a skip from
            # an outage to the model: a source back in a minute is not a
            # source that is down, and the two should weigh differently.
            unconfirmed = _note_unconfirmed(
                unconfirmed,
                _Unconfirmed(
                    _LATCH_SKIP,
                    vendor,
                    f"was skipped after a recent rate limit (for another {remaining:.0f}s)",
                    VendorRateLimitError(
                        f"Vendor {vendor!r} rate limited a recent request; skipped without "
                        f"contacting it for another {remaining:.0f}s"
                    ),
                ),
            )
            continue

        # The send instant the latch compares against (#153). Taken here, so
        # an impl that waits before sending would date its request early —
        # the ones that do (SoSoValue's request budget; yfinance's un-hide
        # lock and backoff ladder) raise types the router never latches, so
        # nothing is misdated today.
        sent_at = time.monotonic()
        try:
            result = impl_func(*args, **kwargs)
        except VendorRateLimitError as e:
            if e.latches_vendor:
                # For as long as the raise says its refusal lasts, else the
                # shared window: a spent daily quota is not over in five
                # minutes, and re-probing it on that window only adds
                # refused requests and log lines (#153).
                ttl_s = VENDOR_THROTTLE_LATCH.arm(vendor, e.latch_ttl_s)
                logger.warning(
                    "Vendor %r rate-limited for %s; trying next vendor, and skipping %r "
                    "without contacting it for the next %.0fs.",
                    vendor,
                    method,
                    vendor,
                    ttl_s,
                )
            else:
                logger.warning("Vendor %r rate-limited for %s; trying next vendor.", vendor, method)
            # The throttle carries the vendor's own words (a Retry-After)
            # where a skip describes a request that was never sent.
            unconfirmed = _note_unconfirmed(
                unconfirmed,
                _Unconfirmed(_THROTTLE_MET, vendor, f"was rate limited ({failure_account(e)})", e),
            )
            continue
        except VendorNotConfiguredError as e:
            # The message whole: it is the operator's remedy (the variable to
            # set, where to get a key), and the optional sentinel no longer
            # quotes it (#203) — this line and the verdict's are where the
            # operator reads it. Every cycle, for a deployment that runs
            # without an optional key on purpose; the boundary is trusted to
            # have redacted the key itself (SoSoValue's 401 scrubs it).
            logger.warning(
                "Vendor %r not configured for %s; trying next vendor: %s", vendor, method, e
            )
            if first_error is None:
                # Surface it if no other vendor can serve the call.
                first_error = _VendorFailure(vendor, e)
            continue
        except NoMarketDataError as e:
            # No data here; another configured vendor may have it. INFO, not
            # WARNING — a routine verdict — but logged whole: the detail is
            # capped in the sentinel and this line is its only other copy.
            logger.info("Vendor %r had no usable data for %s: %s", vendor, method, e)
            last_no_data = e
            continue
        except VendorUnavailableError as e:
            # The vendor answered with an outage page or an unparsable body
            # (#136): the chain goes on and it surfaces at the end like a
            # transport failure, but logged without a traceback — the clause
            # below reserves that for a bug, and a vendor being down is not one.
            logger.warning("Vendor %r answered without data for %s: %s", vendor, method, e)
            first_error, unconfirmed = _met_outage(
                first_error, unconfirmed, vendor, e, failure_account(e)
            )
            continue
        except VendorLibraryError as e:
            # The vendor's own library failed computing the answer — a
            # stockstats bug on a frame it did serve. The chain goes on: a
            # sibling vendor computes the same routed tool its own way
            # (Alpha Vantage has an RSI endpoint), and this used to be
            # rendered as prose at the leaf, which read here as a successful
            # answer and ended the chain at the vendor that had just failed
            # (#187). No production boundary raises the type today — the
            # conversion below is where every library failure now enters — so
            # this lane exists for a boundary that knows the failure is its
            # library's and says so. It therefore logs the whole thing itself,
            # at the same level and in the same words as the conversion —
            # one shared format string, so neither can drift into its own
            # wording — since there is no leaf lane left to have logged it
            # first (#219).
            logger.error(_LIBRARY_FAILURE_LOG, vendor, e.what, method, e.detail, exc_info=True)
            first_library, first_error = _met_library_failure(
                first_library, first_error, category, vendor, e
            )
            continue
        except UnsupportedIndicatorError as e:
            # A caller typo, not a vendor failure: logged without a traceback,
            # which the clause below reserves for a bug. The chain still goes
            # on — another vendor may compute the name (yfinance serves mfi;
            # Alpha Vantage has no endpoint for it) — and it surfaces at the
            # end ahead of any vendor failure, for the tool wrapper to render
            # as report text (#117): the name is the caller's to fix, and a
            # missing key surfacing instead would send them to the wrong
            # remedy (#137).
            logger.warning("Vendor %r does not support the indicator for %s: %s", vendor, method, e)
            if first_caller_error is None:
                first_caller_error = _VendorFailure(vendor, e)
            continue
        except Exception as e:
            # Don't let one vendor's failure crash the call when another can
            # serve it, but never swallow silently: a broken primary must be
            # visible in the logs (#989), not hidden behind a fallback's verdict.
            # exc_info so a real bug (e.g. in an HTML-scraping vendor) leaves a
            # traceback instead of looking identical to a network outage.
            # The same fact as the outage lane above, for the verdict below,
            # read off the exception by ``is_vendor_outage`` — the status it
            # carries, never its class (yfinance's HTTPError is curl_cffi's,
            # not requests'). The words are ``generic_failure_words``' — the
            # status or the class only, never the text: a requests message
            # quotes the request URL, API key included.
            #
            # What the vendor was not down for, and did not fail to reach, is
            # its library failing on data it did serve — the ending nine
            # getters used to choose for themselves by wrapping their fetch,
            # and their siblings at the same routed tool did not, so one
            # pandas bug was a line of prose through yfinance and an aborted
            # run through Alpha Vantage (#219). Decided here, once, for every
            # vendor of every tool: what ``_as_library_failure`` returns joins
            # the lane the typed raisers take, which is where the ending is
            # written. The message travels as the failure's ``detail`` — the
            # cap and the flatten are the report line's, and the log line each
            # branch below writes is the uncapped copy, with the traceback the
            # lane at the getter used to log. One line per failure, whichever
            # branch takes it, so the levels stay readable: ERROR where a
            # library failure is about to be reported as text, WARNING for
            # everything else. A loud category takes the WARNING too — the
            # conversion refuses it, so the failure reaches the caller as
            # itself, with its own traceback, which is louder than a log
            # line and is the whole point of the declaration.
            outage = is_vendor_outage(e)
            library = None if outage else _as_library_failure(category, method, args, e)
            if library is not None:
                # ERROR, where every other ending in this lane keeps the
                # WARNING, and naming the subject rather than the method:
                # this is the degrade the leaf lanes were added to make
                # visible (#187 — seven of them logged nothing, so an
                # operator never saw one happen), and it is the one ending
                # here that hands the analyst a line of text in place of the
                # data it asked for. A vendor being down or handing back a
                # verdict is routine; its library breaking on data it did
                # serve is a bug someone has to fix. Same words and level as
                # the typed lane above, which a boundary naming its own
                # library bug takes.
                logger.error(
                    _LIBRARY_FAILURE_LOG, vendor, library.what, method, e, exc_info=True
                )
                first_library, first_error = _met_library_failure(
                    first_library, first_error, category, vendor, library
                )
            else:
                logger.warning("Vendor %r failed for %s: %s", vendor, method, e, exc_info=True)
                if outage:
                    first_error, unconfirmed = _met_outage(
                        first_error, unconfirmed, vendor, e, generic_failure_words(e)
                    )
                else:
                    if isinstance(e, WiringGapError) and first_wiring is None:
                        first_wiring = _VendorFailure(vendor, e)
                    if first_error is None:
                        first_error = _VendorFailure(vendor, e)
            continue
        # The vendor returned: drop a deadline that predates this request (a
        # lapsed one), keep the one a sibling thread armed while it was in
        # flight — why is ``ThrottleLatch.clear``'s (#153). "Returned", not
        # "answered": a no-data or outage verdict raised above leaves the
        # latch alone, by choice. Only a raised throttle arms the latch, so a
        # vendor that renders a partial throttle into its report (Deribit,
        # when not every request was refused) is never skipped on the
        # strength of it.
        VENDOR_THROTTLE_LATCH.clear(vendor, before=sent_at)
        return result

    # If any vendor reported "no data", the symbol is genuinely unavailable.
    # Return one explicit, instructive sentinel rather than a vendor-specific
    # empty string, so the agent reports "unavailable" instead of inventing a
    # value. This takes precedence over incidental fallback errors — but not
    # over what they say about the verdict: when a vendor in the chain was
    # DOWN, or throttled before it could answer, or skipped on a latch, the
    # rest's "no data" was never confirmed by the source that would normally
    # serve the symbol, and "may be invalid" is a statement the agent reasons
    # from (#142, #172). Those variants swap the middle clause only — the
    # prefix every reader keys on and the do-not-fabricate tail are one
    # literal — and do not assert the symbol valid either: a fallback that
    # DID answer (a stale frame, an "Invalid API call") is still quoted in
    # ``reason``, so the wording is "unconfirmed", not "fine".
    # A core chain in which a vendor's own library failed ends as one line of
    # report text, ahead of every other ending — a sibling's "no data"
    # included: the vendor that failed DID have the symbol's data, its code
    # failed on it, and a sentinel saying the symbol "may be invalid" would
    # be a claim the source that served contradicts. This is the leaves' old
    # policy — a library bug must not abort a run another data point could
    # still serve, and it ended the call as text whatever the chain order —
    # moved to the one place that knows whether any vendor served (#187).
    # Placed here rather than at the leaf, it lets the chain reach a sibling
    # vendor first, and a missing key, an outage or a no-data verdict met on
    # the way stays in the logs, as it did when the leaf rendered the prose
    # itself. Written for every vendor, so the sentence does not depend on
    # which vendor failed (#58): the subject is the getter's, the library's
    # message is flattened and capped on its way in — a pandas message can
    # carry a frame repr, newlines and pipes included — and the ERROR the
    # lane logged keeps the whole of it, where the leaf's own log line used
    # to (#219). An optional category keeps its own
    # sentinels below: no-data first, then ``DATA_UNAVAILABLE``.
    # Ahead of the report line, and ONLY there: a core category's chain that
    # met both our wiring gap and some vendor's library failing must end on
    # ours, which someone can fix, rather than hand the analyst the other as
    # its answer. Both conditions, because this outranks nothing else — a
    # sibling's clean no-data verdict still wins (the ranking every ending
    # below keeps), the caller's own indicator mistake still surfaces ahead
    # of any vendor failure (#137: a missing key in its place would point at
    # the wrong remedy), and among the vendors' own failures the first met
    # still wins. An optional category keeps its sentinel, where a gap and a
    # library failure are named the same way, by whichever was met first
    # (#219).
    if (
        first_wiring is not None
        and first_library is not None
        and category not in OPTIONAL_CATEGORIES
    ):
        raise first_wiring.error

    if first_library is not None and category not in OPTIONAL_CATEGORIES:
        logger.warning(
            "No vendor served %s; reporting the library failure retrieving %s as text%s",
            method,
            first_library.error.what,
            "" if last_no_data is None else f" (a vendor also reported no data: {last_no_data})",
        )
        return _library_failure_prose(first_library.error)

    if last_no_data is not None:
        if first_error is not None:
            # A vendor also hit a real error; surface it in logs so the no-data
            # verdict can't hide a broken primary (network/auth/etc.). One slot
            # is enough: a library failure fills this one too (#219), so it
            # cannot be the only failure the chain met.
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method,
                first_error.error,
            )
        # Both are the CALLER'S symbol echoed back into a sentence it reads —
        # the model's own argument, or the alias table's answer for it — and
        # both are quoted, so they take ``quote_argument`` rather than the
        # bare-echo helper: a spelling carrying its own quote character would
        # otherwise close the span early and the prose after it would read as
        # the router's words (#232, #233). The comparison stays on the RAW
        # pair, so the resolved clause still appears exactly when the alias
        # table changed the symbol, not when the flattening did.
        raw = last_no_data.symbol
        canonical = last_no_data.canonical
        sym = quote_argument(raw)
        resolved = "" if canonical == raw else f" (resolved to {quote_argument(canonical)})"
        # Surface the typed error's detail (e.g. "latest row is 2025-06-11 ...
        # stale") so the agent sees the specific reason — invalid symbol, no
        # coverage, or stale data — not just a generic "unavailable". The
        # detail quotes what the vendor answered (a column list, a date), so
        # it takes the same flatten-and-cap as the other two slots.
        detail = sanitize_untrusted(last_no_data.detail or "", limit=MAX_UNTRUSTED_CHARS)
        reason = f" ({detail})" if detail else ""
        # Which source never confirmed the verdict, and the words for it:
        # the one ranked slot the lanes filled (#217), chain-order neutral
        # on purpose — the vendor may be the primary or the fallback. Each
        # rank names what happened in its own words ("rate limited" is not
        # "unavailable"; the model should not read a throttled source as a
        # down one); the rest of the sentence is one literal, so the three
        # variants stay one shape to every reader.
        if unconfirmed is not None:
            verdict = (
                f": vendor '{unconfirmed.vendor}' {unconfirmed.state} and the other "
                f"configured vendor(s) had no usable data{reason}. Treat the symbol as "
                f"unconfirmed rather than invalid: a source that would normally serve it "
                f"{_WHY[unconfirmed.rank]}, and the others' answers alone do not settle whether "
                f"it is valid, delisted, or not covered."
            )
        else:
            verdict = (
                f" from any configured vendor{reason}. The symbol may be invalid, "
                f"delisted, not covered, or the vendor returned stale data."
            )
        return (
            f"NO_DATA_AVAILABLE: No usable market data for {sym}{resolved}{verdict} "
            f"Do not estimate or fabricate values — report that data is unavailable "
            f"for this symbol."
        )

    # The failure that surfaces, decided in one expression. Only an optional
    # category still reaches here having met a library failure, a core chain
    # having returned above, and there it is one of the vendors' own
    # failures like any other — held in the slot below, where first met
    # wins; its words are the subject's rather than a class name, which is
    # the whole of what it earns here (#219). First, a
    # caller's mistake — the indicator name, which the tool wrapper renders
    # as one line of report text — outranks a vendor's failure: a missing key
    # surfacing instead would abort the call and point at the wrong remedy
    # (#137). Unless a vendor was DOWN: then the name may be one that vendor
    # computes, and the outage is the fact to surface — the typo stays in the
    # logs, as the vendor failure does in the other case. Among the vendors'
    # own failures the FIRST met still wins, an outage included: a missing
    # key ahead of a down fallback surfaces the key, since that is the
    # standing misconfiguration the operator has to fix either way, and the
    # outage is in the logs. A chain exhausted by
    # nothing but rate limits (e.g. a single-vendor chain hitting a 429 with
    # no cache) must degrade like any other failure, not fall through to the
    # bare no-vendor RuntimeError below: a throttle is the fallback verdict so
    # a real error (network/auth/bug) stays the one surfaced, and a throttle
    # actually met outranks a latch skip whatever the chain order, since it
    # carries the vendor's own detail (a Retry-After) where the skip describes
    # a request that was never sent.
    was_down = unconfirmed is not None and unconfirmed.rank == _OUTAGE
    if first_caller_error is not None and was_down:
        logger.warning(
            "Not surfacing the caller's indicator error for %s (%s): vendor %r was "
            "down, so the name may be one it computes",
            method,
            first_caller_error.error,
            unconfirmed.vendor,
        )
        first_caller_error = None
    elif first_caller_error is not None and first_error is not None:
        logger.warning(
            "Surfacing the caller's indicator error for %s; a vendor also failed: %s",
            method,
            first_error.error,
        )
    # A throttle met or a latch skip stands as the failure when nothing else
    # raised — the slot holds the lower-ranked of the two whatever the chain
    # order. An outage held there never surfaces from here: its lane set
    # ``first_error``, which precedes the slot.
    unconfirmed_by = (
        _VendorFailure(unconfirmed.vendor, unconfirmed.error) if unconfirmed is not None else None
    )
    failed = first_caller_error or first_error or unconfirmed_by

    # No vendor returned data and none reported clean "no data" — surface the
    # first real error (e.g. the primary vendor's network failure). Optional
    # enrichment categories degrade to a sentinel instead, so flavour data can't
    # abort the run.
    if failed is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, failed.error)
            # The vendor ahead of the words (#203): the lane's log line names
            # it, but the sentinel is what the report artifacts keep, and a
            # multi-vendor category's reader should not need the log to tell
            # which source failed.
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({failed.vendor}: {_optional_failure_words(failed.error)}). Proceed without "
                f"it; do not fabricate values."
            )
        raise failed.error

    # An empty vendor registry for a known method is a configuration/registry
    # problem — classify it (#32) so callers see the taxonomy, not a bare
    # RuntimeError that bypasses every vendor-error handler.
    raise VendorNotConfiguredError(f"No available vendor for '{method}'")
