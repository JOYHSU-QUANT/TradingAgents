"""The rule run over stored funding history, one boundary a day, and what it collected.

Not a backtest of a book: no fills, no fees, no spot leg, no gas. It
answers the question the plan asks before the parameters are fixed
(carry plan §2 D6): how often would the rule have been in, how often did
it trade, and what did the perp leg collect while in — the sum of the
hourly rates settled while the position was on, as a fraction of the
notional held, since a short perp receives each positive settlement. The
report that nets fees and gas against it is plan PR 4, over real runs.

Every boundary is driven through :func:`.signal.read`, :func:`.signal.decide`
and :func:`.signal.advance`, the same three calls the live coordinator
makes, from the settlements strictly before it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .handoff import iso_utc
from .signal import (
    HOURS_PER_YEAR,
    OUT,
    Action,
    Params,
    Position,
    Reading,
    Side,
    advance,
    decide,
    read,
)
from .upstream import FUNDING_INTERVAL_MS, MS_PER_DAY, FundingPoint

__all__ = [
    "DEFAULT_MAX_READING_AGE_HOURS",
    "DayRow",
    "Summary",
    "floor_day",
    "format_summary",
    "pct",
    "replay",
]

# The live coordinator refuses to decide on a settlement older than this; the
# replay leaves such a boundary undecided for the same reason (one rule).
DEFAULT_MAX_READING_AGE_HOURS = 3


def floor_day(ms: int) -> int:
    """The UTC day boundary at or before ``ms``."""
    return ms - ms % MS_PER_DAY


def _ceil_day(ms: int) -> int:
    return floor_day(ms + MS_PER_DAY - 1)


def pct(value: Decimal | None) -> str:
    """A fraction as the operator reads it: two places of percent, or ``n/a``."""
    return "n/a" if value is None else f"{value * 100:.2f}%"


@dataclass(frozen=True)
class DayRow:
    """One boundary: what was read, what was done, and what the next day settled."""

    boundary_ms: int
    reading: Reading | None
    action: Action
    position: Position
    collected: Decimal
    """The sum of the rates settled in the day after the boundary, while in; 0 while out."""
    settlements: int
    """How many settlements that sum holds."""
    stale: bool = False
    """The reading was older than the live age limit, so the boundary was left undecided."""


@dataclass(frozen=True)
class Summary:
    coin: str
    first_boundary_ms: int | None
    last_boundary_ms: int | None
    days: int
    days_without_z: int
    days_stale: int
    max_reading_age_hours: int
    days_in: int
    entries: int
    exits: int
    longest_hold_days: int
    hours_in: int
    collected: Decimal

    @property
    def annualized_while_in(self) -> Decimal | None:
        if self.hours_in == 0:
            return None
        return self.collected / self.hours_in * HOURS_PER_YEAR

    @property
    def annualized_over_span(self) -> Decimal | None:
        if self.days == 0:
            return None
        return self.collected / (self.days * 24) * HOURS_PER_YEAR


def replay(
    coin: str,
    points: Sequence[FundingPoint],
    params: Params,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    max_reading_age_hours: int = DEFAULT_MAX_READING_AGE_HOURS,
) -> tuple[list[DayRow], Summary]:
    """Drive the rule over ``points`` at every UTC day boundary the window can be read at.

    The first boundary is the first one at least ``window_days`` after the
    oldest settlement, so the first z-score has a window behind it (one
    sample short of full, because the current settlement is the window's
    open edge and is not in its own sample); the last is the last boundary
    whose following day is fully
    settled, so every row's ``collected`` covers a whole day and the span
    annualisation counts no partial day. ``since_ms`` / ``until_ms``
    narrow that span (rounded to boundaries), never widen it. A boundary
    whose latest settlement is more than ``max_reading_age_hours`` before
    it is left undecided, as the live coordinator would refuse it: the
    position carries, and the row is counted as stale.
    """
    rows: list[DayRow] = []
    ordered = sorted(points, key=lambda p: p.time)
    max_age_ms = max_reading_age_hours * FUNDING_INTERVAL_MS
    if not ordered:
        return rows, _summary(coin, rows, max_reading_age_hours)
    times = [p.time for p in ordered]
    first = _ceil_day(ordered[0].time + params.window_days * MS_PER_DAY)
    last = floor_day(ordered[-1].time - MS_PER_DAY)
    if since_ms is not None:
        first = max(first, _ceil_day(since_ms))
    if until_ms is not None:
        last = min(last, floor_day(until_ms))
    position = OUT
    reach = (params.window_days + 1) * MS_PER_DAY
    boundary = first
    while boundary <= last:
        lo = bisect_left(times, boundary - reach)
        hi = bisect_left(times, boundary)
        reading = read(ordered[lo:hi], boundary, params)
        stale = reading is not None and boundary - reading.at_ms > max_age_ms
        action = decide(None if stale else reading, position, boundary, params)
        position = advance(position, action, boundary)
        collected = Decimal(0)
        settlements = 0
        if position.side is Side.IN:
            start = bisect_right(times, boundary)
            stop = bisect_right(times, boundary + MS_PER_DAY)
            collected = sum((p.rate for p in ordered[start:stop]), Decimal(0))
            settlements = stop - start
        rows.append(DayRow(boundary, reading, action, position, collected, settlements, stale))
        boundary += MS_PER_DAY
    return rows, _summary(coin, rows, max_reading_age_hours)


def _summary(coin: str, rows: Sequence[DayRow], max_reading_age_hours: int) -> Summary:
    longest = current = 0
    for row in rows:
        if row.position.side is Side.IN:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return Summary(
        coin=coin,
        first_boundary_ms=rows[0].boundary_ms if rows else None,
        last_boundary_ms=rows[-1].boundary_ms if rows else None,
        days=len(rows),
        days_without_z=sum(1 for r in rows if r.reading is None or r.reading.z is None),
        days_stale=sum(1 for r in rows if r.stale),
        max_reading_age_hours=max_reading_age_hours,
        days_in=sum(1 for r in rows if r.position.side is Side.IN),
        entries=sum(1 for r in rows if r.action is Action.ENTER),
        exits=sum(1 for r in rows if r.action is Action.EXIT),
        longest_hold_days=longest,
        hours_in=sum(r.settlements for r in rows),
        collected=sum((r.collected for r in rows), Decimal(0)),
    )


def format_summary(summary: Summary, params: Params) -> list[str]:
    """The operator's lines for ``summary``."""
    if summary.days == 0:
        return [
            f"carry history: {summary.coin}: no boundary with a full {params.window_days}d window"
        ]
    assert summary.first_boundary_ms is not None and summary.last_boundary_ms is not None
    share = Decimal(summary.days_in) / Decimal(summary.days)
    return [
        (
            f"carry history: {summary.coin}, {summary.days} boundaries from "
            f"{iso_utc(summary.first_boundary_ms)} to {iso_utc(summary.last_boundary_ms)} "
            f"(window {params.window_days}d, z in {params.z_in:g} / out {params.z_out:g}, "
            f"min hold {params.min_hold_days}d)"
        ),
        (
            f"  boundaries without a z-score: {summary.days_without_z}; with a reading older "
            f"than {summary.max_reading_age_hours}h, left undecided as live would: "
            f"{summary.days_stale}"
        ),
        (
            f"  in market: {summary.days_in} days ({pct(share)}); "
            f"entries {summary.entries}, exits {summary.exits}; "
            f"longest hold {summary.longest_hold_days} days"
        ),
        (
            f"  collected while in: {pct(summary.collected)} of notional over "
            f"{summary.hours_in} settlements; {pct(summary.annualized_while_in)} annualized "
            f"while in, {pct(summary.annualized_over_span)} annualized over the span"
        ),
    ]
