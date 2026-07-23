"""Crypto Fear & Greed vendor (alternative.me): unix-timestamp -> UTC date
conversion, 7d/30d change computation, lookahead-safe filtering, malformed /
empty payload handling, report formatting, and router integration.

All API access is mocked, so these run without a network connection.
"""

import calendar
from datetime import datetime, timedelta
from unittest import mock

import pytest

from tradingagents.dataflows import fear_greed, interface
from tradingagents.dataflows.config import set_config


def _ts(date_str: str) -> str:
    """UTC midnight unix seconds (as a string, like the API returns)."""
    return str(calendar.timegm(datetime.strptime(date_str, "%Y-%m-%d").timetuple()))


def _row(date_str, value, label):
    return {"timestamp": _ts(date_str), "value": str(value), "value_classification": label}


# Most-recent-first, like the real API. Latest, exactly 7d ago, exactly 30d ago.
_PAYLOAD = {
    "data": [
        _row("2026-07-23", 31, "Fear"),
        _row("2026-07-16", 25, "Extreme Fear"),
        _row("2026-06-23", 23, "Extreme Fear"),
    ],
    "metadata": {"error": None},
}


@pytest.mark.unit
class TestFormatting:
    def test_latest_and_deltas(self):
        with mock.patch.object(fear_greed, "_request", return_value=_PAYLOAD):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "**Latest (2026-07-23):** 31 — Fear" in out
        assert "**vs 7d:** +6 (from 25 on 2026-07-16)" in out
        assert "**vs 30d:** +8 (from 23 on 2026-06-23)" in out
        assert "| 2026-07-23 | 31 | Fear |" in out

    def test_unix_timestamp_converts_to_utc_date(self):
        with mock.patch.object(fear_greed, "_request", return_value=_PAYLOAD):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        # 30-days-ago row's unix timestamp must render as its UTC calendar date.
        assert "| 2026-06-23 |" in out

    def test_insufficient_history_reports_na(self):
        single = {"data": [_row("2026-07-23", 31, "Fear")]}
        with mock.patch.object(fear_greed, "_request", return_value=single):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "n/a" in out


@pytest.mark.unit
class TestWindow:
    def test_short_window_trims_table_but_keeps_deltas(self):
        # look_back_days bounds the displayed table, but the 7d/30d deltas keep
        # their fixed horizons and may reference a reading outside the window.
        payload = {
            "data": [
                _row("2026-07-23", 40, "Fear"),
                _row("2026-07-20", 35, "Fear"),
                _row("2026-06-23", 20, "Extreme Fear"),  # 30d ago, outside a 7d window
            ]
        }
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 7)
        assert "| 2026-06-23 |" not in out  # outside the 7-day window: not tabled
        assert "vs 30d:** +20 (from 20 on 2026-06-23)" in out  # but drives the delta
        assert "| 2026-07-20 |" in out  # in-window reading is shown

    def test_table_truncates_to_max_rows(self):
        # A window longer than MAX_ROWS readings is truncated to the most recent
        # MAX_ROWS, with a note stating how many the window held.
        start = datetime(2026, 5, 1)
        n = fear_greed.MAX_ROWS + 10  # 50 consecutive daily readings
        data = [
            _row((start + timedelta(days=i)).strftime("%Y-%m-%d"), 30 + (i % 5), "Fear")
            for i in range(n)
        ]
        curr_date = (start + timedelta(days=n - 1)).strftime("%Y-%m-%d")
        with mock.patch.object(fear_greed, "_request", return_value={"data": data}):
            out = fear_greed.get_fear_greed_data(curr_date, n + 5)
        assert f"most recent {fear_greed.MAX_ROWS} of {n} readings" in out

    def test_no_readings_within_window_shows_latest(self):
        # Every reading predates the trailing window: window_points is empty, so
        # the latest available reading is still shown with a caveat note (the
        # table stays consistent with the summary above it).
        payload = {
            "data": [
                _row("2026-05-01", 30, "Fear"),
                _row("2026-04-01", 25, "Extreme Fear"),
            ]
        }
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 7)
        assert "no readings within the 7-day window" in out
        assert "| 2026-05-01 |" in out  # the latest available reading is still shown


@pytest.mark.unit
class TestLookahead:
    def test_future_readings_are_dropped(self):
        payload = {
            "data": [
                _row("2026-07-24", 99, "Extreme Greed"),  # after curr_date
                _row("2026-07-23", 31, "Fear"),
                _row("2026-07-16", 25, "Extreme Fear"),
            ]
        }
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "2026-07-24" not in out
        assert "**Latest (2026-07-23):** 31" in out


@pytest.mark.unit
class TestResilience:
    def test_empty_payload_raises(self):
        with (
            mock.patch.object(fear_greed, "_request", return_value={"data": []}),
            pytest.raises(fear_greed.FearGreedError),
        ):
            fear_greed.get_fear_greed_data("2026-07-23", 30)

    def test_malformed_row_raises(self):
        bad = {"data": [{"timestamp": _ts("2026-07-23"), "value": "N/A"}]}
        with (
            mock.patch.object(fear_greed, "_request", return_value=bad),
            pytest.raises(fear_greed.FearGreedError),
        ):
            fear_greed.get_fear_greed_data("2026-07-23", 30)


@pytest.mark.unit
class TestRouting:
    def test_category_routes_to_alternative_me(self):
        assert interface.get_category_for_method("get_fear_greed") == "crypto_sentiment"
        set_config({"data_vendors": {"crypto_sentiment": "alternative_me"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_fear_greed": {"alternative_me": lambda *a, **k: "FNG_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_fear_greed", "2026-07-23", 30)
        assert out == "FNG_OK"

    def test_optional_category_degrades_to_sentinel(self):
        set_config({"data_vendors": {"crypto_sentiment": "alternative_me"}})

        def _boom(*a, **k):
            raise fear_greed.FearGreedError("alternative.me unavailable")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_fear_greed": {"alternative_me": _boom}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_fear_greed", "2026-07-23", 30)
        assert "DATA_UNAVAILABLE" in out
