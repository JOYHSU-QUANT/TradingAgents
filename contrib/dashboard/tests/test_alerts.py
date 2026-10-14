"""The rules, one by one, and the state between ticks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contrib.dashboard.alerts import (
    ActiveCondition,
    Condition,
    Evaluation,
    Event,
    State,
    StateError,
    advance,
    evaluate,
    fresh_state,
    load_state,
    save_state,
)
from contrib.dashboard.config import Thresholds
from contrib.dashboard.readers import CommandResult

from .support import (
    CARRY_KEY,
    HL_KEY,
    MEMINFO,
    NOW,
    UNI_TREND_KEY,
    UNITS_QUIET,
    FakeRunner,
    build_config,
    carry_day,
    carry_document,
    clock,
    handoff,
    hl_instant,
    kill_switch_event,
    light,
    quiet_documents,
    read_lines_and_host,
    safe_mode_event,
    uniswap_document,
    uniswap_run,
    unit_text,
)


def _tick(
    tmp_path,
    documents,
    *,
    now=NOW,
    units=UNITS_QUIET,
    events_seen=None,
    config=None,
    meminfo_text=MEMINFO,
):
    config, reads, host = read_lines_and_host(
        tmp_path, documents, now=now, units=units, config=config, meminfo_text=meminfo_text
    )
    return evaluate(now=now, config=config, reads=reads, host=host, events_seen=events_seen or {})


def _keys(evaluation):
    return [c.key for c in evaluation.conditions]


def _line_keys(evaluation, line: str):
    """The keys of one line only: a clock moved hours away wakes the rules of the other lines too."""
    return [c.key for c in evaluation.conditions if c.line == line]


def test_a_quiet_host_raises_nothing_and_seeds_the_event_tails(tmp_path):
    evaluation = _tick(tmp_path, quiet_documents())
    assert evaluation.conditions == () and evaluation.events == () and evaluation.notes == ()
    assert evaluation.events_seen == {
        "hl-paper": "2026-10-14T04:50:00+00:00",
        "carry-perp": "2026-10-14T04:50:00+00:00",
    }


# --- hyperliquid -----------------------------------------------------------------------


def test_a_daemon_that_is_not_active_is_down_and_its_progress_is_not_judged(tmp_path):
    failed = unit_text(active="failed", sub="failed", result="exit-code", status=1)
    units = dict(UNITS_QUIET, **{"hl-paper.service": failed})
    docs = quiet_documents()
    docs[HL_KEY]["scheduler"]["lease_heartbeat_at"] = hl_instant(NOW - timedelta(hours=2))
    evaluation = _tick(tmp_path, docs, units=units)
    assert _keys(evaluation) == ["unit:hl-paper.service:down"]
    [down] = evaluation.conditions
    assert down.line == "hl-paper.service" and down.rule == "down"
    assert down.text == (
        "hl-paper.service is failed (failed; result exit-code); "
        "last started Mon 2026-10-12 05:21:10 UTC"
    )


def test_the_segment_marker_parks_the_daemon_without_an_alert_and_leaves_a_note(tmp_path):
    marker = tmp_path / "SEGMENT_SWITCH_READY"
    marker.write_text("", encoding="ascii")
    units = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    docs = quiet_documents()
    docs[HL_KEY]["scheduler"]["next_decision_at"] = hl_instant(NOW - timedelta(hours=2))
    parked = build_config(tmp_path, marker=marker)
    evaluation = _tick(tmp_path, docs, units=units, config=parked)
    assert evaluation.conditions == ()
    assert evaluation.notes == (
        "hl-paper: the segment switch marker is present; the unit and progress rules are skipped",
    )
    marker.unlink()
    evaluation = _tick(tmp_path, docs, units=units, config=parked)
    assert _keys(evaluation) == ["unit:hl-paper.service:down"]


def test_an_overdue_decision_a_stale_lease_and_a_stale_export_are_three_conditions(tmp_path):
    docs = quiet_documents()
    sched = docs[HL_KEY]["scheduler"]
    sched["next_decision_at"] = hl_instant(NOW - timedelta(minutes=31))
    sched["lease_heartbeat_at"] = hl_instant(NOW - timedelta(minutes=16))
    sched["export"]["at"] = hl_instant(NOW - timedelta(hours=5, minutes=6))
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == [
        "hl-paper:cycle_overdue",
        "hl-paper:export_stale",
        "hl-paper:lease_stale",
    ]
    texts = {c.rule: c.text for c in evaluation.conditions}
    assert (
        texts["cycle_overdue"] == "the next decision was due at 2026-10-14 04:29Z, 31 minutes ago"
    )
    assert (
        texts["lease_stale"] == "the lease heartbeat is 16 minutes old (last at 2026-10-14 04:44Z)"
    )
    assert texts["export_stale"] == "the last export was 5.1 hours ago (at 2026-10-13 23:54Z)"


def test_the_thresholds_are_the_configs_not_the_rules(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY]["scheduler"]["next_decision_at"] = hl_instant(NOW - timedelta(minutes=31))
    loose = build_config(tmp_path, thresholds=Thresholds(cycle_overdue_minutes=60))
    assert _tick(tmp_path, docs, config=loose).conditions == ()


def test_a_failed_export_and_a_run_without_scheduler_or_export_yet(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY]["scheduler"]["export"] = {
        "status": "failed",
        "at": hl_instant(NOW - timedelta(hours=1)),
        "error": "disk",
    }
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["hl-paper:export_failed"]
    assert evaluation.conditions[0].text == "the last export failed at 2026-10-14 04:00Z"

    fresh = quiet_documents()
    fresh[HL_KEY]["scheduler"] = None
    fresh[HL_KEY]["created_at"] = hl_instant(NOW - timedelta(minutes=10))
    assert _tick(tmp_path, fresh).conditions == ()
    fresh[HL_KEY]["created_at"] = hl_instant(NOW - timedelta(hours=6))
    assert _keys(_tick(tmp_path, fresh)) == ["hl-paper:cycle_overdue"]

    young = quiet_documents()
    young[HL_KEY]["scheduler"]["export"] = {"status": None, "at": None, "error": None}
    young[HL_KEY]["scheduler"]["lease_heartbeat_at"] = None
    young[HL_KEY]["created_at"] = hl_instant(NOW - timedelta(hours=6))
    assert _keys(_tick(tmp_path, young)) == ["hl-paper:export_stale", "hl-paper:lease_stale"]


def test_safe_mode_holds_whatever_the_unit_does(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY]["safe_mode"] = {
        "type": "manual",
        "reason": "operator",
        "entered_at": hl_instant(NOW - timedelta(hours=3)),
    }
    units = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    evaluation = _tick(tmp_path, docs, units=units)
    assert _keys(evaluation) == ["hl-paper:safe_mode", "unit:hl-paper.service:down"]
    assert evaluation.conditions[0].text == "safe mode manual since 2026-10-14 02:00Z"


def test_events_are_news_once_the_tail_was_seen_and_the_refresh_is_never_news(tmp_path):
    seen = {"hl-paper": "2026-10-14T04:50:00+00:00", "carry-perp": "2026-10-14T04:50:00+00:00"}
    docs = quiet_documents()
    docs[HL_KEY]["kill_switch_events"] = [
        kill_switch_event(NOW - timedelta(minutes=1), "kill_switch_refreshed"),
        kill_switch_event(NOW - timedelta(minutes=2), "emergency_kill_switch_triggered"),
        kill_switch_event(NOW - timedelta(minutes=10), "kill_switch_refreshed"),
    ]
    docs[HL_KEY]["safe_mode_events"] = [
        safe_mode_event(NOW - timedelta(minutes=3), "safe_mode_entered", "recoverable"),
        safe_mode_event(NOW - timedelta(hours=2), "safe_mode_released", "recoverable"),
    ]
    evaluation = _tick(tmp_path, docs, events_seen=seen)
    assert evaluation.conditions == ()
    assert [e.text for e in evaluation.events] == [
        "safe_mode_entered (recoverable) at 2026-10-14 04:57Z",
        "emergency_kill_switch_triggered at 2026-10-14 04:58Z",
    ]
    stamp = hl_instant(NOW - timedelta(minutes=3))
    assert evaluation.events[0].key == f"hl-paper:event:safe_mode_entered@{stamp}"
    assert evaluation.events[0].at == stamp
    assert evaluation.events_seen["hl-paper"] == "2026-10-14T04:59:00+00:00"
    # Seen again: nothing new, the mark stays.
    again = _tick(tmp_path, docs, events_seen=evaluation.events_seen)
    assert again.events == () and again.events_seen == evaluation.events_seen
    # A tail with no row at all keeps the mark as it was.
    docs[HL_KEY]["kill_switch_events"] = []
    docs[HL_KEY]["safe_mode_events"] = []
    assert _tick(tmp_path, docs, events_seen=seen).events_seen["hl-paper"] == seen["hl-paper"]


# --- uniswap ------------------------------------------------------------------------------


def test_a_clock_behind_past_the_visit_window_ahead_or_never_decided(tmp_path):
    latest = datetime(2026, 10, 14, tzinfo=timezone.utc)
    docs = quiet_documents()
    behind_since = datetime(2026, 10, 14, 1, tzinfo=timezone.utc)
    behind = clock("behind", behind_since, undecided=1)
    docs[UNI_TREND_KEY] = uniswap_document(uniswap_run(clock=behind))
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["uniswap-trend:behind"]
    assert evaluation.conditions[0].text == (
        "1 boundary(ies) undecided; the latest, 2026-10-14 01:00Z, passed 4.0 hours ago"
    )
    docs[UNI_TREND_KEY] = uniswap_document(uniswap_run(clock=clock("ahead", latest)))
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["uniswap-trend:clock_ahead"]
    assert "before the last decided bar (2026-10-15 00:00Z)" in evaluation.conditions[0].text
    empty = uniswap_run(
        decided=0,
        first_decided=None,
        last_decided=None,
        clock=None,
        holdings=None,
        holdings_at=None,
        value=None,
        metrics=None,
        recent_decisions=[],
        recent_valuations=[],
        outcomes={"traded": 0, "held": 0, "skipped": 0, "rejected": 0, "partial": 0},
        created_at="2026-10-11T00:00:00Z",
    )
    docs[UNI_TREND_KEY] = uniswap_document(empty)
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["uniswap-trend:no_decision"]
    assert evaluation.conditions[0].text == (
        "no bar decided 77.0 hours after the run was created at 2026-10-11 00:00Z"
    )
    docs[UNI_TREND_KEY] = uniswap_document(dict(empty, created_at="2026-10-13T00:00:00Z"))
    assert _tick(tmp_path, docs).conditions == ()
    docs[UNI_TREND_KEY] = uniswap_document(run=None)
    docs[UNI_TREND_KEY]["run"] = None
    assert _keys(_tick(tmp_path, docs)) == ["uniswap-trend:no_run"]


def test_a_behind_clock_within_the_visit_window_is_not_an_alert(tmp_path):
    latest = datetime(2026, 10, 14, tzinfo=timezone.utc)
    docs = quiet_documents()
    docs[UNI_TREND_KEY] = uniswap_document(uniswap_run(clock=clock("behind", latest, undecided=1)))
    assert _tick(tmp_path, docs, now=latest + timedelta(hours=3)).conditions == ()


def test_a_oneshot_unit_is_judged_by_its_last_run(tmp_path):
    failed = unit_text(
        type_="oneshot",
        active="failed",
        sub="failed",
        result="exit-code",
        status=4,
        exit_="Tue 2026-10-14 00:12:00 UTC",
    )
    units = dict(UNITS_QUIET, **{"uniswap-v3-paper.service": failed})
    evaluation = _tick(tmp_path, quiet_documents(), units=units)
    assert _keys(evaluation) == ["unit:uniswap-v3-paper.service:failed"]
    assert evaluation.conditions[0].text == (
        "uniswap-v3-paper.service's last run failed (exit-code, exit status 4) "
        "at Tue 2026-10-14 00:12:00 UTC"
    )
    running = unit_text(type_="oneshot", active="activating", sub="start", result="success")
    units = dict(UNITS_QUIET, **{"uniswap-v3-paper.service": running})
    assert _tick(tmp_path, quiet_documents(), units=units).conditions == ()


def test_a_unit_not_installed_is_a_condition(tmp_path):
    units = {k: v for k, v in UNITS_QUIET.items() if k != "carry-signal.service"}
    evaluation = _tick(tmp_path, quiet_documents(), units=units)
    assert _keys(evaluation) == ["unit:carry-signal.service:not_found"]


# --- carry --------------------------------------------------------------------------------


def test_a_handoff_for_a_day_before_today_is_stale_once_the_coordinator_is_due(tmp_path):
    yesterday = datetime(2026, 10, 13, tzinfo=timezone.utc)
    docs = quiet_documents()
    docs[CARRY_KEY] = carry_document(handoff=handoff(yesterday))
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["carry:handoff_stale"]
    assert evaluation.conditions[0].text == (
        "the handoff decides 2026-10-13; today is 2026-10-14 and the coordinator was due by 00:30Z"
    )
    early = datetime(2026, 10, 14, 0, 20, tzinfo=timezone.utc)
    assert _line_keys(_tick(tmp_path, docs, now=early), "carry") == []
    tomorrow = datetime(2026, 10, 15, tzinfo=timezone.utc)
    docs[CARRY_KEY] = carry_document(handoff=handoff(tomorrow))
    late_evening = datetime(2026, 10, 14, 23, 55, tzinfo=timezone.utc)
    assert _line_keys(_tick(tmp_path, docs, now=late_evening), "carry") == []
    docs[CARRY_KEY] = carry_document(handoff=None, handoff_error="handoff-ETH.json: cannot be read")
    assert _keys(_tick(tmp_path, docs)) == ["carry:handoff_unreadable"]


def test_the_reports_held_by_d5_light_on_the_newest_day_is_the_condition(tmp_path):
    # The report lights the day a held streak reaches --d5-days and every day
    # after; the exporter reads the newest day only, and passes the threshold.
    today = datetime(2026, 10, 14, tzinfo=timezone.utc)
    days = [carry_day(today - timedelta(days=n)) for n in (4, 3, 2)]
    days.append(carry_day(today - timedelta(days=1), [light("held by D5")]))
    docs = quiet_documents()
    docs[CARRY_KEY] = carry_document(days=days)
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["carry:held_by_d5"]
    assert evaluation.conditions[0].text == (
        "held by D5 on 2026-10-13: a held streak of at least 3 day(s)"
    )
    # Lit yesterday but not today: the streak ended.
    days[-2] = carry_day(today - timedelta(days=2), [light("held by D5")])
    days[-1] = carry_day(today - timedelta(days=1))
    docs[CARRY_KEY] = carry_document(days=days)
    assert _tick(tmp_path, docs).conditions == ()


# --- host, exporter -------------------------------------------------------------------------


def test_disk_and_memory_past_their_thresholds(tmp_path):
    low = "MemTotal: 2003876 kB\nMemAvailable: 102400 kB\n"
    evaluation = _tick(tmp_path, quiet_documents(), meminfo_text=low)
    assert _keys(evaluation) == ["host:memory"]
    assert evaluation.conditions[0].text == "100 MB of memory available"
    strict = build_config(tmp_path, thresholds=Thresholds(disk_used_pct=1))
    evaluation = _tick(tmp_path, quiet_documents(), config=strict)
    assert "host:disk" in _keys(evaluation)
    disk = next(c for c in evaluation.conditions if c.key == "host:disk")
    assert disk.text.startswith("disk ") and "% used on" in disk.text


def test_a_line_that_could_not_be_read_is_a_condition_and_nothing_else_is_judged_for_it(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY] = CommandResult(1, "", "error: no such run\n")
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["hl-paper:read_failed"]
    assert evaluation.conditions[0].text == "the document could not be read: exit 1"
    assert "hl-paper" not in evaluation.events_seen


def test_a_document_of_another_shape_is_rules_failed_not_a_crash(tmp_path):
    docs = quiet_documents()
    del docs[HL_KEY]["scheduler"]["export"]
    docs[CARRY_KEY]["days"] = None
    # A clock state this build does not know is passed over, not refused.
    docs[UNI_TREND_KEY]["run"]["clock"]["state"] = "sideways"
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["carry:rules_failed", "hl-paper:rules_failed"]
    assert evaluation.conditions[0].text.startswith(
        "the rules could not read the document (TypeError"
    )
    assert evaluation.conditions[1].text.startswith(
        "the rules could not read the document (KeyError"
    )


# --- the state ------------------------------------------------------------------------------


def _evaluation(*conditions: Condition, events: tuple[Event, ...] = (), seen=None):
    return Evaluation(tuple(sorted(conditions, key=lambda c: c.key)), events, dict(seen or {}), ())


def test_advance_raises_keeps_and_clears_with_one_pending_line_each():
    down = Condition("unit:hl-paper.service:down", "hl-paper.service", "down", "is inactive")
    late = Condition("hl-paper:cycle_overdue", "hl-paper", "cycle_overdue", "31 minutes late")
    first = advance(fresh_state(), _evaluation(down, late), NOW)
    assert set(first.active) == {down.key, late.key}
    assert [(p.kind, p.key) for p in first.pending] == [("raised", late.key), ("raised", down.key)]
    assert first.active[down.key].since == "2026-10-14T05:00:00+00:00"
    assert [p.rule for p in first.pending] == ["cycle_overdue", "down"]
    later = NOW + timedelta(minutes=2)
    later_late = Condition(late.key, late.line, late.rule, "33 minutes late")
    event = Event(
        "hl-paper:event:x@t", "hl-paper", "x", hl_instant(later), "x at 2026-10-14 05:01Z"
    )
    second = advance(first, _evaluation(later_late, events=(event,)), later)
    assert set(second.active) == {late.key}
    assert second.active[late.key].since == "2026-10-14T05:00:00+00:00"
    assert second.active[late.key].text == "33 minutes late"
    assert [(p.kind, p.key, p.at) for p in second.pending[2:]] == [
        ("cleared", down.key, "2026-10-14T05:02:00+00:00"),
        ("event", event.key, "2026-10-14T05:02:00+00:00"),
    ]
    assert second.recent_events == (event,)
    assert [p.rule for p in second.pending[2:]] == ["down", "event:x"]


def test_the_state_round_trips_through_its_file_and_refuses_another_version(tmp_path):
    path = tmp_path / "alerts-state.json"
    assert load_state(path) == fresh_state()
    down = Condition("unit:x.service:down", "x.service", "down", "is inactive")
    event = Event("hl-paper:event:x@t", "hl-paper", "x", "2026-10-14T04:59:00+00:00", "x")
    seen = {"hl-paper": "2026-10-14T04:59:00+00:00"}
    advanced = advance(fresh_state(), _evaluation(down, events=(event,), seen=seen), NOW)
    state = State(
        advanced.active,
        advanced.events_seen,
        advanced.pending,
        advanced.recent_events,
        "2026-10-13",
    )
    save_state(path, state)
    assert load_state(path) == state
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["last_digest_day"] == "2026-10-13"
    assert list(raw) == [
        "version",
        "active",
        "events_seen",
        "pending",
        "recent_events",
        "last_digest_day",
    ]
    raw["version"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StateError, match="version is not 1"):
        load_state(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="move it aside"):
        load_state(path)
    path.write_text(json.dumps({"version": 1, "active": {}}), encoding="utf-8")
    with pytest.raises(StateError, match="KeyError"):
        load_state(path)


def test_the_state_file_is_written_atomically_and_privately(tmp_path):
    path = tmp_path / "alerts-state.json"
    save_state(path, fresh_state())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alerts-state.json"]
    if Path("/proc").exists():
        assert (path.stat().st_mode & 0o777) == 0o640


# --- the review round ---------------------------------------------------------------------


def test_an_event_mark_keeps_the_microseconds_so_the_newest_row_is_not_news_twice(tmp_path):
    stamped = NOW - timedelta(minutes=2)
    stamped = stamped.replace(microsecond=123456)
    docs = quiet_documents()
    docs[HL_KEY]["kill_switch_events"] = [
        kill_switch_event(stamped, "emergency_kill_switch_triggered")
    ]
    first = _tick(tmp_path, docs)
    assert first.events == ()
    assert first.events_seen["hl-paper"] == "2026-10-14T04:58:00.123456+00:00"
    second = _tick(tmp_path, docs, events_seen=first.events_seen)
    assert second.events == () and second.events_seen == first.events_seen


def test_a_line_that_cannot_be_read_keeps_what_it_held(tmp_path):
    held = {
        "hl-paper:safe_mode": ActiveCondition(
            "hl-paper",
            "safe_mode",
            "safe mode manual since 2026-10-14 02:00Z",
            "2026-10-14T02:02:00+00:00",
        ),
        "hl-paper:rules_failed": ActiveCondition(
            "hl-paper", "rules_failed", "x", "2026-10-14T02:02:00+00:00"
        ),
        "carry-perp:safe_mode": ActiveCondition(
            "carry-perp", "safe_mode", "y", "2026-10-14T02:02:00+00:00"
        ),
    }
    docs = quiet_documents()
    docs[HL_KEY] = CommandResult(None, "", "")
    config, reads, host = read_lines_and_host(tmp_path, docs)
    evaluation = evaluate(now=NOW, config=config, reads=reads, host=host, events_seen={}, held=held)
    assert _keys(evaluation) == ["hl-paper:read_failed", "hl-paper:safe_mode"]
    assert evaluation.conditions[1].text == "safe mode manual since 2026-10-14 02:00Z"
    # Through the state: the held condition keeps its since, the read failure is the only letter line.
    state = advance(State(dict(held), {}, (), (), None), evaluation, NOW)
    assert state.active["hl-paper:safe_mode"].since == "2026-10-14T02:02:00+00:00"
    assert [(p.kind, p.key) for p in state.pending] == [
        ("raised", "hl-paper:read_failed"),
        ("cleared", "hl-paper:rules_failed"),
        ("cleared", "carry-perp:safe_mode"),
    ]


def test_a_unit_that_systemctl_cannot_show_is_its_own_condition_and_is_not_judged_as_active(
    tmp_path,
):
    docs = quiet_documents()
    docs[HL_KEY]["scheduler"]["next_decision_at"] = hl_instant(NOW - timedelta(hours=2))
    inner = FakeRunner(docs)

    def no_bus(argv, cwd, timeout):
        if argv[0] == "systemctl":
            return CommandResult(1, "", "Failed to connect to bus\n")
        return inner(argv, cwd, timeout)

    config, reads, host = read_lines_and_host(tmp_path, docs, runner=no_bus)
    evaluation = evaluate(now=NOW, config=config, reads=reads, host=host, events_seen={})
    assert _keys(evaluation) == [f"unit:{unit}:unreadable" for unit in config.units()]
    # What the units and the lines held is kept, unjudged, while systemctl is mute.
    since = "2026-10-14T02:00:00+00:00"
    held = {
        "hl-paper:cycle_overdue": ActiveCondition("hl-paper", "cycle_overdue", "late", since),
        "hl-paper:safe_mode": ActiveCondition("hl-paper", "safe_mode", "latched", since),
        "unit:hl-paper.service:down": ActiveCondition("hl-paper.service", "down", "off", since),
        "unit:hl-paper.service:unreadable": ActiveCondition(
            "hl-paper.service", "unreadable", "x", since
        ),
    }
    evaluation = evaluate(now=NOW, config=config, reads=reads, host=host, events_seen={}, held=held)
    mine = [c.key for c in evaluation.conditions if c.line in ("hl-paper", "hl-paper.service")]
    assert mine == [
        "hl-paper:cycle_overdue",
        "unit:hl-paper.service:down",
        "unit:hl-paper.service:unreadable",
    ]
    unreadable = next(
        c
        for c in evaluation.conditions
        if c.rule == "unreadable" and c.line == "carry-signal.service"
    )
    assert unreadable.text == "systemctl could not show carry-signal.service"


def test_a_store_that_does_not_hold_up_is_its_own_condition(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY] = CommandResult(5, "", "error: store integrity\n")
    docs[UNI_TREND_KEY] = CommandResult(5, "", "failed: whatever\n")
    evaluation = _tick(tmp_path, docs)
    assert _keys(evaluation) == ["hl-paper:store_damaged", "uniswap-trend:read_failed"]
    assert evaluation.conditions[0].text == (
        "the store does not hold up: status exited 5 (investigate the store)"
    )


def test_a_line_whose_rules_fail_keeps_what_it_held(tmp_path):
    since = "2026-10-14T02:00:00+00:00"
    held = {
        "hl-paper:safe_mode": ActiveCondition("hl-paper", "safe_mode", "latched", since),
        "hl-paper:rules_failed": ActiveCondition("hl-paper", "rules_failed", "x", since),
    }
    docs = quiet_documents()
    del docs[HL_KEY]["scheduler"]["export"]
    config, reads, host = read_lines_and_host(tmp_path, docs)
    evaluation = evaluate(now=NOW, config=config, reads=reads, host=host, events_seen={}, held=held)
    assert _keys(evaluation) == ["hl-paper:rules_failed", "hl-paper:safe_mode"]
    assert "KeyError: 'export'" in evaluation.conditions[0].text
    assert evaluation.conditions[1].text == "latched"


def test_an_empty_event_tail_on_first_sight_is_seeded_with_the_tick(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY]["kill_switch_events"] = []
    docs[HL_KEY]["safe_mode_events"] = []
    first = _tick(tmp_path, docs)
    assert first.events == () and first.events_seen["hl-paper"] == NOW.isoformat()
    docs[HL_KEY]["safe_mode_events"] = [
        safe_mode_event(NOW + timedelta(minutes=1), "safe_mode_entered", "manual")
    ]
    later = NOW + timedelta(minutes=2)
    second = _tick(tmp_path, docs, now=later, events_seen=first.events_seen)
    assert [e.kind for e in second.events] == ["safe_mode_entered"]


def test_a_state_file_whose_marks_are_not_instants_is_refused(tmp_path):
    path = tmp_path / "alerts-state.json"
    save_state(path, fresh_state())
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["events_seen"] = {"hl-paper": "yesterday"}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StateError, match="must be an instant"):
        load_state(path)
    raw["events_seen"] = {}
    raw["last_digest_day"] = "2026-13-40"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StateError, match="ValueError"):
        load_state(path)
