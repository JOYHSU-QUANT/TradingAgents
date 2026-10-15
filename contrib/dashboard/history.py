"""The history the exporter keeps beside the documents: a few series per line, one file each.

A line's document carries a tail — the perp store's recent account snapshots,
a Uniswap run's recent valuations, the carry report's days — and the page's
sparkline draws that tail and no more. The history (dashboard plan PR 5)
keeps what the tails carried past: every tick the exporter **harvests** the
numbers a tail holds (:func:`harvest`, one small whitelist per kind), and
**advances** the line's history with them (:func:`advance`): a point is
appended when its instant is later than the series' last, so a tail seen
twice adds nothing and a tail that overlaps the last tick's fills the gap;
the oldest points fall off past ``max_points``. The carry report is not a
tail but the sleeve's whole span, and a day of it is revised after the
fact (a settlement priced later), so a carry series is **replaced** by the
document's each tick rather than appended to. The host is sampled each tick
(disk, memory, load) and thinned to one point per ``min_interval``.

A history belongs to a run: when the line's run label changes (a segment
switch), the history starts afresh — the old segment's numbers are in its
store, and a curve across two segments would join two opening balances.

The file is ``history/<line>.json`` under the status directory (the page
derives the name from the line's name in ``exporter.json``, never by
globbing), written 0640 like every document (:mod:`.files`). It holds
instants and decimal texts and nothing else — the reader holds every point
to the two spellings this module writes, and nothing it writes is free text
from a document, so nothing in it is anyone's address or key
(``tests/test_history.py`` holds that). A file this build cannot read is
left as it is and named in ``exporter.json`` (``history_error``): a tick
never overwrites a history it could not read.

This module imports nothing that the page's venv does not ship (no YAML):
the page reads these files through :func:`load_history` too.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import accumulate
from math import isfinite
from pathlib import Path
from typing import Any

from .files import read_json_object
from .times import iso, parse_when

__all__ = [
    "HISTORY_DIR",
    "HISTORY_SCHEMA_VERSION",
    "HOST_KIND",
    "HarvestError",
    "History",
    "HistoryError",
    "Point",
    "Series",
    "advance",
    "dump",
    "harvest",
    "history_file",
    "load_history",
    "parse_point",
    "read_history",
]

HISTORY_DIR = "history"
HISTORY_SCHEMA_VERSION = 1
# The host's history goes under this name (no line is named so: it is reserved).
HOST_KIND = "host"
# A point is ``(instant, value)``: the instant as :func:`times.iso` writes it
# (UTC, to the second), the value as plain decimal digits. The reader holds
# a stored point to these two spellings and parses nothing (a stored
# instant compares as text, so "later" is a string comparison).
Point = tuple[str, str]
_INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


class HistoryError(RuntimeError):
    """A history file that is not one this build reads."""


class HarvestError(RuntimeError):
    """A document not shaped as this build harvests it; the message names the class, never the text."""


@dataclass(frozen=True)
class Series:
    """One curve: its label as the page titles it, its points oldest first."""

    label: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class History:
    """One ``history/<line>.json``."""

    line: str
    kind: str
    run: str
    generated_at: str
    max_points: int
    series: Mapping[str, Series]

    def __post_init__(self) -> None:
        if self.max_points < 2:
            raise ValueError("a history keeps at least two points per series")
        for name, series in self.series.items():
            if len(series.points) > self.max_points:
                raise ValueError(f"series {name!r} holds more than max_points")


def history_file(name: str) -> str:
    """The file a line's (or the host's) history is written to, relative to the status directory."""
    return f"{HISTORY_DIR}/{name}.json"


# --- the points -------------------------------------------------------------------


def _instant_text(value: object) -> str:
    """An instant as any document spells it, as :func:`iso` writes it."""
    if not isinstance(value, str) or not value:
        raise TypeError("an instant is text")
    return iso(parse_when(value))


def _day_text(value: object) -> str:
    """A UTC day (``2026-10-14``, the carry report's boundaries) as :func:`iso` writes its midnight."""
    if not isinstance(value, str):
        raise TypeError("a day is text")
    day = date.fromisoformat(value)
    return iso(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))


def _decimal_text(value: object) -> str:
    """``value`` (a document's decimal text or number) as plain digits; refused when not a finite number.

    Refused too past float's range: a chart plots the value as a float, so
    one it cannot plot never enters a file.
    """
    if value is None or isinstance(value, bool):
        raise TypeError("a point's value is a number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("a point's value is a number") from exc
    if not number.is_finite() or not isfinite(float(number)):
        raise ValueError("a point's value is a finite number within float's range")
    return format(number, "f")


def parse_point(point: Point) -> tuple[datetime, Decimal]:
    """A stored point as the page reads it: ``(instant, value)``."""
    return parse_when(point[0]), Decimal(point[1])


def _points(
    rows: Sequence[Mapping[str, Any]],
    instant_key: str,
    value_key: str,
    instant: Callable[[object], str] = _instant_text,
) -> tuple[Point, ...]:
    """One point per row that carries a value under ``value_key``; a row with none is skipped."""
    points: list[Point] = []
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        points.append((instant(row.get(instant_key)), _decimal_text(value)))
    return tuple(points)


def _rows(value: object) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("a tail is a list")
    rows = list(value)
    if any(not isinstance(row, Mapping) for row in rows):
        raise TypeError("a tail's rows are mappings")
    return rows


def _mapping(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("a block of the document is a mapping")
    return value


# --- harvesting ---------------------------------------------------------------------

# What is harvested per kind: ``(series name, label, value key)`` off the
# tail named in :func:`harvest`; the label is what the page titles the chart.
_HYPERLIQUID = (
    ("equity", "account equity (USDC)", "account_equity"),
    ("total_pnl", "total pnl (USDC)", "total_pnl"),
    ("notional", "position notional (USDC)", "total_position_notional"),
)
_UNISWAP = (("value", "value after the bar (quote)", "total_value"),)
_CARRY = (
    ("net", "net per day", "net"),
    ("perp_equity", "perp leg equity", "perp_equity"),
    ("spot_equity", "spot leg equity", "spot_equity"),
)
_HOST = (
    ("disk_used_pct", "disk used (%)", "disk_used_pct"),
    ("memory_available_mb", "memory available (MB)", "memory_available_mb"),
    ("load_1m", "load average, 1 min", "load_1m"),
)
# The kinds whose document is the whole span, not a tail: their series are
# replaced each tick (a day is revised after the fact), never appended to.
_REPLACED_KINDS = frozenset({"carry"})


def _harvest(kind: str, document: Mapping[str, Any]) -> dict[str, Series]:
    if kind == "hyperliquid":
        # The perp document's tails are newest first.
        snapshots = _rows(_mapping(document.get("account")).get("recent_snapshots"))[::-1]
        return {
            name: Series(label, _points(snapshots, "timestamp", key))
            for name, label, key in _HYPERLIQUID
        }
    if kind == "uniswap":
        valuations = _rows(_mapping(document.get("run")).get("recent_valuations"))
        return {
            name: Series(label, _points(valuations, "time", key)) for name, label, key in _UNISWAP
        }
    if kind == "carry":
        days = _rows(document.get("days"))
        series = {
            name: Series(label, _points(days, "boundary", key, _day_text))
            for name, label, key in _CARRY
        }
        # The running total over the span: exact, since the carry series are
        # replaced whole each tick (a revised day revises the total after it).
        net = series["net"].points
        totals = accumulate((Decimal(value) for _, value in net), initial=Decimal(0))
        next(totals)
        series["net_cumulative"] = Series(
            "net, cumulative",
            tuple((at, format(total, "f")) for (at, _), total in zip(net, totals, strict=True)),
        )
        return series
    if kind == HOST_KIND:
        sample: dict[str, Any] = {"at": document.get("generated_at")}
        disk = _mapping(document.get("disk"))
        if disk:
            sample["disk_used_pct"] = f"{float(disk['used_pct']):.1f}"
        memory = _mapping(document.get("memory"))
        if memory:
            sample["memory_available_mb"] = int(memory["available_kb"]) // 1024
        load = document.get("load_average")
        if load is not None:
            sample["load_1m"] = f"{float(load[0]):.2f}"
        return {name: Series(label, _points([sample], "at", key)) for name, label, key in _HOST}
    return {}


def harvest(kind: str, document: Mapping[str, Any]) -> dict[str, Series]:
    """The series a document of ``kind`` carries, each oldest first.

    :class:`HarvestError` for a document not shaped as this build reads it
    (named by the exception's class, never its text). A kind this build does
    not harvest gives no series.
    """
    try:
        return _harvest(kind, document)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError) as exc:
        raise HarvestError(type(exc).__name__) from exc


# --- advancing ------------------------------------------------------------------------


def _append(
    kept: Sequence[Point], new: Sequence[Point], *, min_interval: timedelta, max_points: int
) -> tuple[Point, ...]:
    """``kept`` plus those of ``new`` later than its last by ``min_interval``, the oldest cut past ``max_points``."""
    points = list(kept)
    last = parse_when(points[-1][0]) if points else None
    # Never two points at one instant: the floor is one second.
    gap = max(min_interval, timedelta(seconds=1))
    for point in new:
        moment = parse_when(point[0])
        if last is not None and moment - last < gap:
            continue
        points.append(point)
        last = moment
    return tuple(points[-max_points:])


def advance(
    history: History | None,
    *,
    line: str,
    kind: str,
    run: str,
    harvested: Mapping[str, Series],
    now: datetime,
    max_points: int,
    min_interval: timedelta = timedelta(0),
) -> History:
    """``history`` carried on by ``harvested``; a fresh one when there was none or the run changed.

    A carry history takes the harvest whole (its newest ``max_points``);
    any other appends what is newer than its series' last point. A series
    the harvest does not name keeps what it has, cut to ``max_points`` (the
    setting may have been lowered since it was written).
    """
    if history is not None and (history.run != run or history.kind != kind):
        history = None
    series = (
        {name: Series(s.label, s.points[-max_points:]) for name, s in history.series.items()}
        if history is not None
        else {}
    )
    for name, harvested_series in harvested.items():
        kept = series[name].points if (kind not in _REPLACED_KINDS and name in series) else ()
        series[name] = Series(
            harvested_series.label,
            _append(
                kept, harvested_series.points, min_interval=min_interval, max_points=max_points
            ),
        )
    return History(
        line=line,
        kind=kind,
        run=run,
        generated_at=iso(now),
        max_points=max_points,
        series=series,
    )


# --- the file ----------------------------------------------------------------------


def dump(history: History) -> dict[str, Any]:
    """The history as its JSON document."""
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "source": "dashboard",
        "generated_at": history.generated_at,
        "line": history.line,
        "kind": history.kind,
        "run": history.run,
        "max_points": history.max_points,
        "series": {
            name: {"label": series.label, "points": [list(point) for point in series.points]}
            for name, series in history.series.items()
        },
    }


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoryError(f"{what} is not text")
    return value


def _stored_instant(value: object, what: str) -> str:
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise HistoryError(f"{what} is not an instant as this build writes one")
    return value


def _point(value: object) -> Point:
    if not isinstance(value, list) or len(value) != 2:
        raise HistoryError("a point is [instant, value]")
    instant, amount = value
    if not isinstance(amount, str) or not _NUMBER.fullmatch(amount):
        raise HistoryError("a point's value is not decimal text as this build writes it")
    if not isfinite(float(amount)):
        raise HistoryError("a point's value is past float's range")
    return _stored_instant(instant, "a point's instant"), amount


def _series(name: object, item: object) -> tuple[str, Series]:
    if not isinstance(item, Mapping):
        raise HistoryError("a series is a mapping")
    raw = item.get("points")
    if not isinstance(raw, list):
        raise HistoryError("points is not a list")
    points = tuple(_point(point) for point in raw)
    # Oldest first, strictly: what ``advance`` writes, and what a chart draws
    # (the instants are one spelling, so they compare as text).
    if any(later[0] <= earlier[0] for earlier, later in zip(points, points[1:], strict=False)):
        raise HistoryError("points do not rise")
    return _text(name, "a series name"), Series(_text(item.get("label"), "a series label"), points)


def read_history(document: Mapping[str, Any]) -> History:
    """A loaded JSON object as a :class:`History`; ``HistoryError`` when it is not one this build reads."""
    if document.get("schema_version") != HISTORY_SCHEMA_VERSION:
        raise HistoryError(f"schema_version is not {HISTORY_SCHEMA_VERSION}")
    raw_series = document.get("series")
    if not isinstance(raw_series, Mapping):
        raise HistoryError("series is not a mapping")
    max_points = document.get("max_points")
    if isinstance(max_points, bool) or not isinstance(max_points, int):
        raise HistoryError("max_points is not an integer")
    try:
        return History(
            line=_text(document.get("line"), "line"),
            kind=_text(document.get("kind"), "kind"),
            run=_text(document.get("run"), "run"),
            generated_at=_stored_instant(document.get("generated_at"), "generated_at"),
            max_points=max_points,
            series=dict(_series(name, item) for name, item in raw_series.items()),
        )
    except ValueError as exc:  # the model's own invariants
        raise HistoryError(str(exc)) from exc


def load_history(path: Path) -> History | None:
    """The history at ``path``, ``None`` when there is no file yet; ``HistoryError`` for one this build cannot read."""
    try:
        document = read_json_object(path)
    except ValueError as exc:
        raise HistoryError(str(exc)) from exc
    return None if document is None else read_history(document)
