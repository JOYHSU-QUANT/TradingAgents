"""The reading and the state machine, case by case."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.carry.signal import (
    MIN_RECENT_SAMPLES,
    OUT,
    Action,
    Params,
    Position,
    Reading,
    Side,
    SignalError,
    advance,
    decide,
    finite,
    read,
    whole,
)
from contrib.carry.upstream import MIN_FUNDING_SAMPLES

from .conftest import DAY0, MS_PER_HOUR, alternating, day, hourly

PARAMS = Params()


def _reading(
    z: float | None, *, current: str = "0.00002", recent: str | None = "0.00002"
) -> Reading:
    return Reading(
        at_ms=day(1) - MS_PER_HOUR,
        current=Decimal(current),
        z=z,
        samples=700,
        recent_mean=None if recent is None else Decimal(recent),
        recent_samples=24,
    )


# --- read -------------------------------------------------------------------


def test_read_is_none_when_nothing_settled_before_the_boundary():
    points = hourly(day(1), alternating(1))
    assert read(points, day(1), PARAMS) is None
    assert read([], day(1), PARAMS) is None


def test_read_takes_the_latest_settlement_strictly_before_the_boundary():
    points = hourly(DAY0, alternating(2))
    reading = read(points, day(1), PARAMS)
    assert reading is not None
    # The 23:00 settlement, not the one stamped exactly at the boundary.
    assert reading.at_ms == day(1) - MS_PER_HOUR
    assert reading.current == points[23].rate


def test_read_has_no_z_under_the_perp_packages_sample_floor():
    points = hourly(DAY0, alternating(1)[:MIN_FUNDING_SAMPLES])
    reading = read(points, day(1), PARAMS)
    assert reading is not None
    assert reading.z is None
    # The window is the settlements strictly before the current one.
    assert reading.samples == MIN_FUNDING_SAMPLES - 1


def test_read_scores_z_over_the_trailing_window_of_whole_days():
    points = hourly(DAY0, alternating(40))
    reading = read(points, day(40), PARAMS)
    assert reading is not None
    # The window is ``cutoff <= time < current``: thirty whole days of hourly settlements,
    # and against an alternating series the current (odd, low) settlement sits below the mean.
    assert reading.samples == 30 * 24
    assert reading.z is not None and reading.z < 0


def test_read_recent_mean_covers_the_last_day_including_current():
    rates = alternating(2)
    rates[-1] = Decimal("0.0001")  # the current settlement, a day's worth of weight by itself
    points = hourly(DAY0, rates)
    reading = read(points, day(2), PARAMS)
    assert reading is not None
    assert reading.recent_samples == 24
    assert reading.recent_mean == sum(rates[-24:], Decimal(0)) / 24


def test_read_recent_mean_is_none_under_its_floor():
    points = hourly(DAY0, alternating(1)[: MIN_RECENT_SAMPLES - 1])
    reading = read(points, day(1), PARAMS)
    assert reading is not None
    assert reading.recent_mean is None
    assert reading.recent_samples == MIN_RECENT_SAMPLES - 1


def test_annualised_readings_multiply_by_the_hours_in_a_year():
    reading = _reading(1.0, current="0.00001", recent="0.00002")
    assert reading.current_annualized == Decimal("0.00001") * 8760
    assert reading.recent_annualized == Decimal("0.00002") * 8760
    assert _reading(1.0, recent=None).recent_annualized is None


def test_a_reading_holds_the_floors_read_produces():
    with pytest.raises(SignalError, match="a z-score needs at least"):
        Reading(at_ms=day(1), current=Decimal(1), z=1.0, samples=MIN_FUNDING_SAMPLES - 1,
                recent_mean=None, recent_samples=0)  # fmt: skip
    with pytest.raises(SignalError, match="a recent mean needs at least"):
        Reading(at_ms=day(1), current=Decimal(1), z=None, samples=0,
                recent_mean=Decimal(1), recent_samples=MIN_RECENT_SAMPLES - 1)  # fmt: skip
    with pytest.raises(SignalError, match="at_ms must be at least 1"):
        Reading(at_ms=0, current=Decimal(1), z=None, samples=0, recent_mean=None, recent_samples=0)
    with pytest.raises(SignalError, match="z: expected a finite number"):
        Reading(at_ms=1, current=Decimal(1), z=float("nan"), samples=700,
                recent_mean=None, recent_samples=0)  # fmt: skip


# --- decide: out ------------------------------------------------------------


def test_out_enters_at_the_threshold_when_funding_is_positive():
    assert decide(_reading(1.5), OUT, day(1), PARAMS) is Action.ENTER
    assert decide(_reading(3.0), OUT, day(1), PARAMS) is Action.ENTER


def test_out_stays_out_below_the_threshold():
    assert decide(_reading(1.49), OUT, day(1), PARAMS) is Action.STAY_OUT


def test_out_never_enters_on_a_negative_or_zero_rate():
    assert decide(_reading(5.0, current="-0.00001"), OUT, day(1), PARAMS) is Action.STAY_OUT
    assert decide(_reading(5.0, current="0"), OUT, day(1), PARAMS) is Action.STAY_OUT


def test_out_stays_out_without_a_z_or_a_reading():
    assert decide(_reading(None), OUT, day(1), PARAMS) is Action.STAY_OUT
    assert decide(None, OUT, day(1), PARAMS) is Action.STAY_OUT


# --- decide: in -------------------------------------------------------------

IN_AT_DAY1 = Position(Side.IN, day(1))


def test_in_holds_without_a_reading():
    assert decide(None, IN_AT_DAY1, day(10), PARAMS) is Action.HOLD


def test_in_holds_through_the_minimum_hold_whatever_the_reading_says():
    assert decide(_reading(-2.0, recent="-0.00001"), IN_AT_DAY1, day(3), PARAMS) is Action.HOLD
    assert decide(_reading(-2.0, recent="-0.00001"), IN_AT_DAY1, day(4) - 1, PARAMS) is Action.HOLD


def test_in_exits_at_or_below_the_exit_threshold_once_held_long_enough():
    assert decide(_reading(0.5), IN_AT_DAY1, day(4), PARAMS) is Action.EXIT
    assert decide(_reading(-1.0), IN_AT_DAY1, day(4), PARAMS) is Action.EXIT
    assert decide(_reading(0.51), IN_AT_DAY1, day(4), PARAMS) is Action.HOLD


def test_in_exits_when_the_last_day_paid_nothing():
    assert decide(_reading(1.0, recent="0"), IN_AT_DAY1, day(4), PARAMS) is Action.EXIT
    assert decide(_reading(1.0, recent="-0.000001"), IN_AT_DAY1, day(4), PARAMS) is Action.EXIT
    assert decide(_reading(1.0, recent="0.000001"), IN_AT_DAY1, day(4), PARAMS) is Action.HOLD


def test_in_still_exits_on_the_recent_mean_leg_when_there_is_no_z():
    """D6 made the two exit legs independent: a flat window blinds z, not the day's mean."""
    assert decide(_reading(None, recent="0"), IN_AT_DAY1, day(4), PARAMS) is Action.EXIT
    assert decide(_reading(None, recent="0.000001"), IN_AT_DAY1, day(4), PARAMS) is Action.HOLD


def test_in_holds_when_neither_exit_leg_can_be_read():
    assert decide(_reading(None, recent=None), IN_AT_DAY1, day(10), PARAMS) is Action.HOLD


def test_a_zero_minimum_hold_can_exit_the_next_boundary():
    params = Params(min_hold_days=0)
    assert decide(_reading(0.0), IN_AT_DAY1, day(2), params) is Action.EXIT


# --- actions and advance ----------------------------------------------------


def test_each_action_names_the_side_it_leaves_the_book_on():
    assert Action.ENTER.side_after is Side.IN
    assert Action.HOLD.side_after is Side.IN
    assert Action.EXIT.side_after is Side.OUT
    assert Action.STAY_OUT.side_after is Side.OUT


def test_advance_enters_at_the_boundary_and_exits_to_out():
    entered = advance(OUT, Action.ENTER, day(5))
    assert entered == Position(Side.IN, day(5))
    assert advance(entered, Action.HOLD, day(6)) == entered
    assert advance(entered, Action.EXIT, day(9)) == OUT
    assert advance(OUT, Action.STAY_OUT, day(6)) == OUT


def test_advance_refuses_a_hold_while_out():
    with pytest.raises(SignalError, match="hold is an action taken while in"):
        advance(OUT, Action.HOLD, day(6))


# --- the values' invariants -------------------------------------------------


def test_a_position_is_in_with_an_entry_or_out_without_one():
    with pytest.raises(SignalError):
        Position(Side.IN)
    with pytest.raises(SignalError):
        Position(Side.OUT, day(1))
    with pytest.raises(SignalError):
        Position(Side.IN, 0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_days": 1},
        {"window_days": True},
        {"z_in": 1.0, "z_out": 1.0},
        {"z_in": float("nan")},
        {"z_in": "1.5"},
        {"z_in": 10**400},
        {"min_hold_days": -1},
        {"margin_pct": 0},
        {"margin_pct": 101},
        {"margin_pct": 30.0},
    ],
)
def test_params_refuse_what_the_rule_cannot_run_with(kwargs):
    with pytest.raises(SignalError):
        Params(**kwargs)


def test_the_defaults_are_the_plans():
    assert Params(window_days=30, z_in=1.5, z_out=0.5, min_hold_days=3, margin_pct=30) == PARAMS


def test_the_guards_raise_the_error_class_they_are_given():
    class Mine(ValueError):
        pass

    assert whole(3, "n", low=1, high=5, error=Mine) == 3
    assert finite(2, "x", error=Mine) == 2.0
    with pytest.raises(Mine, match="n must be between 1 and 5, got 7"):
        whole(7, "n", low=1, high=5, error=Mine)
    with pytest.raises(Mine, match="n must be a whole number, got True"):
        whole(True, "n", low=0, error=Mine)
    with pytest.raises(Mine, match="x: expected a number"):
        finite("2", "x", error=Mine)
    with pytest.raises(SignalError):
        finite(float("inf"), "x")
