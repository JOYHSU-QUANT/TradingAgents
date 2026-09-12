"""Does a stored series actually cover the ground it appears to cover?

A backtest reads a series as a grid: slot ``k``, then slot ``k+1``. A store can
satisfy every invariant on every ROW and still not be a grid — a venue outage,
an interrupted backfill, a page the venue truncated — and nothing about the
rows says so. Read as a grid anyway, a hole becomes a single enormous return
between two adjacent-looking bars, which is the shape a momentum rule likes
most. So the store is scanned before anything is measured on it, and what it
finds is RECORDED here (plan §3.4); refusing to evaluate across a hole is the
evaluator's job, later.

Every stamp is assigned to the slot nearest it, and the three findings are
what can go wrong with that assignment:

- a **gap** — two occupied slots with empty ones between them. The series is
  on the grid but incomplete; re-fetching that window is the remedy.
- a **duplicate slot** — two stamps in the SAME slot. Not a cosmetic
  duplicate: two funding settlements inside one hour double-count the carry
  of that hour, and the row count looks healthier for it.
- a **misalignment** — a stamp too far from any slot to be in one. No
  re-fetch repairs this; it means the venue changed cadence, or two cadences
  were written into one series (a ``1d`` page landing in the ``4h`` rows).

"Too far" is a per-series tolerance rather than exact equality, because the
venue stamps its two series differently and measuring both as exact was wrong
on live data: a funding settlement is stamped when it POSTS, tens of
milliseconds past the hour, so an exact grid called 524 of 531 real
settlements misaligned. The tolerances, and the readings behind them, live in
:mod:`~contrib.autoresearch.constants`.

This module measures and says what it found. It never deletes, never fills,
and never writes a rounded stamp back: slots are how the scan READS the
series, not something it does to the store. A research store that quietly
repaired itself would answer the same question differently before and after,
and the whole point of the scan is to be the thing that cannot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .constants import (
    CANDLE_STAMP_TOLERANCE_MS,
    FUNDING_INTERVAL_MS,
    FUNDING_STAMP_TOLERANCE_MS,
)
from .store import ResearchStore
from .upstream import from_epoch_ms, interval_to_ms, parse_interval

__all__ = [
    "Gap",
    "GapReport",
    "render_report",
    "scan_candles",
    "scan_funding",
    "scan_stamps",
]

# How many findings of one kind a rendered report lists before summarising the
# rest. A store whose backfill never ran has thousands, and a wall of them
# buries the one line an operator needs (the count, and where the series
# actually starts).
_MAX_LISTED = 10


@dataclass(frozen=True)
class Gap:
    """One hole: the series jumps from ``after_ms`` to ``before_ms``, missing ``missing`` slots."""

    after_ms: int
    before_ms: int
    missing: int


@dataclass(frozen=True)
class GapReport:
    """What one series looks like on its own grid.

    ``complete`` is deliberately a property rather than a stored flag: the
    verdict is nothing but "none of the three findings", and a stored copy of
    it could disagree with the findings beside it.
    """

    label: str
    step_ms: int
    tolerance_ms: int
    rows: int
    first_ms: int | None
    last_ms: int | None
    gaps: tuple[Gap, ...]
    duplicate_ms: tuple[int, ...]
    misaligned_ms: tuple[int, ...]

    def __post_init__(self) -> None:
        # The one coupling this object has, checked where it is built rather
        # than assumed where it is read. ``rows == 0`` and "there is no span"
        # are the same statement, and a report carrying findings about stamps
        # it says it has none of would be read as authoritative. The house
        # style is the same: the venue DTOs this package borrows check their
        # own invariants in ``__post_init__`` too.
        empty = self.first_ms is None
        if empty != (self.last_ms is None) or empty != (self.rows == 0):
            raise ValueError(
                f"GapReport for {self.label!r} is inconsistent: rows={self.rows} "
                f"but span is {self.first_ms}..{self.last_ms}"
            )
        if empty and not self.complete:
            raise ValueError(f"GapReport for {self.label!r} has findings but no rows")

    @property
    def complete(self) -> bool:
        return not (self.gaps or self.duplicate_ms or self.misaligned_ms)

    @property
    def missing_rows(self) -> int:
        """How many slots of the covered span have no row."""
        return sum(gap.missing for gap in self.gaps)


def scan_stamps(label: str, step_ms: int, tolerance_ms: int, stamps: Sequence[int]) -> GapReport:
    """The one scan both series go through, over already-sorted, unique stamps.

    Public, because it is the DEFINITION of a hole: the evaluator refuses to
    measure a window across one (plan §3.4), and a second definition there
    would let ``gaps`` call a series complete that the evaluator refuses.

    Sorted and unique is a fact about the readers, not a hope: each table's
    primary key makes the stamp unique within a series, and both readers
    ``ORDER BY`` it. Unique STAMPS still allow two stamps in one SLOT, which
    is why that is a finding of its own rather than an impossibility.

    The grid is anchored on the FIRST stamp rather than on the epoch. A venue
    whose bars sit at, say, 02:00 rather than 00:00 is perfectly regular, and
    an epoch-anchored grid would call every row of such a series misaligned.
    Anchoring on the first stamp also keeps the residual below a difference of
    two jitters rather than something that accumulates along the series.
    """
    if not stamps:
        return GapReport(
            label=label,
            step_ms=step_ms,
            tolerance_ms=tolerance_ms,
            rows=0,
            first_ms=None,
            last_ms=None,
            gaps=(),
            duplicate_ms=(),
            misaligned_ms=(),
        )
    first = stamps[0]
    occupied: list[tuple[int, int]] = []  # (stamp, slot index), in stamp order
    misaligned: list[int] = []
    for stamp in stamps:
        offset = stamp - first
        # Integer arithmetic throughout: the nearest slot by adding half a step
        # before the floor division, so the residual lands in [-step/2, step/2)
        # exactly. Dividing through a float would be fine at today's magnitudes
        # and a silent trap at larger ones.
        slot = (offset + step_ms // 2) // step_ms
        if abs(offset - slot * step_ms) > tolerance_ms:
            misaligned.append(stamp)
        else:
            occupied.append((stamp, slot))
    gaps = []
    duplicates = []
    for (earlier, slot_a), (later, slot_b) in zip(occupied, occupied[1:], strict=False):
        if slot_b == slot_a:
            # Report the SECOND of the pair: it is the one the series had no
            # room for, and naming the first would point an operator at the row
            # that is probably fine.
            duplicates.append(later)
        elif slot_b - slot_a > 1:
            gaps.append(Gap(after_ms=earlier, before_ms=later, missing=slot_b - slot_a - 1))
    return GapReport(
        label=label,
        step_ms=step_ms,
        tolerance_ms=tolerance_ms,
        rows=len(stamps),
        first_ms=first,
        last_ms=stamps[-1],
        gaps=tuple(gaps),
        duplicate_ms=tuple(duplicates),
        misaligned_ms=tuple(misaligned),
    )


def scan_candles(store: ResearchStore, *, coin: str, interval: str) -> GapReport:
    """Scan the ``coin``/``interval`` candle series on its own interval grid.

    Read through :meth:`~contrib.autoresearch.store.ResearchStore.iter_candles`
    rather than by selecting ``open_time`` alone: that reader rebuilds each row
    as a ``Candle``, so a row whose prices or stamps were corrupted in the
    store fails here, where an operator is already looking at data quality,
    instead of surviving behind a clean-looking gap report.
    """
    key = parse_interval(interval).value
    stamps = [c.open_time for c in store.iter_candles(coin, key)]
    return scan_stamps(
        f"{coin} {key} candles", interval_to_ms(key), CANDLE_STAMP_TOLERANCE_MS, stamps
    )


def scan_funding(store: ResearchStore, *, coin: str) -> GapReport:
    """Scan ``coin``'s funding series on the venue's hourly settlement grid."""
    stamps = [p.time for p in store.iter_funding(coin)]
    return scan_stamps(f"{coin} funding", FUNDING_INTERVAL_MS, FUNDING_STAMP_TOLERANCE_MS, stamps)


def _stamp(ms: int) -> str:
    """A venue stamp as a readable UTC instant, decoded the way the venue's own are."""
    return from_epoch_ms(ms).isoformat()


def _listed(lines: list[str], stamps: Sequence[int], *, prefix: str, noun: str) -> None:
    """Append at most :data:`_MAX_LISTED` stamps, then say how many were left out."""
    for stamp in stamps[:_MAX_LISTED]:
        lines.append(f"  {prefix}: {_stamp(stamp)}")
    if len(stamps) > _MAX_LISTED:
        lines.append(f"  ... and {len(stamps) - _MAX_LISTED} more {noun}")


def render_report(report: GapReport) -> list[str]:
    """The report as lines to print — one summary line, then the findings.

    A series with nothing wrong still gets a line. "The scan found no gaps"
    and "the scan did not run" are different facts, and a report that printed
    nothing for the good case would make them look identical.
    """
    if report.first_ms is None or report.last_ms is None:
        # Branching on the span itself, not on ``rows``: they say the same
        # thing (``__post_init__`` refuses a report where they disagree), and
        # only this spelling proves it to a reader — or a type checker —
        # standing at the line below.
        return [f"{report.label}: no rows stored"]
    span = f"{_stamp(report.first_ms)} .. {_stamp(report.last_ms)}"
    head = f"{report.label}: {report.rows} rows, {span}"
    if report.complete:
        return [f"{head} - no gaps"]
    lines = [
        f"{head} - {len(report.gaps)} gap(s), {report.missing_rows} row(s) missing,"
        f" {len(report.duplicate_ms)} duplicate slot(s),"
        f" {len(report.misaligned_ms)} off-grid stamp(s)"
    ]
    for gap in report.gaps[:_MAX_LISTED]:
        lines.append(
            f"  gap: {_stamp(gap.after_ms)} -> {_stamp(gap.before_ms)} ({gap.missing} missing)"
        )
    if len(report.gaps) > _MAX_LISTED:
        lines.append(f"  ... and {len(report.gaps) - _MAX_LISTED} more gap(s)")
    _listed(lines, report.duplicate_ms, prefix="duplicate slot", noun="duplicate slot(s)")
    _listed(lines, report.misaligned_ms, prefix="off-grid", noun="off-grid stamp(s)")
    return lines
