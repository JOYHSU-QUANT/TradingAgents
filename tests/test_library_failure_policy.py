"""The untyped lane: what a vendor leaves unhandled is read as its library (#187, #219).

Nine getters used to hand-copy ``except VendorError: raise`` / ``except
OSError: raise`` / ``except Exception: return "Error retrieving ..."``. One
that forgot the guard rendered a throttle as a report (#85), seven logged
nothing, and the prose — read by the router as a successful answer — ended
the chain at the vendor that had just failed. PR #218 replaced the copies
with one ``with`` block; this suite pinned that block. What it did not fix
was WHICH getters wore it: the same routed tool aborted the run through
Alpha Vantage and reported a line of prose through yfinance, because only
the yfinance side had ever carried a broad handler to replace (#219).

The conversion is the router's now, so these tests drive the router: what it
reads as a vendor's library failing, what it does not, and where the
traceback goes. That every registered impl leaves its untyped failures
unhandled is the registry-derived suites' job (test_yfinance_rate_limit for
the yfinance leaves, test_alpha_vantage_hardening for Alpha Vantage), and
the endings themselves are test_vendor_routing's.
"""

import logging
from unittest import mock

import pytest

from tests.test_yfinance_rate_limit import _PROPAGATED_OSERRORS
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    UnsupportedIndicatorError,
    VendorError,
    VendorLibraryError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)

# One routed tool, one vendor, so the ending is the failure's alone. news_data
# is a core category: nothing here degrades to an optional category's sentinel.
_METHOD = "get_news"
_ARGS = ("AAPL", "2026-06-01", "2026-06-05")
_SUBJECT = "news for AAPL"


def _route(impl):
    set_config({"data_vendors": {"news_data": "yfinance"}})
    with mock.patch.dict(interface.VENDOR_METHODS, {_METHOD: {"yfinance": impl}}):
        return interface.route_to_vendor(_METHOD, *_ARGS)


def _raises(exc):
    def impl(*a, **k):
        raise exc

    return impl


class _VendorNamedError(VendorError):
    """A thin vendor-named subclass, the shape ``DeribitError`` and friends take.

    The taxonomy promises one of these needs no new ``except`` clause, so it
    reaches the untyped lane like a library bug — and must not be rewritten
    into one: its message is the vendor's own verdict.
    """


# What the untyped lane must not read as the vendor's library: every taxonomy
# type (its own router lane, or the base type's promise, decides its ending),
# the caller's indicator mistake, the OSError family both transport libraries
# raise through (#116) plus the OHLCV cache's own, and this project's own
# wiring (#111, #200).
_NOT_THE_LIBRARY = {
    "no_data": NoMarketDataError("AAPL", detail="no rows"),
    "rate_limit": VendorRateLimitError("429"),
    "unavailable": VendorUnavailableError("HTTP 503"),
    "not_configured": VendorNotConfiguredError("no key"),
    "vendor_named": _VendorNamedError("no BTC DVOL readings on or before 2026-06-01"),
    "caller": UnsupportedIndicatorError("bogus"),
    # The measured transport family, from the suite that documents why these
    # three pin it — not a second copy of the table.
    **_PROPAGATED_OSERRORS,
    "wiring": WiringGapError("news configuration: 'news_article_limit'"),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_NOT_THE_LIBRARY))
def test_what_the_untyped_lane_does_not_read_as_the_library(name):
    exc = _NOT_THE_LIBRARY[name]
    if isinstance(exc, NoMarketDataError):
        # Its own ending, and the one that is not a raise: the symbol is
        # unavailable, which is a verdict rather than a failure.
        assert _route(_raises(exc)).startswith("NO_DATA_AVAILABLE")
        return
    # The instance itself, not a wrapper: a core chain with nothing else to
    # say raises what it met, and the report line is reserved for a library.
    with pytest.raises(type(exc)) as info:
        _route(_raises(exc))
    assert info.value is exc


@pytest.mark.unit
def test_anything_else_is_the_library_named_from_the_call():
    # The subject is the routed tool's, read off this call's own arguments —
    # the vendor does not enter into it, which is what makes two vendors of
    # one tool name the same failure alike (#187).
    out = _route(_raises(KeyError("volume")))
    assert out == f"Error retrieving {_SUBJECT}: 'volume'"


@pytest.mark.unit
def test_the_failure_carries_the_whole_message_and_its_cause():
    # The router caps the message on its way into the report line; the type
    # itself carries it whole, and the library's own exception rides along as
    # __cause__ for whoever reads the failure rather than the report.
    cause = RuntimeError("boom")
    failure = interface._as_library_failure("news_data", _METHOD, _ARGS, cause)
    assert isinstance(failure, VendorLibraryError)
    assert failure.what == _SUBJECT
    assert failure.detail == "boom"
    assert failure.__cause__ is cause


@pytest.mark.unit
def test_the_traceback_is_logged_by_the_router_with_the_whole_message(caplog):
    # The lane at the getter used to log this, under the getter's own module;
    # with the conversion at the router there is one log line, and it is the
    # uncapped copy of a message the report line caps (#187).
    cause = RuntimeError("boom")
    with caplog.at_level(logging.WARNING, logger="tradingagents.dataflows.interface"):
        _route(_raises(cause))
    [record] = [r for r in caplog.records if r.exc_info is not None]
    assert record.name == "tradingagents.dataflows.interface"
    assert record.exc_info[1] is cause
    assert "boom" in record.getMessage()


@pytest.mark.unit
@pytest.mark.parametrize("failure", [KeyError("volume"), VendorLibraryError("x", "y")])
def test_a_loud_category_raises_whoever_named_the_library_failure(failure):
    # ``LOUD_LIBRARY_CATEGORIES`` is about the ENDING, so it cannot depend on
    # who typed the failure: a boundary naming its own library bug gets the
    # same answer from the router as one that left it untyped (#219). OHLCV
    # is the analyst's primary input, and a report line where the prices
    # should be would leave the run reasoning from nothing.
    set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
    with (
        mock.patch.dict(interface.VENDOR_METHODS, {"get_stock_data": {"yfinance": _raises(failure)}}),
        pytest.raises(type(failure)) as info,
    ):
        interface.route_to_vendor("get_stock_data", "AAPL", "2026-06-01", "2026-06-05")
    assert info.value is failure


@pytest.mark.unit
def test_the_success_path_is_untouched():
    assert _route(lambda *a, **k: "the report") == "the report"
