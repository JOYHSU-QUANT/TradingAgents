"""SoSoValue macro-calendar vendor: calendar and event-history parsing
(live-captured fixtures), the value/surprise arithmetic (unit-matched only),
per-event partial-failure semantics with the consecutive-network-failure
breaker, rolling-snapshot caching with stale fallback and read-side
validation, lookahead-safe rendering (scheduled rows never show an actual;
released figures only on or before curr_date), and router integration.

All network access is mocked and the parsers run against fixtures captured
from the real API, so these run without a network connection or a key.
"""

import json
import os
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import quote

import pytest
import requests

from tradingagents.dataflows import interface, sosovalue_common, sosovalue_macro
from tradingagents.dataflows.config import set_config

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_json(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# Live-captured API responses (see the module docstring's live-verified facts).
CAL_FIX = _fixture_json("sosovalue_macro_events.json")
CPI_FIX = _fixture_json("sosovalue_macro_history_cpi_yoy.json")
NFP_FIX = _fixture_json("sosovalue_macro_history_nfp.json")

TRACKED = sosovalue_macro.TRACKED_EVENTS


def _at(stamp: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in stamp else "%Y-%m-%d"
    return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)


def _row(date: str, actual: str, forecast: str, previous: str) -> dict:
    return {"date": date, "actual": actual, "forecast": forecast, "previous": previous}


def _histories(overrides=None, failed=(), unknown=()):
    """A full TRACKED_EVENTS partition: overrides win, failed/unknown are
    omitted, every other tracked event gets one old released row."""
    histories = {}
    for name in TRACKED:
        if name in failed or name in unknown:
            continue
        if overrides and name in overrides:
            histories[name] = overrides[name]
        else:
            histories[name] = [_row("2026-01-15", "1.0%", "1.1%", "0.9%")]
    return histories


def _snapshot(
    calendar=None,
    calendar_unusable=0,
    histories=None,
    events_failed=(),
    events_unknown=(),
    fetched_at="2026-08-11T00:00:00Z",
    stale=False,
):
    return sosovalue_macro._MacroSnapshot(
        calendar=calendar
        if calendar is not None
        else [{"date": "2026-08-11", "events": ["CPI (YoY)"]}],
        calendar_unusable=calendar_unusable,
        histories=_histories() if histories is None else histories,
        events_failed=list(events_failed),
        events_unknown=list(events_unknown),
        fetched_at=fetched_at,
        stale=stale,
    )


def _render(snapshot, curr_date="2026-08-11", look_back_days=None):
    with mock.patch.object(sosovalue_macro, "_load_snapshot", return_value=snapshot):
        return sosovalue_macro.get_economic_calendar_data(curr_date, look_back_days)


# --------------------------------------------------------------------------- #
# calendar parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseCalendar:
    def test_live_fixture_parses_ascending_with_names(self):
        rows, unusable = sosovalue_macro._parse_calendar(CAL_FIX["data"])
        assert unusable == 0
        assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
        assert rows[0]["date"] == "2026-08-10"
        assert "CPI (YoY)" in rows[1]["events"]

    def test_empty_calendar_raises(self):
        with pytest.raises(sosovalue_common.SoSoValueError, match="empty macro calendar"):
            sosovalue_macro._parse_calendar([])

    def test_oversized_calendar_raises(self):
        data = [
            {"date": f"2026-{m:02d}-{d:02d}", "events": ["CPI (YoY)"]}
            for m in range(1, 3)
            for d in range(1, 22)
        ]
        assert len(data) > sosovalue_macro.MAX_CALENDAR_ROWS
        with pytest.raises(sosovalue_common.SoSoValueError, match="day-rows"):
            sosovalue_macro._parse_calendar(data)

    def test_malformed_row_raises(self):
        with pytest.raises(sosovalue_common.SoSoValueError, match="Malformed"):
            sosovalue_macro._parse_calendar([{"date": "2026-08-11"}])
        with pytest.raises(sosovalue_common.SoSoValueError, match="Malformed"):
            sosovalue_macro._parse_calendar([{"date": "not-a-date", "events": []}])

    def test_duplicate_date_raises(self):
        data = [
            {"date": "2026-08-11", "events": ["CPI (YoY)"]},
            {"date": "2026-08-11", "events": ["PPI (MoM)"]},
        ]
        with pytest.raises(sosovalue_common.SoSoValueError, match="repeats a date"):
            sosovalue_macro._parse_calendar(data)

    def test_unusable_names_are_dropped_and_counted(self, caplog):
        data = [
            {
                "date": "2026-08-11",
                # empty, oversized, non-string, control character: all dropped.
                "events": ["CPI (YoY)", "", "x" * 61, 123, "bad\x01name"],
            }
        ]
        with caplog.at_level("WARNING"):
            rows, unusable = sosovalue_macro._parse_calendar(data)
        assert rows[0]["events"] == ["CPI (YoY)"]
        assert unusable == 4

    def test_duplicate_name_within_a_day_is_deduped_not_counted(self):
        data = [{"date": "2026-08-11", "events": ["CPI (YoY)", "CPI (YoY)"]}]
        rows, unusable = sosovalue_macro._parse_calendar(data)
        assert rows[0]["events"] == ["CPI (YoY)"]
        assert unusable == 0


# --------------------------------------------------------------------------- #
# event-history parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseEventRows:
    def test_cpi_fixture_parses_ascending_with_pending_row(self):
        rows = sosovalue_macro._parse_event_rows(CPI_FIX["data"], "CPI (YoY)")
        assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
        # The newest live row is the scheduled-but-unreleased print: actual "".
        assert rows[-1]["actual"] == ""
        assert rows[-1]["forecast"]

    def test_nfp_fixture_keeps_plain_negative_number_strings(self):
        rows = sosovalue_macro._parse_event_rows(NFP_FIX["data"], "Nonfarm Payrolls")
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-08-07"]["actual"] == "-23"

    def test_empty_history_raises_as_a_defensive_backstop(self):
        # An unknown/renamed name returns 200 + [] (live-verified).
        # _fetch_one_event pre-checks emptiness into events_unknown; this
        # raise only backstops a direct call so emptiness can never pass as
        # a valid history.
        with pytest.raises(sosovalue_common.SoSoValueError, match="renamed"):
            sosovalue_macro._parse_event_rows([], "CPI (YoY)")

    def test_malformed_or_unprintable_rows_raise(self):
        with pytest.raises(sosovalue_common.SoSoValueError, match="Malformed"):
            sosovalue_macro._parse_event_rows([{"date": "2026-08-07", "actual": "1%"}], "X")
        with pytest.raises(sosovalue_common.SoSoValueError, match="Malformed"):
            sosovalue_macro._parse_event_rows([_row("2026-08-07", "1%", "2%", None)], "X")
        with pytest.raises(sosovalue_common.SoSoValueError, match="Malformed"):
            sosovalue_macro._parse_event_rows([_row("2026-08-07", "1%\x00", "2%", "3%")], "X")

    def test_duplicate_dates_are_both_kept(self):
        # Live-verified on the NFP fixture: 2025-12-16 carries TWO prints (a
        # delayed release and its catch-up), and a last-wins collapse would
        # silently drop a real release plus its surprise.
        rows = sosovalue_macro._parse_event_rows(
            [_row("2026-08-07", "1%", "2%", "3%"), _row("2026-08-07", "9%", "2%", "3%")],
            "X",
        )
        assert [r["actual"] for r in rows] == ["1%", "9%"]
        nfp = sosovalue_macro._parse_event_rows(NFP_FIX["data"], "Nonfarm Payrolls")
        assert sum(1 for r in nfp if r["date"] == "2025-12-16") == 2


# --------------------------------------------------------------------------- #
# value parsing and surprises
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestValueArithmetic:
    def test_parse_value_shapes(self):
        assert sosovalue_macro._parse_value("3.5%") == (3.5, "%")
        assert sosovalue_macro._parse_value("-23") == (-23.0, "")
        assert sosovalue_macro._parse_value("1,234") == (1234.0, "")
        assert sosovalue_macro._parse_value("2.5K") == (2.5, "K")
        assert sosovalue_macro._parse_value("") is None
        assert sosovalue_macro._parse_value("abc") is None
        assert sosovalue_macro._parse_value("1 234") is None

    def test_surprise_is_unit_matched_subtraction(self):
        # Percent minus percent is percentage points, not a percent.
        assert sosovalue_macro._surprise_cell("3.5%", "3.8%") == "-0.3pp"
        # NFP-style plain numbers subtract as-is (live: -23 vs 85).
        assert sosovalue_macro._surprise_cell("-23", "85") == "-108"
        assert sosovalue_macro._surprise_cell("250K", "200K") == "+50K"
        # Raw-count magnitudes stay grouped fixed-point, never "+1e+06".
        assert sosovalue_macro._surprise_cell("1,236,000", "236,000") == "+1,000,000"

    def test_surprise_never_crosses_units_or_guesses(self):
        assert sosovalue_macro._surprise_cell("3.5%", "85") == "n/a"
        assert sosovalue_macro._surprise_cell("", "85") == "n/a"
        assert sosovalue_macro._surprise_cell("n.a.", "85") == "n/a"


# --------------------------------------------------------------------------- #
# _fetch_all: partial-failure semantics
# --------------------------------------------------------------------------- #
def _request_impl(history_by_name=None, history_error=None, error_names=()):
    """A _request stand-in: serves the calendar fixture and per-name histories."""
    calls = []

    def impl(path, params):
        calls.append(path)
        if path == "/macro/events":
            return CAL_FIX["data"]
        for name in TRACKED:
            if path == f"/macro/events/{quote(name, safe='')}/history":
                if name in error_names:
                    raise history_error
                if history_by_name and name in history_by_name:
                    return history_by_name[name]
                return CPI_FIX["data"]
        raise AssertionError(f"unexpected path {path}")

    impl.calls = calls
    return impl


@pytest.mark.unit
class TestFetchAll:
    def test_full_success_partitions_all_tracked_events(self, monkeypatch):
        monkeypatch.setattr(sosovalue_macro, "_request", _request_impl())
        payload = sosovalue_macro._fetch_all()
        assert set(payload["histories"]) == set(TRACKED)
        assert payload["events_failed"] == []
        assert payload["events_unknown"] == []
        assert len(payload["calendar"]) == len(CAL_FIX["data"])

    def test_an_empty_history_is_unknown_not_failed(self, monkeypatch):
        # 200 + [] means the name is renamed/dropped upstream: retrying cannot
        # heal it, so it must not land in events_failed (which arms the short
        # TTL and would hot-loop the whole sweep hourly, forever).
        monkeypatch.setattr(
            sosovalue_macro,
            "_request",
            _request_impl(history_by_name={"GDP (QoQ)": []}),
        )
        payload = sosovalue_macro._fetch_all()
        assert payload["events_unknown"] == ["GDP (QoQ)"]
        assert payload["events_failed"] == []
        assert set(payload["histories"]) == set(TRACKED) - {"GDP (QoQ)"}

    def test_a_rate_limit_drains_the_rest_of_the_sweep(self, monkeypatch):
        # The plan limit is per-key and per-minute: the first 429 proves every
        # remaining request would 429 too, so they are not sent at all.
        impl = _request_impl(
            history_error=sosovalue_common.SoSoValueRateLimitError("429"),
            error_names={TRACKED[2]},
        )
        monkeypatch.setattr(sosovalue_macro, "_request", impl)
        payload = sosovalue_macro._fetch_all()
        assert set(payload["events_failed"]) == set(TRACKED[2:])
        assert set(payload["histories"]) == set(TRACKED[:2])
        history_calls = [c for c in impl.calls if c != "/macro/events"]
        assert len(history_calls) == 3  # two successes + the one 429

    def test_a_first_request_429_keeps_its_rate_limit_type(self, monkeypatch):
        # A quota trip that drains the whole sweep must stay a rate-limit
        # error, not masquerade as structural breakage.
        impl = _request_impl(
            history_error=sosovalue_common.SoSoValueRateLimitError("429"),
            error_names=set(TRACKED),
        )
        monkeypatch.setattr(sosovalue_macro, "_request", impl)
        with pytest.raises(
            sosovalue_common.SoSoValueRateLimitError, match="rate limited before any"
        ):
            sosovalue_macro._fetch_all()
        assert len([c for c in impl.calls if c != "/macro/events"]) == 1

    def test_a_single_network_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.setattr(
            sosovalue_macro,
            "_request",
            _request_impl(
                history_error=requests.ConnectionError("down"),
                error_names={"GDP (QoQ)"},
            ),
        )
        payload = sosovalue_macro._fetch_all()
        assert payload["events_failed"] == ["GDP (QoQ)"]
        assert set(payload["histories"]) == set(TRACKED) - {"GDP (QoQ)"}

    def test_all_histories_failed_raises_instead_of_writing_emptiness(self, monkeypatch):
        # A figure-less schedule must not overwrite a complete cached snapshot
        # while presenting itself as fresh: raising routes the caller through
        # the stale fallback, which preserves and discloses the old snapshot.
        # The breaker still bounds the burn: only its worth of history
        # requests is actually sent.
        impl = _request_impl(
            history_error=requests.ConnectionError("down"), error_names=set(TRACKED)
        )
        monkeypatch.setattr(sosovalue_macro, "_request", impl)
        with pytest.raises(sosovalue_common.SoSoValueError, match="none of the"):
            sosovalue_macro._fetch_all()
        history_calls = [c for c in impl.calls if c != "/macro/events"]
        assert len(history_calls) == sosovalue_macro.MAX_CONSECUTIVE_NETWORK_FAILURES

    def test_rejected_key_mid_loop_propagates(self, monkeypatch):
        monkeypatch.setattr(
            sosovalue_macro,
            "_request",
            _request_impl(
                history_error=sosovalue_common.SoSoValueNotConfiguredError("401"),
                error_names={"CPI (YoY)"},
            ),
        )
        with pytest.raises(sosovalue_common.SoSoValueNotConfiguredError):
            sosovalue_macro._fetch_all()

    def test_calendar_failure_is_fatal(self, monkeypatch):
        def impl(path, params):
            raise sosovalue_common.SoSoValueError("calendar broke")

        monkeypatch.setattr(sosovalue_macro, "_request", impl)
        with pytest.raises(sosovalue_common.SoSoValueError, match="calendar broke"):
            sosovalue_macro._fetch_all()


# --------------------------------------------------------------------------- #
# caching, TTLs, stale fallback, read-side validation
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCacheAndLoad:
    def _setup(self, tmp_path, monkeypatch, now="2026-08-11T06:00:00Z"):
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.setenv("SOSOVALUE_API_KEY", "test-key")
        monkeypatch.setattr(sosovalue_common, "_utc_now", lambda: _at(now))
        impl = _request_impl()
        monkeypatch.setattr(sosovalue_macro, "_request", impl)
        return impl

    def _write_cache(self, tmp_path, **overrides):
        payload = {
            "calendar": [{"date": "2026-08-11", "events": ["CPI (YoY)"]}],
            "calendar_unusable": 0,
            "histories": _histories(),
            "events_failed": [],
            "events_unknown": [],
            "fetched_at": "2026-08-11T05:00:00Z",
        }
        payload.update(overrides)
        (tmp_path / "sosovalue_macro.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_within_ttl_reuses_cache_without_requests(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path)  # 1h old vs 6h TTL
        snapshot = sosovalue_macro._load_snapshot()
        assert snapshot.stale is False
        assert impl.calls == []

    def test_incomplete_snapshot_uses_the_short_ttl(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-11T06:30:00Z")
        # 1.5h old: fresh under the 6h TTL, expired under the 1h incomplete TTL.
        self._write_cache(
            tmp_path,
            histories=_histories(failed=("GDP (QoQ)",)),
            events_failed=["GDP (QoQ)"],
        )
        snapshot = sosovalue_macro._load_snapshot()
        assert impl.calls  # refetched
        assert snapshot.events_failed == []

    def test_unknown_events_do_not_shorten_the_ttl(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-11T07:30:00Z")
        # 2.5h old with an unknown (renamed) event: still fresh under the 6h
        # TTL — an hourly re-sweep cannot heal a rename, so it must not run.
        self._write_cache(
            tmp_path,
            histories=_histories(unknown=("GDP (QoQ)",)),
            events_unknown=["GDP (QoQ)"],
        )
        snapshot = sosovalue_macro._load_snapshot()
        assert impl.calls == []
        assert snapshot.events_unknown == ["GDP (QoQ)"]

    def test_past_ttl_refetches_and_overwrites(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-11T12:30:00Z")
        self._write_cache(tmp_path)  # 7.5h old
        sosovalue_macro._load_snapshot()
        assert "/macro/events" in impl.calls
        payload = json.loads((tmp_path / "sosovalue_macro.json").read_text(encoding="utf-8"))
        assert payload["fetched_at"] == "2026-08-11T12:30:00Z"

    def test_unset_key_raises_even_with_a_fresh_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path)
        monkeypatch.delenv("SOSOVALUE_API_KEY")
        with pytest.raises(sosovalue_common.SoSoValueNotConfiguredError):
            sosovalue_macro._load_snapshot()

    def test_failure_without_cache_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_macro, "_request", broken)
        with pytest.raises(sosovalue_common.SoSoValueError, match="no usable cache"):
            sosovalue_macro._load_snapshot()
        assert not (tmp_path / "sosovalue_macro.json").exists()

    def test_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-12T05:00:00Z")
        self._write_cache(tmp_path)  # 24h old

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_macro, "_request", broken)
        snapshot = sosovalue_macro._load_snapshot()
        assert snapshot.stale is True

    def test_stale_cache_past_cap_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-26T06:00:00Z")  # 15 days
        self._write_cache(tmp_path)

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_macro, "_request", broken)
        with pytest.raises(sosovalue_common.SoSoValueError, match="days stale"):
            sosovalue_macro._load_snapshot()

    def test_stale_cache_at_cap_is_still_served(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-25T05:00:00Z")  # exactly 14d
        self._write_cache(tmp_path)

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_macro, "_request", broken)
        assert sosovalue_macro._load_snapshot().stale is True

    def test_future_dated_fetched_at_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-11T00:00:00Z")
        self._write_cache(tmp_path, fetched_at="2026-08-11T05:00:00Z")  # future stamp

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_macro, "_request", broken)
        with pytest.raises(sosovalue_common.SoSoValueError, match="unparseable or future-dated"):
            sosovalue_macro._load_snapshot()

    def test_rate_limit_wrap_keeps_its_type_past_the_cap(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-26T06:00:00Z")
        self._write_cache(tmp_path)

        def limited(path, params):
            raise sosovalue_common.SoSoValueRateLimitError("429")

        monkeypatch.setattr(sosovalue_macro, "_request", limited)
        with pytest.raises(sosovalue_common.SoSoValueRateLimitError):
            sosovalue_macro._load_snapshot()

    def test_partition_mismatch_rejects_the_cache(self, tmp_path, monkeypatch, caplog):
        impl = self._setup(tmp_path, monkeypatch)
        histories = _histories()
        histories.pop("GDP (QoQ)")  # missing AND not in events_failed
        self._write_cache(tmp_path, histories=histories)
        with caplog.at_level("WARNING"):
            sosovalue_macro._load_snapshot()
        assert impl.calls  # cache was rejected, so it refetched
        assert "partition TRACKED_EVENTS" in caplog.text

    def test_malformed_history_row_rejects_the_cache(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch)
        histories = _histories()
        histories["CPI (YoY)"] = [_row("2026-01-15", "1.0%", "1.1%", "0.9%\x00")]
        self._write_cache(tmp_path, histories=histories)
        sosovalue_macro._load_snapshot()
        assert impl.calls


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRender:
    def _rich_snapshot(self):
        return _snapshot(
            calendar=[
                {"date": "2026-08-11", "events": ["CPI (YoY)"]},
                {"date": "2026-08-12", "events": ["PPI (MoM)"]},
            ],
            histories=_histories(
                overrides={
                    "CPI (YoY)": [
                        _row("2026-07-14", "3.5%", "3.8%", "4.2%"),
                        _row("2026-08-12", "", "3.4%", "3.5%"),
                    ],
                    "Nonfarm Payrolls": [
                        _row("2026-08-07", "-23", "85", "57"),
                        # A future print the provider has ALREADY filled in
                        # (the backtest case): its actual must never render.
                        _row("2026-08-21", "999", "80", "-23"),
                    ],
                }
            ),
        )

    def test_scheduled_rows_carry_forecast_but_never_an_actual(self):
        report = _render(self._rich_snapshot())
        assert "| 2026-08-12 | 1d | CPI (YoY) | 3.4% | 3.5% |" in report
        assert "| 2026-08-21 | 10d | Nonfarm Payrolls | 80 | -23 |" in report
        # The filled-in future actual is nowhere in the report.
        assert "999" not in report

    def test_untracked_calendar_names_appear_name_only(self):
        report = _render(self._rich_snapshot())
        assert "| 2026-08-12 | 1d | PPI (MoM) | — | — |" in report

    def test_the_same_print_is_not_double_listed_across_the_two_sources(self):
        # The calendar lists CPI (YoY) one day off its history row (a
        # live-observed offset): a calendar mention within a day of a shown
        # figures row is the same print and must not add a name-only line.
        snapshot = _snapshot(
            calendar=[{"date": "2026-08-13", "events": ["CPI (YoY)"]}],
            histories=_histories(overrides={"CPI (YoY)": [_row("2026-08-12", "", "3.4%", "3.5%")]}),
        )
        report = _render(snapshot)
        assert "| 2026-08-12 | 1d | CPI (YoY) | 3.4% | 3.5% |" in report
        assert "| 2026-08-13 | 2d | CPI (YoY) | — | — |" not in report

    def test_a_second_weekly_occurrence_is_not_swallowed_by_the_dedupe(self):
        # A weekly event has two prints inside the 14-day window; the history
        # carries only the next pending row, so the calendar's later date is
        # a DIFFERENT print and must still be listed (name-only).
        snapshot = _snapshot(
            calendar=[
                {"date": "2026-08-13", "events": ["Initial Jobless Claims"]},
                {"date": "2026-08-20", "events": ["Initial Jobless Claims"]},
            ],
            histories=_histories(
                overrides={"Initial Jobless Claims": [_row("2026-08-13", "", "202", "199")]}
            ),
        )
        report = _render(snapshot)
        assert "| 2026-08-13 | 2d | Initial Jobless Claims | 202 | 199 |" in report
        assert "| 2026-08-20 | 9d | Initial Jobless Claims | — | — |" in report

    def test_released_rows_show_surprises(self):
        report = _render(self._rich_snapshot())
        assert "| 2026-07-14 | CPI (YoY) | 3.5% | 3.8% | -0.3pp | 4.2% |" in report
        assert "| 2026-08-07 | Nonfarm Payrolls | -23 | 85 | -108 | 57 |" in report

    def test_lookahead_a_past_curr_date_sees_no_future_release(self):
        report = _render(self._rich_snapshot(), curr_date="2026-07-20")
        assert "-108" not in report  # NFP 2026-08-07 is the future here
        assert "2026-08-07" not in report.split("Scheduled")[0]

    def test_passed_but_unreleased_print_is_labelled(self):
        snapshot = _snapshot(
            histories=_histories(
                overrides={"Initial Jobless Claims": [_row("2026-08-10", "", "202", "199")]}
            )
        )
        report = _render(snapshot)
        assert "| 2026-08-10 | Initial Jobless Claims | not yet released |" in report

    def test_same_day_release_gets_the_timing_caveat(self):
        snapshot = _snapshot(
            histories=_histories(
                overrides={"Nonfarm Payrolls": [_row("2026-08-11", "-23", "85", "57")]}
            )
        )
        assert "may postdate an intraday decision time" in _render(snapshot)
        assert "may postdate an intraday decision time" not in _render(self._rich_snapshot())

    def test_fixed_caveats_are_always_present(self):
        report = _render(self._rich_snapshot())
        assert "No Fed rate decisions" in report
        assert "regime / risk modifier" in report
        assert "not point-in-time snapshots" in report

    def test_failed_events_and_unusable_names_are_disclosed(self):
        snapshot = _snapshot(
            calendar_unusable=2,
            histories=_histories(failed=("GDP (QoQ)", "Retail Sales (MoM)")),
            events_failed=["GDP (QoQ)", "Retail Sales (MoM)"],
        )
        report = _render(snapshot)
        assert f"({len(TRACKED) - 2}/{len(TRACKED)})" in report
        assert "GDP (QoQ), Retail Sales (MoM)" in report
        assert "2 calendar entries had no usable event name" in report

    def test_unknown_events_are_disclosed_as_a_code_gap(self):
        snapshot = _snapshot(
            histories=_histories(unknown=("Core CPI (YoY)",)),
            events_unknown=["Core CPI (YoY)"],
        )
        report = _render(snapshot)
        assert "unknown to the provider (Core CPI (YoY))" in report
        assert "renamed or dropped upstream" in report

    def test_stale_snapshot_is_disclosed(self):
        report = _render(_snapshot(fetched_at="2026-08-09T00:00:00Z", stale=True), "2026-08-11")
        assert "STALE by" in report

    def test_released_table_is_capped_with_a_note(self):
        rows = [
            _row(f"2026-{m:02d}-{d:02d}", "1%", "2%", "3%") for m in (6, 7) for d in range(1, 27)
        ]
        snapshot = _snapshot(histories=_histories(overrides={"CPI (YoY)": rows}))
        report = _render(snapshot, curr_date="2026-08-01", look_back_days=90)
        assert f"most recent {sosovalue_macro.MAX_ROWS} of" in report

    def test_empty_sections_state_their_reach_honestly(self):
        snapshot = _snapshot(
            calendar=[{"date": "2026-08-11", "events": ["CPI (YoY)"]}],
            histories=_histories(
                overrides={name: [_row("2020-01-15", "1%", "1%", "1%")] for name in TRACKED}
            ),
        )
        report = _render(snapshot, curr_date="2023-06-01")
        assert "none visible for 2023-06-01" in report
        assert "no tracked releases" in report

    def test_non_padded_curr_date_is_normalized(self):
        padded = _render(self._rich_snapshot(), curr_date="2026-08-11")
        sloppy = _render(self._rich_snapshot(), curr_date="2026-8-11")
        assert padded == sloppy


# --------------------------------------------------------------------------- #
# router integration
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouterIntegration:
    def _with_vendor(self, vendor):
        set_config({"data_vendors": {"economic_calendar": vendor}})

    def teardown_method(self):
        set_config({"data_vendors": {"economic_calendar": "none"}})

    def test_routes_to_the_sosovalue_report(self):
        self._with_vendor("sosovalue")
        with mock.patch.object(sosovalue_macro, "_load_snapshot", return_value=_snapshot()):
            report = interface.route_to_vendor("get_economic_calendar", "2026-08-11", None)
        assert "US Economic Calendar" in report

    def test_vendor_failure_degrades_to_the_sentinel(self):
        self._with_vendor("sosovalue")
        with mock.patch.object(
            sosovalue_macro,
            "_load_snapshot",
            side_effect=sosovalue_common.SoSoValueError("down"),
        ):
            report = interface.route_to_vendor("get_economic_calendar", "2026-08-11", None)
        assert report.startswith("DATA_UNAVAILABLE")

    def test_unset_key_degrades_to_the_sentinel(self, monkeypatch, tmp_path):
        self._with_vendor("sosovalue")
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.delenv("SOSOVALUE_API_KEY", raising=False)
        report = interface.route_to_vendor("get_economic_calendar", "2026-08-11", None)
        assert report.startswith("DATA_UNAVAILABLE")

    def test_none_vendor_is_the_disabled_sentinel(self):
        self._with_vendor("none")
        report = interface.route_to_vendor("get_economic_calendar", "2026-08-11", None)
        assert "disabled by configuration" in report
