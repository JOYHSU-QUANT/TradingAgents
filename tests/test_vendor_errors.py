"""The vendor data-error hierarchy: every "vendor couldn't return usable data"
condition derives from VendorError, so the router catches base types and any
vendor slots in without new handling.
"""
import contextlib
import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.alpha_vantage_common import (
    AlphaVantageDailyQuotaError,
    AlphaVantageNotConfiguredError,
    AlphaVantageRateLimitError,
)
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorLibraryError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)
from tradingagents.dataflows.fred import FredNotConfiguredError
from tradingagents.dataflows.utils import finite_float, raise_for_http_status


@pytest.mark.unit
class HierarchyTests(unittest.TestCase):
    def test_all_conditions_derive_from_vendor_error(self):
        for cls in (
            NoMarketDataError,
            VendorRateLimitError,
            VendorUnavailableError,
            VendorLibraryError,
            VendorNotConfiguredError,
        ):
            self.assertTrue(issubclass(cls, VendorError))

    def test_a_library_failure_carries_its_subject_and_the_librarys_message(self):
        # The router writes the report line from these two, separately: the
        # subject is the getter's, the message the library's (capped on its
        # way into the report, whole in the log).
        err = VendorLibraryError("rsi values for AAPL", str(KeyError("volume")))
        self.assertEqual(err.what, "rsi values for AAPL")
        self.assertEqual(err.detail, "'volume'")
        self.assertEqual(str(err), "rsi values for AAPL: 'volume'")

    def test_not_configured_is_still_a_value_error(self):
        # Back-compat: existing `except ValueError` callers keep working.
        self.assertTrue(issubclass(VendorNotConfiguredError, ValueError))

    def test_vendor_named_errors_subclass_the_generic_bases(self):
        self.assertTrue(issubclass(AlphaVantageRateLimitError, VendorRateLimitError))
        # The daily-quota verdict is the rate-limit type with a longer stand-off
        # window; the base carries the shared window as its default (#153).
        self.assertTrue(issubclass(AlphaVantageDailyQuotaError, AlphaVantageRateLimitError))
        self.assertIsNone(VendorRateLimitError.latch_ttl_s)
        self.assertTrue(issubclass(AlphaVantageNotConfiguredError, VendorNotConfiguredError))
        self.assertTrue(issubclass(FredNotConfiguredError, VendorNotConfiguredError))
        # ... and therefore still ValueErrors
        self.assertTrue(issubclass(FredNotConfiguredError, ValueError))

    def test_symbol_utils_reexports_no_market_data_error(self):
        from tradingagents.dataflows.symbol_utils import (
            NoMarketDataError as ReExported,
        )
        self.assertIs(ReExported, NoMarketDataError)


@pytest.mark.unit
class RouterHandlesBaseTypesTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_rate_limit_subclass_caught_by_base(self):
        # A vendor-named rate-limit error skips to the next vendor in the chain.
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})

        def _throttled(*a, **k):
            raise AlphaVantageRateLimitError("slow down")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _throttled, "yfinance": lambda *a, **k: "YF"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "YF")

    def test_not_configured_falls_through_to_next_vendor(self):
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})

        def _unconfigured(*a, **k):
            raise AlphaVantageNotConfiguredError("no key")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _unconfigured, "yfinance": lambda *a, **k: "YF"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "YF")

    def test_sole_unconfigured_vendor_surfaces_the_error(self):
        # With no fallback, the not-configured condition must surface (not vanish).
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage"}})

        def _unconfigured(*a, **k):
            raise AlphaVantageNotConfiguredError("no key")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _unconfigured}},
            clear=False,
        ), self.assertRaises(AlphaVantageNotConfiguredError):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")


if __name__ == "__main__":
    unittest.main()


@pytest.mark.unit
class TestRateLimitPolicyOrdering:
    """``raise_for_http_status`` consults the boundary's 429 policy FIRST.

    The parameter exists because the helper types only a 5xx: a 429 left to
    ``raise_for_status()`` becomes an ``HTTPError``, which ``is_unreached``
    excludes, so the boundary files a routine throttle as its own structural
    breakage (#278 shipped that).
    """

    def _response(self, status):
        response = mock.Mock(spec=["status_code", "raise_for_status", "headers"])
        response.status_code = status
        response.headers = {}
        response.raise_for_status.side_effect = (
            requests.HTTPError(f"HTTP {status}", response=response) if status >= 400 else None
        )
        return response

    def test_the_policy_is_consulted_before_the_library_raise(self):
        class _Throttled(VendorRateLimitError):
            pass

        def _policy(_response):
            raise _Throttled("this vendor is throttling us")

        response = self._response(429)
        with pytest.raises(_Throttled):
            raise_for_http_status(response, "Vendor", rate_limit=_policy)
        # The library's raise never ran: reaching it is the misfiling.
        response.raise_for_status.assert_not_called()

    def test_without_a_policy_a_429_keeps_the_library_behaviour(self):
        # Deliberate: several boundaries absorb a throttle in their own retry
        # or stale-cache lane, and a typed raise from here would bypass it.
        with pytest.raises(requests.HTTPError):
            raise_for_http_status(self._response(429), "Vendor")

    def test_the_policy_is_not_consulted_for_any_other_status(self):
        seen = []
        for status in (200, 403, 500):
            response = self._response(status)
            with contextlib.suppress(requests.HTTPError, VendorUnavailableError):
                raise_for_http_status(response, "Vendor", rate_limit=lambda r: seen.append(r))
        assert seen == []

    def test_a_5xx_still_outranks_the_library_raise_with_a_policy_given(self):
        with pytest.raises(VendorUnavailableError):
            raise_for_http_status(self._response(503), "Vendor", rate_limit=lambda r: None)

    def test_a_policy_that_returns_is_our_bug_not_a_silent_fallthrough(self):
        # Returning would leave the throttle to fall through to the library
        # raise - the exact misclassification the parameter prevents.
        with pytest.raises(WiringGapError, match="returned instead of raising"):
            raise_for_http_status(self._response(429), "Vendor", rate_limit=lambda r: None)


@pytest.mark.unit
class TestFiniteFloat:
    """The bool-rejecting numeric rule the vendor modules share.

    A different question from ``is_finite_number``: these values are about to
    be summed, compared or weighted, so a JSON ``true`` passing as 1 would be a
    figure nobody sent.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            (1, 1.0),
            (-2.5, -2.5),
            (0, 0.0),
            (True, None),
            (False, None),
            (float("nan"), None),
            (float("inf"), None),
            (float("-inf"), None),
            (None, None),
            (object(), None),
            ([1], None),
        ],
        ids=["int", "float", "zero", "true", "false", "nan", "inf", "neg_inf", "none", "object", "list"],
    )
    def test_the_value_classes(self, value, expected):
        assert finite_float(value) == expected or (
            expected is None and finite_float(value) is None
        )

    def test_a_huge_int_answers_rather_than_raising(self):
        # ``math.isfinite`` RAISES on an int too large to convert to a float,
        # and a JSON integer literal has no bound, so json.loads can hand one
        # back. A predicate that throws at an input class it exists to turn
        # away inverts its own contract - and two of the four copies this
        # replaced guarded it while the others did not.
        assert finite_float(10**400) is None
        assert finite_float(-(10**400)) is None

    def test_strings_are_refused_unless_the_vendor_sends_them(self):
        # Opt-in because it is a property of the VENDOR, not the question: a
        # string reaching the vendors that do not set it means the payload is
        # not the shape they parsed.
        assert finite_float("1.5") is None
        assert finite_float("1.5", allow_str=True) == 1.5
        assert finite_float("nope", allow_str=True) is None
        assert finite_float("nan", allow_str=True) is None
        assert finite_float("inf", allow_str=True) is None

    def test_a_bool_is_refused_even_when_strings_are_allowed(self):
        assert finite_float(True, allow_str=True) is None
