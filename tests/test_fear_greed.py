"""Crypto Fear & Greed vendor (alternative.me): unix-timestamp -> UTC date
conversion, 7d/30d change computation, lookahead-safe filtering, malformed /
empty payload handling, report formatting, and router integration.

All API access is mocked, so these run without a network connection.
"""

import calendar
from datetime import datetime, timedelta
from unittest import mock

import pytest
import requests

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

    def test_zero_look_back_clamps_to_default_window(self):
        # A hallucinated look_back_days=0 must fall back to the default window, not
        # collapse to a 0-day window (which, unless a reading lands exactly on
        # curr_date, degrades to the single-latest fallback). Symmetric with the
        # None/negative clamp: the report is identical to the default render.
        with mock.patch.object(fear_greed, "_request", return_value=_PAYLOAD):
            zero = fear_greed.get_fear_greed_data("2026-07-23", 0)
            default = fear_greed.get_fear_greed_data(
                "2026-07-23", fear_greed.DEFAULT_LOOKBACK_DAYS
            )
        assert zero == default
        assert "no readings within" not in zero


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

    def test_non_zero_padded_curr_date_does_not_leak_future_readings(self):
        # A non-zero-padded curr_date ("2026-7-8") must be normalized BEFORE the
        # lookahead filter: a raw lexical compare would admit 2026-07-09 because
        # '2026-07-09' <= '2026-7-8' is True, leaking a future reading.
        payload = {
            "data": [
                _row("2026-07-09", 99, "Extreme Greed"),  # after the intended date
                _row("2026-07-08", 31, "Fear"),
            ]
        }
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-7-8", 30)  # intended 2026-07-08
        assert "2026-07-09" not in out  # future reading still dropped
        assert "**Latest (2026-07-08):** 31" in out
        assert "Window ending 2026-07-08" in out  # header shows the canonical date


@pytest.mark.unit
class TestDeltaAnchoring:
    def test_deltas_anchor_on_curr_date_not_the_latest_reading(self):
        # Anchoring on the latest reading lets the "7d" window float backwards
        # whenever the series lags: with the newest reading 4 days behind
        # curr_date, "vs 7d" would silently span curr_date-11 -> curr_date-4.
        # Anchored on curr_date, the reference is the reading at or before
        # curr_date-7 (2026-07-16), not latest-7 (2026-07-12).
        payload = {
            "data": [
                _row("2026-07-19", 40, "Fear"),  # latest, 4 days behind curr_date
                _row("2026-07-16", 30, "Fear"),  # curr_date - 7
                _row("2026-07-12", 10, "Extreme Fear"),  # latest - 7
            ]
        }
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "**vs 7d:** +10 (from 30 on 2026-07-16)" in out

    def test_frozen_series_is_distinguished_from_missing_history(self):
        # The newest reading IS the 7d reference point: the series has not
        # advanced past curr_date-7. That is a frozen series (a live-data
        # warning), NOT a fetch-reach artifact ("insufficient history"), and the
        # two must not collapse to one string.
        payload = {"data": [_row("2026-07-16", 28, "Fear")]}
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert (
            "**vs 7d:** n/a (no reading between then and 2026-07-16; series has not updated)"
            in out
        )
        # vs 30d has no reading reaching that far back at all -> the other string.
        assert "**vs 30d:** n/a (insufficient history)" in out

    def test_data_lag_is_flagged(self):
        # A successful fetch says nothing about whether alternative.me published;
        # this vendor is uncached, so there is no fetched_at staleness check.
        payload = {"data": [_row("2026-07-19", 40, "Fear")]}
        with mock.patch.object(fear_greed, "_request", return_value=payload):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "Data lag" in out
        assert "4 days before 2026-07-23" in out

    def test_no_lag_caveat_when_current(self):
        with mock.patch.object(fear_greed, "_request", return_value=_PAYLOAD):
            out = fear_greed.get_fear_greed_data("2026-07-23", 30)
        assert "Data lag" not in out


@pytest.mark.unit
class TestRequestWiring:
    """Exercise _request itself — every other test mocks it away, so without
    these the limit sizing, query params and retry could all be wrong silently."""

    def test_sends_limit_and_format_and_returns_payload(self):
        response = mock.Mock()
        response.json.return_value = _PAYLOAD
        response.raise_for_status.return_value = None
        with mock.patch.object(fear_greed.requests, "get", return_value=response) as get:
            assert fear_greed._request(75) == _PAYLOAD
        _, kwargs = get.call_args
        assert kwargs["params"] == {"limit": 75, "format": "json"}
        assert kwargs["timeout"] == fear_greed.REQUEST_TIMEOUT

    def test_limit_covers_window_plus_comparison_buffer(self):
        # Guards _FETCH_BUFFER_DAYS: a 0 buffer would leave no 30-days-ago
        # reference point and every test above would still pass.
        seen = {}

        def _capture(limit):
            seen["limit"] = limit
            return _PAYLOAD

        with mock.patch.object(fear_greed, "_request", side_effect=_capture):
            fear_greed.get_fear_greed_data("2026-07-23", 7)
        assert seen["limit"] == 30 + fear_greed._FETCH_BUFFER_DAYS

    def test_look_back_days_clamped_before_sizing_the_fetch(self):
        # An untrusted look_back_days must not size an unbounded outbound fetch
        # (unlike Farside, where it only filters an already-scraped page): it is
        # clamped to MAX_LOOKBACK_DAYS before the limit is computed.
        seen = {}

        def _capture(limit):
            seen["limit"] = limit
            return _PAYLOAD

        with mock.patch.object(fear_greed, "_request", side_effect=_capture):
            fear_greed.get_fear_greed_data("2026-07-23", 100000)
        assert seen["limit"] == fear_greed.MAX_LOOKBACK_DAYS + fear_greed._FETCH_BUFFER_DAYS

    def test_retries_once_then_succeeds(self):
        response = mock.Mock()
        response.json.return_value = _PAYLOAD
        response.raise_for_status.return_value = None
        with (
            mock.patch.object(fear_greed.time, "sleep") as sleep,
            mock.patch.object(
                fear_greed.requests,
                "get",
                side_effect=[requests.RequestException("blip"), response],
            ) as get,
        ):
            assert fear_greed._request(75) == _PAYLOAD
        assert get.call_count == 2
        sleep.assert_called_once_with(fear_greed._RETRY_DELAY_SECONDS)

    def test_gives_up_after_retry_with_typed_error(self):
        with (
            mock.patch.object(fear_greed.time, "sleep"),
            mock.patch.object(
                fear_greed.requests, "get", side_effect=requests.RequestException("down")
            ) as get,
            pytest.raises(fear_greed.FearGreedError, match="unreachable"),
        ):
            fear_greed._request(75)
        assert get.call_count == fear_greed._RETRY_ATTEMPTS

    def test_non_object_payload_raises_typed_error(self):
        # A CDN/WAF error page can decode as valid JSON that is not an object;
        # without the guard this surfaced as a bare AttributeError.
        response = mock.Mock()
        response.json.return_value = [1, 2, 3]
        response.raise_for_status.return_value = None
        with (
            mock.patch.object(fear_greed.requests, "get", return_value=response),
            pytest.raises(fear_greed.FearGreedError, match="expected an object"),
        ):
            fear_greed._request(75)


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

    def test_non_list_data_raises_typed_error(self):
        # A truthy non-list ({"data": true}, e.g. a CDN/WAF interception) would
        # reach `for row in data` and raise a bare TypeError outside the module's
        # FearGreedError contract; it must surface as the typed error instead.
        with (
            mock.patch.object(fear_greed, "_request", return_value={"data": True}),
            pytest.raises(fear_greed.FearGreedError, match="expected a list"),
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
