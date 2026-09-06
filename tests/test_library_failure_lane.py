"""The library lane: a getter's untyped failure leaves as ``VendorLibraryError`` (#187, #86).

Nine leaves used to hand-copy ``except VendorError: raise`` / ``except OSError:
raise`` / ``except Exception: return "Error retrieving ..."``. One that forgot
the guard rendered a throttle as a report (#85), seven logged nothing, and the
prose — read by the router as a successful answer — ended the chain at the
vendor that had just failed even when a sibling computed the same tool its
own way. ``utils.library_failure_lane`` is that handler once; these tests pin
what it lets through, what it wraps and what it logs. That every leaf which
used to carry the copy now runs under it is the registry-derived tests' job:
test_yfinance_rate_limit drives every registered yfinance impl through its
seam, test_alpha_vantage_hardening the Alpha Vantage indicator impl, and
test_vendor_routing the router's side.
"""

import logging

import pytest
import requests
from curl_cffi.requests import exceptions as curl_exceptions

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    UnsupportedIndicatorError,
    VendorLibraryError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
)
from tradingagents.dataflows.utils import library_failure_lane

_logger = logging.getLogger(__name__)


def _leaf(symbol, indicator, fail=None):
    with library_failure_lane(f"{indicator} values for {symbol}", log=_logger):
        if fail is not None:
            raise fail
        return f"{indicator} for {symbol}"


# What the lane must let out untouched: every taxonomy type (its router lane
# is its own — an already-typed library failure included, so a lane inside a
# lane cannot double-wrap), the caller's indicator mistake, and the OSError
# family both transport libraries raise through (#116) plus the OHLCV cache's
# own.
_PASSED_THROUGH = {
    "no_data": NoMarketDataError("AAPL", detail="no rows"),
    "rate_limit": VendorRateLimitError("429"),
    "unavailable": VendorUnavailableError("HTTP 503"),
    "not_configured": VendorNotConfiguredError("no key"),
    "library": VendorLibraryError("x", "y"),
    "caller": UnsupportedIndicatorError("bogus"),
    "cache": PermissionError("cache dir is read-only"),
    "requests": requests.Timeout("read timed out"),
    "curl_cffi": curl_exceptions.ConnectionError("connection reset"),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_PASSED_THROUGH))
def test_typed_caller_and_transport_failures_pass_through_untouched(name, caplog):
    exc = _PASSED_THROUGH[name]
    with caplog.at_level(logging.ERROR), pytest.raises(type(exc)) as info:
        _leaf("AAPL", "rsi", fail=exc)
    assert info.value is exc
    assert not caplog.records  # nothing to log: those lanes are the router's


@pytest.mark.unit
def test_anything_else_leaves_as_the_library_type_with_the_subject_filled_in(caplog):
    cause = KeyError("volume")
    with caplog.at_level(logging.ERROR), pytest.raises(VendorLibraryError) as info:
        _leaf("AAPL", "rsi", fail=cause)
    err = info.value
    assert err.what == "rsi values for AAPL"
    assert err.detail == str(cause)  # whole; the router caps it on its way into the report
    assert err.__cause__ is cause
    assert str(err) == "rsi values for AAPL: 'volume'"


@pytest.mark.unit
def test_the_traceback_is_logged_under_the_logger_the_leaf_passed(caplog):
    cause = RuntimeError("boom")
    with caplog.at_level(logging.ERROR), pytest.raises(VendorLibraryError):
        _leaf("AAPL", "rsi", fail=cause)
    [record] = caplog.records
    assert record.name == __name__  # the leaf's module, not utils'
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[1] is cause
    assert "rsi values for AAPL" in record.getMessage()
    assert "boom" in record.getMessage()


@pytest.mark.unit
def test_the_success_path_is_untouched(caplog):
    with caplog.at_level(logging.ERROR):
        assert _leaf("AAPL", "rsi") == "rsi for AAPL"
    assert not caplog.records
