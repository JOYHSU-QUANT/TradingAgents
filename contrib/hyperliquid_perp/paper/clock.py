"""Injectable clock for the paper execution engine (execution §1.1 / §5.5).

The engine schedules TWAP slices on a 30-second cadence and expires plans an
hour after creation. Wiring those to ``datetime.now`` / ``time.sleep`` directly
would make the whole engine untestable (a one-hour plan would take one real
hour) and non-deterministic. So time enters the engine through this one seam:

- :class:`WallClock` — production. ``now()`` is the real UTC clock.
- :class:`ManualClock` — tests. ``now()`` returns a fixed instant the test
  advances explicitly, so a 120-slice / one-hour plan runs in microseconds and
  the same script always produces the same schedule.

Only ``now()`` is on the protocol: the engine never *sleeps*. It is driven one
tick at a time by its caller (the PR4 monitor loop binds a :class:`WallClock`
and actually waits 30 s between ticks; a test binds a :class:`ManualClock` and
advances it), so the engine's own logic stays a pure function of "what time is
it now?" — no blocking call ever hides inside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "ManualClock", "WallClock"]


@runtime_checkable
class Clock(Protocol):
    """The engine's only source of "now". Always timezone-aware UTC."""

    def now(self) -> datetime:
        """The current instant as a timezone-aware UTC :class:`datetime`."""
        ...


class WallClock:
    """The real UTC clock — production wiring."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """A clock the caller advances by hand — deterministic test wiring.

    ``advance`` moves time forward (never backward, which would let a test
    fabricate an out-of-order schedule the wall clock could never produce);
    ``set`` jumps to an explicit instant for restart / gap scenarios, and is
    the one way to move to a specific time (still never backward).
    """

    def __init__(self, start: datetime) -> None:
        self._now = self._require_utc(start, name="start")

    @staticmethod
    def _require_utc(value: datetime, *, name: str) -> datetime:
        # Parity with every other datetime boundary in this package (ids,
        # accounting, schema): a naive instant would serialize to an offset-less
        # ISO string that reads as UTC only on a UTC host. Normalise to UTC.
        if value.tzinfo is None:
            raise ValueError(f"ManualClock {name} must be timezone-aware (UTC)")
        return value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        """Move time forward by ``seconds`` (must be >= 0); return the new now."""
        if seconds < 0:
            raise ValueError(f"ManualClock.advance seconds must be >= 0, got {seconds}")
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, instant: datetime) -> datetime:
        """Jump to ``instant`` (must not be before the current now); return it."""
        instant = self._require_utc(instant, name="instant")
        if instant < self._now:
            raise ValueError(f"ManualClock.set cannot move time backward: {instant} < {self._now}")
        self._now = instant
        return self._now
