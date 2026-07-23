"""Farside spot-ETF flow vendor: table parsing (negatives, thousands, blanks,
structure mismatch), per-UTC-day caching with stale fallback, lookahead-safe
windowing, report formatting, and router integration.

All network access is mocked and the parser runs against a trimmed local fixture,
so these run without a network connection.
"""

import os
from unittest import mock

import pytest
import requests

from tradingagents.dataflows import farside, interface
from tradingagents.dataflows.config import set_config

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


BTC_HTML = _fixture("farside_btc.html")
# Parsed once for the render tests (the fixture is the source of truth for the
# expected numbers below).
RECORDS = farside._parse_flow_table(BTC_HTML, "BTC")


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseFlowValue:
    def test_parenthesised_negative(self):
        assert farside._parse_flow_value("(44.5)") == -44.5

    def test_thousands_separator(self):
        assert farside._parse_flow_value("1,119.9") == 1119.9

    def test_blank_and_dash_are_zero(self):
        assert farside._parse_flow_value("") == 0.0
        assert farside._parse_flow_value("-") == 0.0
        assert farside._parse_flow_value("—") == 0.0

    def test_plain_number(self):
        assert farside._parse_flow_value("209.4") == 209.4

    def test_garbage_raises(self):
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_value("abc")


# --------------------------------------------------------------------------- #
# Table parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseTable:
    def test_parses_all_data_rows_excluding_summary(self):
        # 06/07/08/09 Jul are data rows; the "Average" summary row is excluded.
        dates = [r["date"] for r in RECORDS]
        assert dates == ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]

    def test_negatives_thousands_and_blanks(self):
        by_date = {r["date"]: r for r in RECORDS}
        # redFont parenthesised negative
        assert by_date["2026-07-06"]["issuers"]["GBTC"] == -44.5
        # blank BTC cell -> 0.0
        assert by_date["2026-07-07"]["issuers"]["BTC"] == 0.0
        # thousands separator
        assert by_date["2026-07-08"]["issuers"]["IBIT"] == 1119.9
        # Total column
        assert by_date["2026-07-08"]["total"] == 1214.9

    def test_issuer_names_from_header(self):
        assert set(RECORDS[0]["issuers"]) == {"IBIT", "FBTC", "GBTC", "BTC"}

    def test_missing_table_raises(self):
        with pytest.raises(farside.FarsideError, match="No ETF flow table"):
            farside._parse_flow_table("<html><body><p>nope</p></body></html>", "BTC")

    def test_no_data_rows_raises(self):
        html = '<table class="etf"><tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr></table>'
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_table(html, "BTC")

    def test_ragged_row_raises(self):
        # First data row sets 4 columns; the second has 3 -> structural mismatch.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "<tr><td>07 Jul 2026</td><td>1.0</td><td>2.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError, match="expected"):
            farside._parse_flow_table(html, "BTC")

    def test_unparseable_value_raises(self):
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>garbage</td><td>3.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_table(html, "BTC")

    def test_unparseable_month_raises(self):
        # Passes _DATE_RE ([A-Za-z]{3}) but is not a real month: the locale-safe
        # parser must raise rather than silently mis-parse.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Xyz 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_table(html, "BTC")


# --------------------------------------------------------------------------- #
# Rendering + lookahead
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRender:
    def _render(self, curr_date, look_back_days=30):
        with mock.patch.object(
            farside, "_load_flows", return_value=(RECORDS, "2026-07-09T00:00:00Z", False)
        ):
            return farside.get_etf_flow_data("BTC", curr_date, look_back_days)

    def test_lookahead_drops_future_rows(self):
        out = self._render("2026-07-08")
        assert "2026-07-09" not in out  # future row filtered
        assert "**Latest (2026-07-08):** +1214.9 net" in out

    def test_cumulative_streak_and_breakdown(self):
        out = self._render("2026-07-08")
        # 216.9 + 29.9 + 1214.9 = 1461.7
        assert "+1461.7" in out
        assert "3-day inflow" in out
        # latest-day leaders ranked by |flow|
        assert "IBIT +1119.9" in out
        assert "| 2026-07-08 | +1214.9 |" in out

    def test_unsupported_asset_reports_no_signal(self):
        # Does not touch the network/loader — returns a clear "no signal" string.
        out = farside.get_etf_flow_data("SOL", "2026-07-08", 30)
        assert "no spot-ETF flow signal" in out

    def test_empty_window(self):
        out = self._render("2020-01-01")
        assert "No ETF flow rows on or before 2020-01-01" in out


# --------------------------------------------------------------------------- #
# Caching (per UTC day, with stale fallback)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCache:
    def _use_tmp_cache(self, tmp_path):
        set_config({"data_cache_dir": str(tmp_path)})

    def test_non_dict_cache_is_ignored(self, tmp_path):
        # A tampered/corrupt cache whose top-level JSON is not an object must be
        # treated as a miss, not crash on .get().
        self._use_tmp_cache(tmp_path)
        path = tmp_path / "farside_btc_2026-07-23.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert farside._read_cache(str(path)) is None

    def test_same_utc_day_reuses_cache(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-23")
        calls = {"n": 0}

        def _fetch(asset):
            calls["n"] += 1
            return BTC_HTML

        monkeypatch.setattr(farside, "_request_html", _fetch)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        farside.get_etf_flow_data("BTC", "2026-07-09")
        assert calls["n"] == 1  # second call served from the same-day cache

    def test_cross_utc_day_refetches(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        calls = {"n": 0}

        def _fetch(asset):
            calls["n"] += 1
            return BTC_HTML

        monkeypatch.setattr(farside, "_request_html", _fetch)
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-23")
        farside.get_etf_flow_data("BTC", "2026-07-09")
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-24")
        farside.get_etf_flow_data("BTC", "2026-07-09")
        assert calls["n"] == 2

    def test_failure_without_cache_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-23")
        monkeypatch.setattr(
            farside,
            "_request_html",
            mock.Mock(side_effect=requests.RequestException("boom")),
        )
        with pytest.raises(farside.FarsideError):
            farside.get_etf_flow_data("BTC", "2026-07-09")
        assert not [f for f in os.listdir(tmp_path) if f.startswith("farside_")]

    def test_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        # Day 1: successful fetch populates the cache.
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-23")
        monkeypatch.setattr(farside, "_request_html", lambda asset: BTC_HTML)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # Day 2: fetch fails -> stale fallback to day 1's snapshot.
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-24")
        monkeypatch.setattr(
            farside,
            "_request_html",
            mock.Mock(side_effect=requests.RequestException("boom")),
        )
        out = farside.get_etf_flow_data("BTC", "2026-07-09")
        assert "STALE by 1 days" in out  # 2026-07-24 minus fetched 2026-07-23
        assert "+1214.9" in out  # still shows the cached data

    def test_stale_cache_beyond_cap_degrades(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        # Day 1: successful fetch stamps the snapshot at 2026-07-01.
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-07-01")
        monkeypatch.setattr(farside, "_request_html", lambda asset: BTC_HTML)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # A month later fetch keeps failing -> the only cache is 31 days stale,
        # past the 14-day cap -> refuse to serve it, raise so the router degrades.
        monkeypatch.setattr(farside, "_utc_today", lambda: "2026-08-01")
        monkeypatch.setattr(
            farside,
            "_request_html",
            mock.Mock(side_effect=requests.RequestException("boom")),
        )
        with pytest.raises(farside.FarsideError, match="cap"):
            farside.get_etf_flow_data("BTC", "2026-07-09")


# --------------------------------------------------------------------------- #
# Router integration
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouting:
    def test_category_routes_to_farside(self):
        assert interface.get_category_for_method("get_etf_flows") == "etf_flows"
        set_config({"data_vendors": {"etf_flows": "farside"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"farside": lambda *a, **k: "ETF_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-08", 30)
        assert out == "ETF_OK"

    def test_optional_category_degrades_to_sentinel(self):
        # etf_flows is optional: a vendor failure degrades to a sentinel instead
        # of aborting the run.
        set_config({"data_vendors": {"etf_flows": "farside"}})

        def _boom(*a, **k):
            raise farside.FarsideError("Farside unavailable")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"farside": _boom}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-08", 30)
        assert "DATA_UNAVAILABLE" in out
