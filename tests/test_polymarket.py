"""Polymarket prediction-market vendor: forward-looking filtering, volume
ranking, formatting, graceful degradation, and router integration.

All API access is mocked, so these run without a network connection.
"""

import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface, polymarket
from tradingagents.dataflows.config import set_config


def _market(question, prob, *, volume, end_date, closed=False, wk=None):
    return {
        "question": question,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{prob}", "{round(1 - prob, 4)}"]',
        "volumeNum": volume,
        "endDate": end_date,
        "closed": closed,
        "oneWeekPriceChange": wk,
    }


# One event with a mix: a high-volume open market, a closed one, a past-dated
# one, and a lower-volume open one. Far-future / far-past dates keep the test
# independent of the real clock.
_SEARCH = {
    "events": [
        {
            "markets": [
                _market(
                    "Open big?", 0.76, volume=5_000_000, end_date="2030-12-31T00:00:00Z", wk=-0.045
                ),
                _market(
                    "Resolved already?",
                    1.0,
                    volume=9_000_000,
                    end_date="2030-12-31T00:00:00Z",
                    closed=True,
                ),
                _market("Past event?", 0.5, volume=8_000_000, end_date="2020-01-01T00:00:00Z"),
                _market("Open small?", 0.30, volume=1_000, end_date="2030-06-30T00:00:00Z"),
            ]
        }
    ]
}


@pytest.mark.unit
class PolymarketFilterTests(unittest.TestCase):
    def test_closed_and_past_markets_are_excluded(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Open big?", out)
        self.assertIn("Open small?", out)
        self.assertNotIn("Resolved already?", out)  # closed
        self.assertNotIn("Past event?", out)  # endDate in the past

    def test_ranked_by_volume(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertLess(out.index("Open big?"), out.index("Open small?"))

    def test_limit_caps_results(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=1)
        self.assertIn("Open big?", out)
        self.assertNotIn("Open small?", out)


@pytest.mark.unit
class PolymarketFormatTests(unittest.TestCase):
    def test_probability_volume_and_weekly_change_render(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Yes 76%", out)
        self.assertIn("$5,000,000 volume", out)
        self.assertIn("resolves 2030-12-31", out)
        self.assertIn("1-week -4.5pp", out)  # -0.045 -> -4.5pp

    def test_weekly_change_omitted_when_absent(self):
        # "Open small?" has wk=None -> no 1-week clause on its line.
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        small_line = next(ln for ln in out.splitlines() if "Open small?" in ln)
        self.assertNotIn("1-week", small_line)

    def test_no_matches_reports_clearly(self):
        with mock.patch.object(polymarket, "_request", return_value={"events": []}):
            out = polymarket.get_prediction_markets("obscure ticker", limit=6)
        self.assertIn("No open prediction markets", out)


@pytest.mark.unit
class PolymarketResilienceTests(unittest.TestCase):
    def test_network_error_degrades_gracefully(self):
        # An external-service hiccup must not raise into the analyst.
        with mock.patch.object(
            polymarket, "_request", side_effect=requests.RequestException("boom")
        ):
            out = polymarket.get_prediction_markets("Fed rate cut")
        self.assertIn("unavailable", out.lower())
        self.assertIn("Fed rate cut", out)


@pytest.mark.unit
class TestMalformedMarketsOmitted:
    """Malformed vendor data must be omitted and disclosed, never rendered
    with a fabricated label or an impossible probability (#29)."""

    @staticmethod
    def _fetch(market):
        search = {"events": [{"markets": [market]}]}
        with mock.patch.object(polymarket, "_request", return_value=search):
            return polymarket.get_prediction_markets("anything", limit=10)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("outcomes", '["No"]'),  # two prices, one outcome
            ("outcomePrices", '["1.30", "-0.30"]'),  # probability outside [0, 1]
            ("outcomePrices", '["abc", "def"]'),  # unparsable price strings
        ],
    )
    def test_malformed_market_omitted_with_disclosure(self, field, value):
        market = _market("Malformed?", 0.30, volume=100, end_date="2030-12-31T00:00:00Z")
        market[field] = value
        out = self._fetch(market)
        assert "Malformed?" not in out
        assert "1 market(s) omitted" in out

    def test_unparseable_outcomes_never_fabricates_yes(self):
        # Force the market past the forward-looking filter so the render loop
        # itself is exercised: it must not invent a "Yes" label when the
        # outcomes list failed to parse (the first outcome could be "No").
        market = _market("Broken outcomes?", 0.30, volume=100, end_date="2030-12-31T00:00:00Z")
        market["outcomes"] = "not-json"
        with mock.patch.object(polymarket, "_is_forward_looking", return_value=True):
            out = self._fetch(market)
        assert "Yes" not in out.split("Live, market-implied")[1]
        assert "Broken outcomes?" not in out
        assert "1 market(s) omitted" in out

    def test_no_disclosure_when_all_markets_clean(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        assert "omitted" not in out


@pytest.mark.unit
class PolymarketRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_category_routes_to_polymarket(self):
        self.assertEqual(
            interface.get_category_for_method("get_prediction_markets"),
            "prediction_markets",
        )
        set_config({"data_vendors": {"prediction_markets": "polymarket"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_prediction_markets": {"polymarket": lambda *a, **k: "POLY_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_prediction_markets", "fed", 5)
        self.assertEqual(out, "POLY_OK")


if __name__ == "__main__":
    unittest.main()
