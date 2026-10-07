"""The rule: one z-score of the latest funding settlement, and a state machine around it.

Pure functions and frozen values; nothing here reads a venue, a store or a
clock. The coordinator (:mod:`.cli`) and the historical replay
(:mod:`.history`) both drive the same three steps, so what the replay
measures is what the live signal does:

1. :func:`read` — from the settlements strictly before a boundary, the
   latest one is "current"; its z-score against the trailing
   ``window_days`` (the perp package's own :func:`funding_zscore`, which
   excludes the current point from its own sample) and the mean of the
   settlements in the last :data:`RECENT_HOURS` hours (current included)
   are the reading.
2. :func:`decide` — the state machine (carry plan §2 D6):
   out → enter when ``z >= z_in`` and the current rate is positive (a
   short perp collects positive funding; there is no spot borrow, so a
   negative rate is never traded — plan §1.2 item 5); in → exit when
   ``z <= z_out`` OR the recent mean is at or below zero, each leg read on
   its own, and only once the position has been held ``min_hold_days``
   (churn control); every other case holds what it has. A boundary with
   no reading changes nothing. A reading with no z (too few samples, or a
   flat window) cannot enter, and cannot exit on the z leg — but the
   recent-mean leg still can, because D6 made the two exit legs
   independent.
3. :func:`advance` — the position after the action. Which side an action
   leaves the book on is :attr:`Action.side_after`, stated once; the
   handoff's consistency check reads the same attribute.

A rate is a fraction per hour (``Decimal("0.0000125")`` is 0.00125%/h), as
the venue reports it; annualising multiplies by :data:`HOURS_PER_YEAR`.
The two value guards, :func:`whole` and :func:`finite`, live here because
the handoff module validates its document through them with its own error
class, so a document and a parameter are refused the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from .upstream import (
    FUNDING_INTERVAL_MS,
    MIN_FUNDING_SAMPLES,
    MS_PER_DAY,
    FundingPoint,
    SpecError,
    funding_zscore,
    require_number,
)

__all__ = [
    "HOURS_PER_YEAR",
    "MIN_RECENT_SAMPLES",
    "OUT",
    "RECENT_HOURS",
    "Action",
    "Params",
    "Position",
    "Reading",
    "Side",
    "SignalError",
    "advance",
    "decide",
    "finite",
    "read",
    "whole",
]

HOURS_PER_YEAR: Final = 24 * 365
# The exit check's second leg: the mean of the settlements in the last day.
# Funding settles hourly, so a full day is 24; the mean is only read from
# half a day of samples or more, so one missing hour does not blind it.
RECENT_HOURS: Final = 24
MIN_RECENT_SAMPLES: Final = 12


class SignalError(ValueError):
    """A parameter or a value the rule cannot be run with."""


def whole(
    value: object,
    what: str,
    *,
    low: int,
    high: int | None = None,
    error: type[ValueError] = SignalError,
) -> int:
    """``value`` as a whole number within the bounds; ``error`` names the failure."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{what} must be a whole number, got {value!r}")
    if value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"between {low} and {high}"
        raise error(f"{what} must be {bound}, got {value}")
    return value


def finite(value: object, what: str, *, error: type[ValueError] = SignalError) -> float:
    """``value`` as a finite float, by the research package's one numeric guard."""
    try:
        return require_number(value, what)
    except SpecError as exc:
        raise error(str(exc)) from None


@dataclass(frozen=True)
class Params:
    """The rule's knobs, every one of them checked at construction.

    ``window_days`` is the z-score's trailing sample; ``z_in`` / ``z_out``
    the entry and exit thresholds, exit strictly below entry so the band
    between them is where a position rests; ``min_hold_days`` the shortest
    stay once in; ``margin_pct`` the perp leg's target margin, a whole
    percent on the perp package's grid.
    """

    window_days: int = 30
    z_in: float = 1.5
    z_out: float = 0.5
    min_hold_days: int = 3
    margin_pct: int = 30

    def __post_init__(self) -> None:
        whole(self.window_days, "window_days", low=2)
        z_in = finite(self.z_in, "z_in")
        z_out = finite(self.z_out, "z_out")
        if not z_out < z_in:
            raise SignalError(f"z_out must be below z_in, got z_in={z_in} z_out={z_out}")
        whole(self.min_hold_days, "min_hold_days", low=0)
        whole(self.margin_pct, "margin_pct", low=1, high=100)


class Side(str, Enum):
    """Whether the book is on: ``in`` is short perp + long spot, ``out`` is flat both."""

    IN = "in"
    OUT = "out"


class Action(str, Enum):
    """What a boundary did to the position."""

    ENTER = "enter"
    HOLD = "hold"
    EXIT = "exit"
    STAY_OUT = "stay_out"

    @property
    def side_after(self) -> Side:
        """The side the book is on once this action is taken."""
        return Side.IN if self in (Action.ENTER, Action.HOLD) else Side.OUT


@dataclass(frozen=True)
class Position:
    """The book's side, and when it was entered (``None`` while out)."""

    side: Side
    entered_at_ms: int | None = None

    def __post_init__(self) -> None:
        if (self.side is Side.IN) != (self.entered_at_ms is not None):
            raise SignalError(
                f"a position is in with an entry instant or out without one, "
                f"got side={self.side.value} entered_at_ms={self.entered_at_ms!r}"
            )
        if self.entered_at_ms is not None:
            whole(self.entered_at_ms, "entered_at_ms", low=1)


OUT: Final = Position(Side.OUT)


@dataclass(frozen=True)
class Reading:
    """What the funding series said at a boundary, from the settlements before it.

    The invariants :func:`read` produces are enforced here too, so a reading
    rebuilt from a document is held to the same floors as one read from the
    series: a z-score exists only over at least :data:`MIN_FUNDING_SAMPLES`
    samples, a recent mean only over at least :data:`MIN_RECENT_SAMPLES`.
    """

    at_ms: int
    """The current settlement's instant — the latest one strictly before the boundary."""
    current: Decimal
    """Its rate, a fraction per hour."""
    z: float | None
    """Its z-score against the trailing window; ``None`` when the window cannot say."""
    samples: int
    """How many settlements the window held."""
    recent_mean: Decimal | None
    """The mean rate over the last :data:`RECENT_HOURS` hours, incl. current; ``None`` under the floor."""
    recent_samples: int

    def __post_init__(self) -> None:
        whole(self.at_ms, "at_ms", low=1)
        whole(self.samples, "samples", low=0)
        whole(self.recent_samples, "recent_samples", low=0)
        if self.z is not None:
            finite(self.z, "z")
            if self.samples < MIN_FUNDING_SAMPLES:
                raise SignalError(
                    f"a z-score needs at least {MIN_FUNDING_SAMPLES} samples, got {self.samples}"
                )
        if self.recent_mean is not None and self.recent_samples < MIN_RECENT_SAMPLES:
            raise SignalError(
                f"a recent mean needs at least {MIN_RECENT_SAMPLES} samples, "
                f"got {self.recent_samples}"
            )

    @property
    def recent_annualized(self) -> Decimal | None:
        return None if self.recent_mean is None else self.recent_mean * HOURS_PER_YEAR

    @property
    def current_annualized(self) -> Decimal:
        return self.current * HOURS_PER_YEAR


def read(history: Sequence[FundingPoint], as_of_ms: int, params: Params) -> Reading | None:
    """The reading at ``as_of_ms`` from ``history``; ``None`` when nothing settled before it."""
    prior = [p for p in history if p.time < as_of_ms]
    if not prior:
        return None
    current = max(prior, key=lambda p: p.time)
    z, samples = funding_zscore(prior, current.rate, current.time, params.window_days)
    floor = current.time - RECENT_HOURS * FUNDING_INTERVAL_MS
    recent = [p.rate for p in prior if floor < p.time <= current.time]
    recent_mean = (
        sum(recent, Decimal(0)) / len(recent) if len(recent) >= MIN_RECENT_SAMPLES else None
    )
    return Reading(
        at_ms=current.time,
        current=current.rate,
        z=z,
        samples=samples,
        recent_mean=recent_mean,
        recent_samples=len(recent),
    )


def decide(reading: Reading | None, position: Position, as_of_ms: int, params: Params) -> Action:
    """The action at ``as_of_ms`` (module docstring, step 2)."""
    if position.side is Side.OUT:
        if reading is None or reading.z is None:
            return Action.STAY_OUT
        if reading.z >= params.z_in and reading.current > 0:
            return Action.ENTER
        return Action.STAY_OUT
    if reading is None:
        return Action.HOLD
    assert position.entered_at_ms is not None  # Position's invariant
    if as_of_ms - position.entered_at_ms < params.min_hold_days * MS_PER_DAY:
        return Action.HOLD
    if reading.z is not None and reading.z <= params.z_out:
        return Action.EXIT
    if reading.recent_mean is not None and reading.recent_mean <= 0:
        return Action.EXIT
    return Action.HOLD


def advance(position: Position, action: Action, as_of_ms: int) -> Position:
    """The position after ``action`` taken at ``as_of_ms``."""
    if action is Action.ENTER:
        return Position(Side.IN, as_of_ms)
    if action is Action.HOLD:
        if position.side is not Side.IN:
            raise SignalError("hold is an action taken while in; the book is out")
        return position
    return OUT
