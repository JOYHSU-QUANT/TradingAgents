"""Tests for the shared in-flight decision state machine (issue #181).

The object is lane-agnostic (``parsed`` / ``registration`` are type
parameters), so it is exercised here with plain strings standing in for the
decision and registration DTOs; the two lanes' suites pin that THEY drive this
object (``tests/paper/test_scheduler.py``, ``tests/live/test_decision.py``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.common.inflight import (
    NON_RETRYABLE_PREFIX,
    InFlightDecision,
    failed_cycle_next_at,
    inflight_ids,
    non_retryable_message,
    parse_stored_response,
)

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_4H = timedelta(hours=4)


def _inflight(**overrides):
    kwargs = {"attempt_id": "r|a", "scheduled_at": _T0, "try_no": 1}
    kwargs.update(overrides)
    return InFlightDecision.for_try(**kwargs)


def _settled(parsed="decision"):
    f = _inflight(parsed=parsed)
    f.raw_stored = True  # as a lane's store step settles it
    return f


# -- construction and the id scheme --------------------------------------------


def test_for_try_derives_the_per_try_ids_from_the_one_scheme():
    f = InFlightDecision.for_try("r|a", _T0, 2, parsed="decision")
    assert (f.input_id, f.output_id) == inflight_ids("r|a", 2) == ("r|a#in2", "r|a#out2")
    assert f.attempt_count == 2 and f.parsed == "decision"
    # The conservative defaults: nothing settled, nothing gated, nothing failed.
    assert f.raw_stored is False and f.registration is None and f.pending_fail is None


@pytest.mark.parametrize(("attempt_id", "try_no"), [("", 1), ("r|a", 0)])
def test_the_id_scheme_refuses_a_malformed_key(attempt_id, try_no):
    with pytest.raises(ValueError, match="inflight_ids"):
        inflight_ids(attempt_id, try_no)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", ""),
        ("input_id", ""),
        ("output_id", ""),
        ("attempt_count", 0),
        ("scheduled_at", datetime(2026, 7, 6, 12, 0)),  # naive
    ],
)
def test_construction_guards_the_row_keys(field, value):
    kwargs = {
        "attempt_id": "r|a",
        "scheduled_at": _T0,
        "attempt_count": 1,
        "input_id": "r|a#in1",
        "output_id": "r|a#out1",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=f"InFlightDecision.{field}"):
        InFlightDecision(**kwargs)


# -- the ordering rules ----------------------------------------------------------


def test_gating_needs_a_collected_settled_unfailed_decision():
    f = _inflight()
    with pytest.raises(AssertionError, match="no decision has been collected"):
        f.require_gateable()
    f.parsed = "decision"
    with pytest.raises(AssertionError, match="store has not settled"):
        f.require_gateable()
    f.raw_stored = True
    assert f.require_gateable() == "decision"  # collected + settled: the gate may run on it
    f.arm_fail("timeout", "boom")
    with pytest.raises(AssertionError, match="only its api_failed record is owed"):
        f.require_gateable()


def test_a_registration_is_cached_once():
    f = _settled()
    f.cache_registration("plan-1")
    assert f.registration == "plan-1"
    with pytest.raises(AssertionError, match="gate must not run twice"):
        f.cache_registration("plan-2")
    assert f.registration == "plan-1"  # the committed plan's registration stands


def test_a_failure_is_armed_once():
    f = _inflight()
    f.arm_fail(None, "non-retryable: RuntimeError('x')")
    assert f.pending_fail == (None, "non-retryable: RuntimeError('x')")
    with pytest.raises(AssertionError, match="a cycle fails once"):
        f.arm_fail("timeout", "second verdict")
    assert f.pending_fail == (None, "non-retryable: RuntimeError('x')")  # the first stands


# -- the resume step -------------------------------------------------------------


def test_parse_stored_response_settles_the_store_from_the_text():
    f = _inflight()
    parse_stored_response(f, '{"x": 1}', lambda raw: ("parsed", raw), log=logging.getLogger("t"))
    assert f.parsed == ("parsed", '{"x": 1}')
    assert f.raw_stored is True  # the SOURCE is the store — settled by definition
    assert f.require_gateable() == ("parsed", '{"x": 1}')  # ... so it may be gated at once


def test_parse_stored_response_logs_the_full_text_on_the_callers_logger_then_reraises(caplog):
    f = _inflight()
    lane = logging.getLogger("tests.inflight.some_lane")

    def boom(raw):
        raise ValueError("corrupt stored response")

    with (
        caplog.at_level(logging.ERROR, logger=lane.name),
        pytest.raises(ValueError, match="corrupt stored response"),
    ):
        parse_stored_response(f, "garbage ☃", boom, log=lane)
    assert f.parsed is None and f.raw_stored is False  # nothing settled on a raise
    # On the CALLER's logger (the lane the operator greps), at ERROR, with the
    # text's repr: the terminal record clears the row and this is the copy the
    # post-mortem gets.
    [rec] = [r for r in caplog.records if r.name == lane.name]
    assert rec.levelno == logging.ERROR
    assert repr("garbage ☃") in rec.getMessage()
    assert "r|a" in rec.getMessage()


# -- the failed-cycle anchor and the non-retryable prefix --------------------------


def test_a_failed_cycle_anchors_its_successor_on_the_schedule_when_that_is_still_ahead():
    assert failed_cycle_next_at(_T0, _T0 + timedelta(minutes=5), _4H) == _T0 + _4H


def test_a_late_failed_cycle_anchors_its_successor_on_the_terminal_instant():
    # scheduled_at + interval already lies in the past (or is exactly now):
    # anchoring on it would fire the next cycle at once and chain one ladder
    # per missed interval — §3's "never backfilled", extended to failures.
    late = _T0 + timedelta(hours=28)
    assert failed_cycle_next_at(_T0, late, _4H) == late + _4H
    assert failed_cycle_next_at(_T0, _T0 + _4H, _4H) == _T0 + _4H + _4H


def test_non_retryable_message_carries_the_prefix_and_the_repr():
    msg = non_retryable_message(ValueError("x"))
    assert msg.startswith(NON_RETRYABLE_PREFIX)
    assert msg == "non-retryable: ValueError('x')"  # the RUNBOOKs' literal contract
