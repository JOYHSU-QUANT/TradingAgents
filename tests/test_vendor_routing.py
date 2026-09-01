"""Vendor router must respect the configured chain and never silently hide a
broken primary.

Regressions for #988 (explicit single-vendor config still fell back to others),
#289 (fallback ran for unchosen vendors), and #989 (serious primary failures
were swallowed without a trace).
"""

import copy
import json
import types
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.alpha_vantage_news as avn
import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    UnsupportedIndicatorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbol_utils import NoMarketDataError
from tradingagents.dataflows.throttle import THROTTLE_LATCH_TTL_S, VENDOR_THROTTLE_LATCH


def _reset_config():
    # Hard reset: set_config() merges, so empty DEFAULT dicts (e.g. tool_vendors)
    # don't clear keys leaked by other tests. Replace the global outright.
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _no_data(symbol, *a, **k):
    raise NoMarketDataError(symbol, symbol, "no rows")


def _returns(value):
    def impl(symbol, *a, **k):
        return value

    return impl


def _raises(exc):
    def impl(symbol, *a, **k):
        raise exc

    return impl


@pytest.mark.unit
class VendorRoutingTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def _route(self, vendors_for_get_stock_data):
        return mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": vendors_for_get_stock_data},
            clear=False,
        )

    def test_explicit_single_vendor_does_not_fall_back(self):
        # #988: with yfinance pinned, a healthy alpha_vantage must NOT be used.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        av = mock.Mock(side_effect=_returns("AV_DATA"))
        with self._route({"yfinance": _no_data, "alpha_vantage": av}):
            result = interface.route_to_vendor("get_stock_data", "FAKE", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        av.assert_not_called()  # the unchosen vendor was never tried

    def test_explicit_multi_vendor_falls_back_within_chain(self):
        # Listing both vendors opts in to ordered fallback. An all-valid chain
        # must also stay noise-free: the unknown-name warning may not fire.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with (
            self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}),
            self.assertNoLogs("tradingagents.dataflows.interface", level="WARNING"),
        ):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def test_primary_error_is_logged_not_masked(self):
        # #989: primary errors + fallback no-data -> NO_DATA, but the failure
        # must be visible in logs (broken primary not hidden).
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with (
            self._route({"yfinance": _raises(ValueError("boom")), "alpha_vantage": _no_data}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
        ):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        joined = "\n".join(cm.output)
        self.assertIn("boom", joined)  # the real error surfaced in logs
        self.assertIn("yfinance", joined)

    def test_unknown_configured_vendor_raises(self):
        set_config({"data_vendors": {"core_stock_apis": "bogus_vendor"}})
        with self.assertRaises(ValueError) as ctx:
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("bogus_vendor", str(ctx.exception))

    def test_unknown_vendor_in_mixed_chain_warns_and_keeps_survivors(self):
        # A mis-typed name beside a valid one must not silently shrink the
        # chain: the all-unknown raise above cannot fire, so the dropped name
        # has to surface in the logs while the survivor serves the call.
        set_config({"data_vendors": {"core_stock_apis": "bogus_vendor,yfinance"}})
        with (
            self._route({"yfinance": _returns("YF_DATA"), "alpha_vantage": _no_data}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
        ):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "YF_DATA")
        self.assertIn("bogus_vendor", "\n".join(cm.output))

    def test_default_sentinel_uses_all_vendors(self):
        # No explicit choice ("default") keeps the resilient full-chain behavior.
        set_config({"data_vendors": {"core_stock_apis": "default"}})
        with self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def _route_method(self, method, vendors):
        return mock.patch.dict(interface.VENDOR_METHODS, {method: vendors}, clear=False)

    def test_optional_category_degrades_instead_of_raising(self):
        # An optional enrichment vendor (FRED macro) that raises must NOT abort
        # the run — the router returns a sentinel so the analysis proceeds.
        set_config({"data_vendors": {"macro_data": "fred"}})
        with self._route_method(
            "get_macro_indicators", {"fred": _raises(ValueError("FRED 400: bad series"))}
        ):
            result = interface.route_to_vendor("get_macro_indicators", "cpi", "2026-01-01")
        self.assertIn("DATA_UNAVAILABLE", result)
        self.assertIn("macro_data", result)

    def test_alpha_vantage_indicator_rate_limit_reaches_the_fallback_vendor(self):
        # #60 end-to-end, through the REAL Alpha Vantage indicator getter: a 429
        # used to come back as an "Error retrieving ..." string, which the router
        # reads as a successful answer — the chain stopped at the exhausted
        # vendor and the agent got prose where indicator values belonged.
        import tradingagents.dataflows.alpha_vantage_indicator as avi
        from tradingagents.dataflows.alpha_vantage_common import AlphaVantageRateLimitError

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        with (
            mock.patch.object(
                avi,
                "_make_api_request",
                side_effect=AlphaVantageRateLimitError("25 requests per day"),
            ),
            self._route_method(
                "get_indicators",
                {"alpha_vantage": avi.get_indicator, "yfinance": _returns("YF_INDICATORS")},
            ),
        ):
            result = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 30)
        self.assertEqual(result, "YF_INDICATORS")

    def test_alpha_vantage_indicator_http_failure_reaches_the_fallback_vendor(self):
        # #87 end-to-end through the REAL getter and the REAL request boundary:
        # #72 classified only HTTP 429, so a 503 became "Error retrieving rsi
        # data: 503 Server Error" — which the router reads as a successful
        # answer, so the chain stopped at the vendor that had just gone down.
        import tradingagents.dataflows.alpha_vantage_indicator as avi

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        patch_key, patch_get = self._av_answers(status_code=503)
        with (
            patch_key,
            patch_get,
            self._route_method(
                "get_indicators",
                {"alpha_vantage": avi.get_indicator, "yfinance": _returns("YF_INDICATORS")},
            ),
        ):
            result = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 30)
        self.assertEqual(result, "YF_INDICATORS")

    def test_alpha_vantage_indicator_http_failure_alone_fails_loudly(self):
        # technical_indicators is a core category: with no other vendor to try,
        # the outage surfaces instead of degrading into prose an agent would
        # analyse as a report (the decided outcome in #60) — as the outage
        # type the request boundary now maps a 5xx to (#142), so the router
        # logged it without a traceback on the way out.
        import tradingagents.dataflows.alpha_vantage_indicator as avi

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage"}})
        patch_key, patch_get = self._av_answers(status_code=503)
        with (
            patch_key,
            patch_get,
            self._route_method("get_indicators", {"alpha_vantage": avi.get_indicator}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
            self.assertRaises(VendorUnavailableError),
        ):
            interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 30)
        self.assertTrue(all(r.exc_info is None for r in cm.records))

    def _av_answers(self, body_dict=None, status_code=200):
        # The strict fake from the hardening tests, not a mock.Mock: Mock
        # auto-creates attributes, so a new response read in _make_api_request
        # would silently pass here instead of failing the fake. A non-200
        # status answers with no body, since raise_for_status() fires before
        # anything reads one.
        import tradingagents.dataflows.alpha_vantage_common as av
        from tests.test_alpha_vantage_hardening import _patched_get

        body = "" if body_dict is None else json.dumps(body_dict)
        return (
            mock.patch.object(av, "get_api_key", return_value="k"),
            mock.patch.object(av.requests, "get", _patched_get(body, status_code=status_code)),
        )

    def test_alpha_vantage_error_envelope_reaches_the_fallback_vendor(self):
        # #68 end-to-end through the REAL Alpha Vantage news getter: an
        # "Error Message" body used to return as JSON the router read as a
        # successful answer, so the chain stopped at a vendor that had just
        # rejected the call.
        set_config({"data_vendors": {"news_data": "alpha_vantage,yfinance"}})
        patch_key, patch_get = self._av_answers({"Error Message": "Invalid API call."})
        with (
            patch_key,
            patch_get,
            self._route_method(
                "get_news",
                {"alpha_vantage": avn.get_news, "yfinance": _returns("YF_NEWS")},
            ),
        ):
            result = interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")
        self.assertEqual(result, "YF_NEWS")

    def test_alpha_vantage_error_envelope_alone_yields_the_no_data_sentinel(self):
        # A single-vendor chain answers the honest sentinel, and the vendor's
        # own wording rides along so a parameter mistake is not flattened into
        # a bare "no data".
        set_config({"data_vendors": {"news_data": "alpha_vantage"}})
        patch_key, patch_get = self._av_answers({"Error Message": "Invalid API call."})
        with (
            patch_key,
            patch_get,
            self._route_method("get_news", {"alpha_vantage": avn.get_news}),
        ):
            result = interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")
        self.assertIn("NO_DATA_AVAILABLE", result)
        self.assertIn("Invalid API call", result)

    def test_alpha_vantage_indicator_empty_window_reaches_the_fallback_vendor(self):
        # #106 end-to-end through the REAL getter: rows that all fell outside
        # the requested window used to be reported inside a well-formed
        # "## RSI values from ... to ..." report, which the router reads as a
        # successful answer — so the chain stopped at the vendor that had just
        # said it had nothing.
        import tradingagents.dataflows.alpha_vantage_indicator as avi

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        with (
            mock.patch.object(avi, "_make_api_request", return_value="time,RSI\n2020-01-02,55.0\n"),
            self._route_method(
                "get_indicators",
                {"alpha_vantage": avi.get_indicator, "yfinance": _returns("YF_INDICATORS")},
            ),
        ):
            result = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 30)
        self.assertEqual(result, "YF_INDICATORS")

    def test_alpha_vantage_indicator_empty_window_alone_yields_the_no_data_sentinel(self):
        # With no other vendor to try, the honest sentinel replaces the report
        # shape, and the reason rides along so "no rows in your window" is not
        # flattened into a bare "no data".
        import tradingagents.dataflows.alpha_vantage_indicator as avi

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage"}})
        with (
            mock.patch.object(avi, "_make_api_request", return_value="time,RSI\n2020-01-02,55.0\n"),
            self._route_method("get_indicators", {"alpha_vantage": avi.get_indicator}),
        ):
            result = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 30)
        self.assertIn("NO_DATA_AVAILABLE", result)
        self.assertIn("no rsi rows between", result)

    def test_alpha_vantage_vwma_reaches_the_vendor_that_can_compute_it(self):
        # Alpha Vantage has no VWMA endpoint and used to say so in prose, which
        # the router records as a successful report — so the chain stopped at
        # the vendor that cannot serve it while yfinance, which computes vwma
        # from OHLCV via stockstats, was never asked (#106).
        import tradingagents.dataflows.alpha_vantage_indicator as avi

        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        with self._route_method(
            "get_indicators",
            {"alpha_vantage": avi.get_indicator, "yfinance": _returns("YF_VWMA")},
        ):
            result = interface.route_to_vendor("get_indicators", "AAPL", "vwma", "2026-06-01", 30)
        self.assertEqual(result, "YF_VWMA")

    def test_core_category_still_raises_on_error(self):
        # A core category (single configured vendor) propagates the error so a
        # broken primary is loud, not silently degraded.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        with self._route({"yfinance": _raises(ValueError("boom"))}), self.assertRaises(ValueError):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")


if __name__ == "__main__":
    unittest.main()


@pytest.mark.unit
class EmptyVendorRegistryTests(unittest.TestCase):
    """An empty vendor registry must raise the classified
    VendorNotConfiguredError, not a bare RuntimeError outside the
    taxonomy (#32)."""

    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def test_empty_registry_raises_classified_error(self):
        from tradingagents.dataflows.errors import VendorNotConfiguredError

        set_config({"data_vendors": {"core_stock_apis": "default"}})
        with (
            mock.patch.dict(interface.VENDOR_METHODS, {"get_stock_data": {}}, clear=False),
            self.assertRaises(VendorNotConfiguredError),
        ):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-02")


# --- the router's per-vendor throttle latch (#114) ---
#
# One vendor's 429 used to be re-discovered by every tool call that reached it
# in the same cycle. The rate-limit lane is the one point every vendor's
# throttle passes through, so the memory lives there: a vendor that has just
# raised VendorRateLimitError is skipped in its turn for a short window and the
# chain goes on in its configured order. (Config isolation, the latch reset and
# frozen_clock come from conftest.)


_throttled = _raises(VendorRateLimitError("429"))


def _chain(method, vendors):
    return mock.patch.dict(interface.VENDOR_METHODS, {method: vendors}, clear=False)


def _stock(symbol="AAPL"):
    return interface.route_to_vendor("get_stock_data", symbol, "2026-01-01", "2026-01-10")


@pytest.mark.unit
def test_a_vendor_that_just_throttled_is_skipped_and_the_chain_goes_on():
    # Discrimination for the whole feature: without the latch the throttled
    # primary is called again on the second routing and the count fails.
    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
    yf = mock.Mock(side_effect=_throttled)
    with _chain("get_stock_data", {"yfinance": yf, "alpha_vantage": _returns("AV_DATA")}):
        assert _stock("AAPL") == "AV_DATA"
        assert _stock("MSFT") == "AV_DATA"
    assert yf.call_count == 1  # discovered once; the second routing never contacted it


@pytest.mark.unit
def test_the_latch_is_per_vendor_across_methods():
    # A quota is spent per key, not per endpoint: a throttle met on one method
    # spares the same vendor's other methods too.
    set_config(
        {
            "data_vendors": {
                "core_stock_apis": "alpha_vantage,yfinance",
                "news_data": "alpha_vantage,yfinance",
            }
        }
    )
    av_news = mock.Mock(side_effect=_throttled)
    av_stock = mock.Mock(side_effect=_returns("AV_DATA"))
    with (
        _chain("get_news", {"alpha_vantage": av_news, "yfinance": _returns("YF_NEWS")}),
        _chain("get_stock_data", {"alpha_vantage": av_stock, "yfinance": _returns("YF_DATA")}),
    ):
        news = interface.route_to_vendor("get_news", "AAPL", "2026-01-01", "2026-01-10")
        assert news == "YF_NEWS"
        assert _stock() == "YF_DATA"
    av_stock.assert_not_called()


@pytest.mark.unit
def test_the_vendor_latch_holds_for_its_window_and_lets_go_on_the_deadline(frozen_clock):
    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
    yf = mock.Mock(side_effect=[VendorRateLimitError("429"), "YF_DATA"])
    with _chain("get_stock_data", {"yfinance": yf, "alpha_vantage": _returns("AV_DATA")}):
        _stock()
        frozen_clock["t"] += THROTTLE_LATCH_TTL_S - 1
        assert _stock() == "AV_DATA"
        assert yf.call_count == 1
        # Exclusive bound, like the yfinance latch: at the deadline itself the
        # vendor is contacted again, and its answer is what the caller gets.
        frozen_clock["t"] += 1
        assert _stock() == "YF_DATA"


@pytest.mark.unit
def test_an_answer_clears_the_vendor_latch():
    # A deadline recorded by a sibling thread while this call was in flight is
    # gone once the vendor answers.
    set_config({"data_vendors": {"core_stock_apis": "yfinance"}})

    def arm_then_answer(symbol, *a, **k):
        VENDOR_THROTTLE_LATCH.arm("yfinance")
        return "YF_DATA"

    with _chain("get_stock_data", {"yfinance": arm_then_answer}):
        assert _stock() == "YF_DATA"
    assert VENDOR_THROTTLE_LATCH.remaining_s("yfinance") is None


@pytest.mark.unit
def test_a_latched_core_chain_still_fails_loud():
    # Same verdict the chain reaches after contacting the vendor and being
    # refused again: a core category exhausted by nothing but rate limits
    # surfaces the throttle — not a bare RuntimeError, and not silence.
    set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
    with _chain("get_stock_data", {"yfinance": _throttled}):
        with pytest.raises(VendorRateLimitError):
            _stock()
        with pytest.raises(VendorRateLimitError, match="skipped without contacting"):
            _stock()


@pytest.mark.unit
def test_a_latched_optional_chain_still_degrades_to_the_sentinel():
    set_config({"data_vendors": {"options_data": "deribit"}})
    deribit = mock.Mock(side_effect=_throttled)
    with _chain("get_options_market", {"deribit": deribit}):
        first = interface.route_to_vendor("get_options_market", "BTC", "2026-01-01")
        second = interface.route_to_vendor("get_options_market", "BTC", "2026-01-01")
    assert first.startswith("DATA_UNAVAILABLE") and second.startswith("DATA_UNAVAILABLE")
    assert deribit.call_count == 1


@pytest.mark.unit
def test_only_a_raised_throttle_arms_the_vendor_latch():
    # Deribit renders a partial throttle into its report and raises only when
    # every request it made was refused; a report is an answer, so the vendor
    # is contacted again next time. A failure outside the rate-limit lane
    # arms nothing either.
    set_config({"data_vendors": {"options_data": "deribit"}})
    partial = mock.Mock(side_effect=_returns("DVOL unavailable (rate limited); skew: 3.1"))
    with _chain("get_options_market", {"deribit": partial}):
        interface.route_to_vendor("get_options_market", "BTC", "2026-01-01")
        interface.route_to_vendor("get_options_market", "BTC", "2026-01-01")
    assert partial.call_count == 2

    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
    broken = mock.Mock(side_effect=_raises(ValueError("boom")))
    with _chain("get_stock_data", {"yfinance": broken, "alpha_vantage": _returns("AV_DATA")}):
        _stock()
        _stock()
    assert broken.call_count == 2


@pytest.mark.unit
def test_a_throttle_that_does_not_latch_the_vendor_leaves_it_contacted():
    # SoSoValue's rate-limit type says "throttled AND no usable cache for this
    # call", not "this client is refused": its sibling tools answer the same
    # throttle with a stale-cache report, which a latch would have turned into
    # DATA_UNAVAILABLE for the rest of the window — a different verdict, not
    # a cheaper one. The type carries that fact, so the router keeps asking.
    from tradingagents.dataflows.sosovalue_common import SoSoValueRateLimitError

    assert not SoSoValueRateLimitError.latches_vendor
    set_config({"data_vendors": {"crypto_etf_flows": "sosovalue"}})
    sosovalue = mock.Mock(side_effect=_raises(SoSoValueRateLimitError("429")))
    with _chain("get_etf_flows", {"sosovalue": sosovalue}):
        interface.route_to_vendor("get_etf_flows", "BTC", "2026-01-01")
        interface.route_to_vendor("get_etf_flows", "BTC", "2026-01-01")
    assert sosovalue.call_count == 2
    assert VENDOR_THROTTLE_LATCH.remaining_s("sosovalue") is None


@pytest.mark.unit
def test_yfinance_stands_off_at_its_own_boundary_not_at_the_router():
    # yfinance's latch lives in yf_retry. The indicator getter reads the OHLCV
    # cache (load_ohlcv) before it gets there, so a symbol whose bars are on
    # disk is still served while Yahoo is being stood off from; latched at
    # the router as well, the same call would be refused in front of that
    # cache — a different verdict, not a cheaper one. So the router never
    # latches yfinance: its rate-limit type says so, the getter keeps being
    # called, and a cache-served answer does not disturb the standing-off.
    import tradingagents.dataflows.stockstats_utils as su

    assert not su.YFinanceRateLimitError.latches_vendor

    set_config({"data_vendors": {"technical_indicators": "yfinance,alpha_vantage"}})
    su._YF_THROTTLE_LATCH.arm(su._YF_LATCH_KEY)  # what an exhausted ladder does

    def yfinance_indicators(symbol, *a, **k):
        # A cache hit never reaches yf_retry; a miss is refused there at once.
        if symbol == "CACHED":
            return "RSI FROM BARS ON DISK"
        return su.yf_retry(lambda: "never reached")

    def indicators(symbol):
        return interface.route_to_vendor("get_indicators", symbol, "rsi", "2026-06-01", 30)

    with _chain(
        "get_indicators", {"yfinance": yfinance_indicators, "alpha_vantage": _returns("AV_RSI")}
    ):
        assert indicators("UNCACHED") == "AV_RSI"
        assert indicators("CACHED") == "RSI FROM BARS ON DISK"
        assert indicators("UNCACHED") == "AV_RSI"  # the router did not latch yfinance
    assert VENDOR_THROTTLE_LATCH.remaining_s("yfinance") is None
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is not None


@pytest.mark.unit
def test_a_throttle_actually_met_outranks_a_latch_skip():
    # Chain order used to decide which throttle surfaced; a refusal the chain
    # met carries the vendor's own detail (a Retry-After), a skip describes a
    # request never sent.
    set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})
    VENDOR_THROTTLE_LATCH.arm("alpha_vantage")
    met = _raises(VendorRateLimitError("Retry-After: 42"))
    with (
        _chain("get_stock_data", {"alpha_vantage": _throttled, "yfinance": met}),
        pytest.raises(VendorRateLimitError, match="Retry-After: 42"),
    ):
        _stock()


_down = _raises(VendorUnavailableError("Yahoo Finance answered without data: HTTP 503"))


@pytest.mark.unit
class OutageVerdictTests(unittest.TestCase):
    """A vendor that was DOWN changes what a fallback's "no data" means.

    The no-data sentinel used to say "the symbol may be invalid, delisted, not
    covered" whenever any vendor said no data — including when the primary
    had answered an outage page or could not be reached, and only the
    fallback had been asked about the symbol at all. The agent reasons from
    that sentence (#142). Now the router remembers the outage past the chain
    and the sentinel says which vendor was down instead, under the same
    prefix every reader keys on.
    """

    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def test_a_down_primary_makes_a_fallbacks_no_data_unconfirmed(self):
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with (
            _chain("get_stock_data", {"yfinance": _down, "alpha_vantage": _no_data}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
        ):
            out = _stock()
        self.assertTrue(out.startswith("NO_DATA_AVAILABLE:"))
        self.assertIn("vendor 'yfinance' was unavailable", out)
        self.assertIn("HTTP 503", out)  # what the down vendor said rides along
        self.assertIn("no rows", out)  # and so does the fallback's own detail
        self.assertIn("unconfirmed rather than invalid", out)
        self.assertIn("the other configured vendor(s) had no usable data (no rows)", out)
        self.assertNotIn("may be invalid", out)
        # The shared tail every reader keys on is the same literal as the
        # symbol-wording variant's.
        self.assertTrue(out.endswith("report that data is unavailable for this symbol."))
        # The outage lane logs without a traceback: a vendor down is not a bug.
        self.assertTrue(all(r.exc_info is None for r in cm.records))

    def test_an_unreachable_primary_counts_as_down_but_only_by_name(self):
        # The transport lane is the same fact for the verdict: a reset or a
        # timeout (every requests exception is an OSError) never asked the
        # vendor about the symbol either. Its text stays out of the sentinel:
        # a requests message quotes the request URL, API key and all.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        reset = _raises(
            requests.ConnectionError(
                "HTTPSConnectionPool(host='x'): Max retries exceeded with url: "
                "/query?symbol=AAPL&apikey=SECRET-KEY"
            )
        )
        with _chain("get_stock_data", {"yfinance": reset, "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn(
            "vendor 'yfinance' was unavailable (could not be reached: ConnectionError)", out
        )
        self.assertNotIn("SECRET-KEY", out)
        self.assertNotIn("may be invalid", out)

    def test_a_4xx_the_boundary_left_alone_is_the_vendor_answering_not_down(self):
        # A requests.HTTPError is an OSError too, but a 404 means the vendor
        # answered about this request — its boundary left the status alone on
        # purpose — so the no-data verdict stands as one on the symbol.
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})
        not_found = _raises(
            requests.HTTPError(
                "404 Client Error: Not Found for url: /query",
                response=types.SimpleNamespace(status_code=404),
            )
        )
        with _chain("get_stock_data", {"alpha_vantage": not_found, "yfinance": _no_data}):
            out = _stock()
        self.assertIn("may be invalid", out)
        self.assertNotIn("was unavailable", out)

    def test_the_status_is_read_off_the_exception_not_its_library(self):
        # yfinance fetches through curl_cffi, whose HTTPError is an OSError
        # but not requests' — and its un-hidden window lets a 401/403 (Yahoo
        # refusing this client) and a 5xx out raw. Both are the vendor being
        # unavailable, said by status; a class check would have called them
        # "could not be reached" (false: Yahoo answered) or, worse, pinned
        # the rule to one library.
        from curl_cffi.requests import exceptions as curl_exceptions

        def _curl(status):
            return _raises(
                curl_exceptions.HTTPError(
                    f"HTTP Error {status}", 0, types.SimpleNamespace(status_code=status)
                )
            )

        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with _chain("get_stock_data", {"yfinance": _curl(503), "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn("vendor 'yfinance' was unavailable (answered HTTP 503)", out)
        with _chain("get_stock_data", {"yfinance": _curl(403), "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn("was unavailable (answered HTTP 403, refusing this client)", out)
        self.assertNotIn("could not be reached", out)
        # And a status that is an answer about the request is not an outage,
        # whichever library raised it.
        with _chain("get_stock_data", {"yfinance": _curl(404), "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn("may be invalid", out)

    def test_a_requests_exception_that_is_a_value_error_is_not_an_outage(self):
        # MissingSchema / InvalidURL are code bugs dressed as requests
        # exceptions (they are ValueError too): the traceback lane keeps them,
        # and the sentinel must not say the vendor was unreachable.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        bug = _raises(requests.exceptions.MissingSchema("Invalid URL 'query': No scheme"))
        with _chain("get_stock_data", {"yfinance": bug, "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn("may be invalid", out)
        self.assertNotIn("was unavailable", out)

    def test_what_the_down_vendor_said_is_flattened_before_the_model_reads_it(self):
        # yfinance's mapping quotes the library's exception, and the sentinel
        # is an LLM prompt: a fragment cannot open a heading or a table row.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        forged = _raises(
            VendorUnavailableError("Yahoo Finance answered without data:\n## forged heading | cell")
        )
        with _chain("get_stock_data", {"yfinance": forged, "alpha_vantage": _no_data}):
            out = _stock()
        self.assertIn("forged heading", out)
        self.assertNotIn("##", out)
        self.assertNotIn("|", out)
        self.assertNotIn("\n", out)

    def test_the_outage_wording_does_not_depend_on_chain_order(self):
        # "Any vendor in the chain", not "the primary": the fallback being the
        # one that was down leaves the primary's no-data just as unconfirmed.
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})
        with _chain("get_stock_data", {"alpha_vantage": _no_data, "yfinance": _down}):
            out = _stock()
        self.assertIn("vendor 'yfinance' was unavailable", out)
        self.assertNotIn("may be invalid", out)

    def test_a_bug_beside_a_no_data_verdict_keeps_the_symbol_wording(self):
        # The counterpart: only an outage or a transport failure earns the
        # new wording. A vendor that raised a plain error DID answer, so the
        # no-data verdict stands as one on the symbol — and the error stays
        # visible in the logs as before (#989).
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with (
            _chain(
                "get_stock_data",
                {"yfinance": _raises(ValueError("boom")), "alpha_vantage": _no_data},
            ),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
        ):
            out = _stock()
        self.assertIn("may be invalid", out)
        self.assertNotIn("was unavailable", out)
        self.assertIn("boom", "\n".join(cm.output))


@pytest.mark.unit
class CallerErrorPrecedenceTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def test_a_callers_indicator_typo_outranks_a_vendors_missing_key(self):
        # Chain alpha_vantage,yfinance with no Alpha Vantage key and an
        # indicator name yfinance does not compute: the first error the chain
        # met used to win, so the run aborted on a missing key where the
        # caller's typo — the thing the tool wrapper renders as one line of
        # report text — was the root cause (#137). The missing key is not
        # lost: it is still in the logs.
        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        no_key = _raises(VendorNotConfiguredError("ALPHA_VANTAGE_API_KEY is not set"))
        typo = _raises(UnsupportedIndicatorError("no indicator named 'rsii'"))
        with (
            _chain("get_indicators", {"alpha_vantage": no_key, "yfinance": typo}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
            pytest.raises(UnsupportedIndicatorError, match="rsii"),
        ):
            interface.route_to_vendor("get_indicators", "AAPL", "rsii", "2026-06-01", 30)
        self.assertIn("ALPHA_VANTAGE_API_KEY is not set", "\n".join(cm.output))

    def test_a_down_vendor_outranks_the_callers_indicator_error(self):
        # The exception to the rule above: when the vendor that failed was
        # DOWN, the name may be one it computes, so the outage is the fact to
        # surface — and the typo stays in the logs instead.
        set_config({"data_vendors": {"technical_indicators": "alpha_vantage,yfinance"}})
        typo = _raises(UnsupportedIndicatorError("no indicator named 'x'"))
        with (
            _chain("get_indicators", {"alpha_vantage": _down, "yfinance": typo}),
            self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm,
            pytest.raises(VendorUnavailableError, match="HTTP 503"),
        ):
            interface.route_to_vendor("get_indicators", "AAPL", "x", "2026-06-01", 30)
        self.assertIn("no indicator named 'x'", "\n".join(cm.output))
