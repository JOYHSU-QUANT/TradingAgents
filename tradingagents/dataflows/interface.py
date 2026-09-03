import logging
import time

import requests

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
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
)
from .farside import get_etf_flow_data as get_farside_etf_flows
from .fear_greed import get_fear_greed_data as get_alternative_me_fear_greed
from .fred import get_macro_data as get_fred_macro_data
from .polymarket import get_prediction_markets as get_polymarket_prediction_markets
from .sosovalue import get_etf_flow_data as get_sosovalue_etf_flows
from .sosovalue_macro import get_economic_calendar_data as get_sosovalue_economic_calendar
from .sosovalue_treasuries import get_btc_treasury_data as get_sosovalue_btc_treasuries
from .throttle import VENDOR_THROTTLE_LATCH
from .utils import MAX_UNTRUSTED_CHARS, http_status, sanitize_untrusted
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


def _failure_account(e: BaseException) -> str:
    """The words a failed vendor call contributes to a sentinel the model reads.

    Written into two slots of ``route_to_vendor``: the optional category's
    ``DATA_UNAVAILABLE`` parenthesis and the no-data sentinel's outage clause.
    A typed vendor error's message was authored at the boundary, so it rides
    along — flattened and capped, because not every boundary caps what it
    quotes (yfinance quotes the library's exception, decoded error body
    included, #172); a remedy a boundary appends after the vendor's text is
    the operator's, in the log. So would the caller's own indicator mistake,
    whose message is the remedy, should an optional category ever compute
    one. Anything else is the generic lane's and
    contributes ``_generic_failure_words`` — never its text: a ``requests``
    message quotes the request URL, API key included (#171). The router's
    warning log has the full message either way.
    """
    if isinstance(e, (VendorError, UnsupportedIndicatorError)):
        # A typed error raised with no message would render as "()".
        return sanitize_untrusted(e, limit=MAX_UNTRUSTED_CHARS) or type(e).__name__
    return _generic_failure_words(e)


def _generic_failure_words(e: BaseException) -> str:
    """The words for an exception the generic lane caught, by status and class.

    One vocabulary for both sentinels — the no-data outage clause and the
    optional parenthesis read the same 503 as "answered HTTP 503" rather
    than one of them saying ``HTTPError: HTTP 503``. The status first, read
    off the exception the library-neutral way (``http_status``): a 401/403
    is the vendor refusing this client, any other status is its answer. No
    status and a transport exception (every ``requests`` and curl_cffi
    failure is an ``OSError``; the ``ValueError``-flavoured ones and a
    ``requests.HTTPError`` with no response are not the wire) is the vendor
    not reached. Anything else — a library bug — is its class name. Which of
    these count as an OUTAGE for the no-data verdict is the lane's decision,
    not this function's.
    """
    status = http_status(e)
    if status is not None:
        refused = ", refusing this client" if status in (401, 403) else ""
        return f"answered HTTP {status}{refused}"
    if isinstance(e, OSError) and not isinstance(e, (ValueError, requests.HTTPError)):
        return f"could not be reached: {type(e).__name__}"
    return type(e).__name__


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
    first_error: Exception | None = None
    # A caller's mistake (an indicator name no vendor computes) is kept apart
    # from the vendors' failures so it outranks them at the verdict (#137).
    first_caller_error: UnsupportedIndicatorError | None = None
    # The first vendor that was DOWN — answered with an outage page or could
    # not be reached — and a short, flattened account of it: a fallback's "no
    # data" is then unconfirmed by the source that would normally serve the
    # symbol, and the sentinel has to say so (#142). Text only, never the
    # exception: it travels into a sentinel the model reads.
    first_outage: tuple[str, str] | None = None
    first_rate_limit: VendorRateLimitError | None = None
    first_skip: VendorRateLimitError | None = None
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
            if first_skip is None:
                first_skip = VendorRateLimitError(
                    f"Vendor {vendor!r} rate limited a recent request; skipped without "
                    f"contacting it for another {remaining:.0f}s"
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
            if first_rate_limit is None:
                first_rate_limit = e
            continue
        except VendorNotConfiguredError as e:
            logger.warning("Vendor %r not configured for %s; trying next vendor.", vendor, method)
            if first_error is None:
                first_error = e  # Surface it if no other vendor can serve the call.
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
            if first_error is None:
                first_error = e
            if first_outage is None:
                first_outage = (vendor, _failure_account(e))
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
                first_caller_error = e
            continue
        except Exception as e:
            # Don't let one vendor's failure crash the call when another can
            # serve it, but never swallow silently: a broken primary must be
            # visible in the logs (#989), not hidden behind a fallback's verdict.
            # exc_info so a real bug (e.g. in an HTML-scraping vendor) leaves a
            # traceback instead of looking identical to a network outage.
            logger.warning("Vendor %r failed for %s: %s", vendor, method, e, exc_info=True)
            if first_error is None:
                first_error = e
            # The same fact as the outage lane above, for the verdict below,
            # read off the exception: a transport failure — a reset, a
            # timeout; requests' and curl_cffi's exceptions are all OSError —
            # is the vendor being unreachable, and an answered status can say
            # as much: a 5xx, or a 401/403 refusing this client (what the
            # yfinance window lets out raw for exactly that reason). Judged
            # by the status the exception carries, never by its class —
            # yfinance's HTTPError is curl_cffi's, not requests'. Any other
            # status is the vendor answering about this request (the 404 a
            # boundary leaves alone), as is a requests HTTPError with no
            # status to read; a ValueError-flavoured requests exception
            # (MissingSchema, InvalidURL, JSONDecodeError) is a bug or an
            # answer, not the wire. The words are ``_generic_failure_words``'
            # — the status or the class only, never the text: a requests
            # message quotes the request URL, API key included.
            if first_outage is None and isinstance(e, OSError) and not isinstance(e, ValueError):
                status = http_status(e)
                unreached = status is None and not isinstance(e, requests.HTTPError)
                if unreached or (status is not None and (status >= 500 or status in (401, 403))):
                    first_outage = (vendor, _generic_failure_words(e))
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
    # DOWN, the rest's "no data" was never confirmed by the source that would
    # normally serve the symbol, and "may be invalid" is a statement the
    # agent reasons from (#142). The outage variant swaps the middle clause
    # only — the prefix every reader keys on and the do-not-fabricate tail
    # are one literal — and does not assert the symbol valid either: a
    # fallback that DID answer (a stale frame, an "Invalid API call") is
    # still quoted in ``reason``, so the wording is "unconfirmed", not "fine".
    if last_no_data is not None:
        if first_error is not None:
            # A vendor also hit a real error; surface it in logs so the no-data
            # verdict can't hide a broken primary (network/auth/etc.).
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method,
                first_error,
            )
        sym = last_no_data.symbol
        canonical = last_no_data.canonical
        resolved = "" if canonical == sym else f" (resolved to '{canonical}')"
        # Surface the typed error's detail (e.g. "latest row is 2025-06-11 ...
        # stale") so the agent sees the specific reason — invalid symbol, no
        # coverage, or stale data — not just a generic "unavailable". The
        # detail quotes what the vendor answered (a column list, a date), so
        # it takes the same flatten-and-cap as the other two slots.
        detail = sanitize_untrusted(last_no_data.detail or "", limit=MAX_UNTRUSTED_CHARS)
        reason = f" ({detail})" if detail else ""
        if first_outage is not None:
            down_vendor, outage = first_outage
            # Chain-order neutral on purpose: the vendor that was down may be
            # the primary or the fallback, and either way the others' "no
            # data" went unconfirmed by it.
            verdict = (
                f": vendor '{down_vendor}' was unavailable ({outage}) and the other "
                f"configured vendor(s) had no usable data{reason}. Treat the symbol as "
                f"unconfirmed rather than invalid: a source that would normally serve "
                f"it was unavailable, and the others' answers alone do not settle "
                f"whether it is valid, delisted, or not covered."
            )
        else:
            verdict = (
                f" from any configured vendor{reason}. The symbol may be invalid, "
                f"delisted, not covered, or the vendor returned stale data."
            )
        return (
            f"NO_DATA_AVAILABLE: No usable market data for '{sym}'{resolved}{verdict} "
            f"Do not estimate or fabricate values — report that data is unavailable "
            f"for this symbol."
        )

    # The failure that surfaces, decided in one expression. A caller's
    # mistake — the indicator name, which the tool wrapper renders as one
    # line of report text — outranks a vendor's failure: a missing key
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
    if first_caller_error is not None and first_outage is not None:
        logger.warning(
            "Not surfacing the caller's indicator error for %s (%s): vendor %r was "
            "down, so the name may be one it computes",
            method,
            first_caller_error,
            first_outage[0],
        )
        first_caller_error = None
    elif first_caller_error is not None and first_error is not None:
        logger.warning(
            "Surfacing the caller's indicator error for %s; a vendor also failed: %s",
            method,
            first_error,
        )
    first_error = first_caller_error or first_error or first_rate_limit or first_skip

    # No vendor returned data and none reported clean "no data" — surface the
    # first real error (e.g. the primary vendor's network failure). Optional
    # enrichment categories degrade to a sentinel instead, so flavour data can't
    # abort the run.
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({_failure_account(first_error)}). Proceed without it; do not fabricate values."
            )
        raise first_error

    # An empty vendor registry for a known method is a configuration/registry
    # problem — classify it (#32) so callers see the taxonomy, not a bare
    # RuntimeError that bypasses every vendor-error handler.
    raise VendorNotConfiguredError(f"No available vendor for '{method}'")
