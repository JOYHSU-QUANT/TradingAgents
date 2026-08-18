"""Shared freshness-annotation helpers (#30): data_lag_note renders a
disclosure only when the newest row genuinely trails the analysis date,
live_snapshot_note only when the analysis date trails the wall clock, and both
degrade to "" on unparseable input — an annotation helper must never raise."""

import pytest

from tradingagents.dataflows.utils import data_lag_note, live_snapshot_note


@pytest.mark.unit
class TestDataLagNote:
    def test_stale_renders_note_with_dates_gap_and_source(self):
        note = data_lag_note("2026-08-01", "2026-08-18", 7, "FRED observation")
        assert "Data lag" in note
        assert "2026-08-01" in note
        assert "17 days" in note
        assert "2026-08-18" in note
        assert "FRED observation" in note
        assert "stale" in note

    def test_fresh_returns_empty(self):
        assert data_lag_note("2026-08-15", "2026-08-18", 7, "x") == ""

    def test_bound_is_inclusive(self):
        # Exactly max_lag_days is still fresh; one day past it is not.
        assert data_lag_note("2026-08-11", "2026-08-18", 7, "x") == ""
        assert data_lag_note("2026-08-10", "2026-08-18", 7, "x") != ""

    def test_latest_after_curr_date_is_not_a_lag(self):
        assert data_lag_note("2026-08-20", "2026-08-18", 7, "x") == ""

    @pytest.mark.parametrize(
        "latest,curr",
        [
            (None, "2026-08-18"),
            ("garbage", "2026-08-18"),
            ("2026-08-01", "not-a-date"),
            ("", ""),
        ],
    )
    def test_unparseable_input_degrades_to_empty(self, latest, curr):
        assert data_lag_note(latest, curr, 7, "x") == ""

    def test_iso_time_suffix_is_tolerated(self):
        note = data_lag_note("2026-08-01T12:00:00Z", "2026-08-18T00:00:00Z", 7, "row")
        assert "2026-08-01" in note
        assert "T12:00:00" not in note  # rendered as plain dates

    def test_singular_day_grammar(self):
        note = data_lag_note("2026-08-17", "2026-08-18", 0, "row")
        assert "1 day before" in note
        assert "1 days" not in note


@pytest.mark.unit
class TestLiveSnapshotNote:
    def test_backtest_date_renders_disclosure(self):
        note = live_snapshot_note(
            "2026-08-01", "prediction-market probabilities are", 2, today="2026-08-18"
        )
        assert "live values" in note
        assert "2026-08-18" in note
        assert "2026-08-01" in note
        assert "17 days earlier" in note
        assert "prediction-market probabilities are" in note

    def test_near_current_date_returns_empty(self):
        assert live_snapshot_note("2026-08-17", "x are", 2, today="2026-08-18") == ""

    def test_bound_is_inclusive(self):
        assert live_snapshot_note("2026-08-16", "x are", 2, today="2026-08-18") == ""
        assert live_snapshot_note("2026-08-15", "x are", 2, today="2026-08-18") != ""

    def test_future_curr_date_returns_empty(self):
        assert live_snapshot_note("2026-08-20", "x are", 2, today="2026-08-18") == ""

    @pytest.mark.parametrize("curr", [None, "garbage", ""])
    def test_unparseable_input_degrades_to_empty(self, curr):
        assert live_snapshot_note(curr, "x are", 2, today="2026-08-18") == ""

    def test_default_today_is_wall_clock(self):
        # A curr_date decades back must always trigger regardless of when the
        # suite runs; this is the only test allowed to touch the real clock.
        assert live_snapshot_note("2000-01-01", "x are", 2) != ""
