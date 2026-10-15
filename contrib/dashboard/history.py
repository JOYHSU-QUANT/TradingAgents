"""The history the exporter keeps beside the documents: a few series per line, one file each.

A line's document carries a tail — the perp store's recent account snapshots,
a Uniswap run's recent valuations — and the page's sparkline draws that tail
and no more. The history (dashboard plan PR 5) keeps what the tails carried
past: every tick the exporter **harvests** the numbers a document holds
(:func:`harvest`, one small whitelist per kind), and **advances** the line's
history with them (:func:`advance`): a point is appended when its instant is
later than the series' last, so a tail seen twice adds nothing and a tail
that overlaps the last tick's fills the gap; the oldest points fall off past
``max_points``. The carry report is not a tail but the sleeve's whole span,
and a day of it is revised after the fact (a settlement priced later), so a
carry series is **replaced** by the document's each tick rather than
appended to. The host is sampled each tick (disk, memory, load) and thinned
to one point per ``min_interval``.

A history belongs to a run: when the line's run label changes (a segment
switch), the history starts afresh — the old segment's numbers are in its
store, and a curve across two segments would join two opening balances.

The file is ``history/<line>.json`` under the status directory (the page
derives the name from the line's name in ``exporter.json``, never by
globbing), written 0640 like every document (:mod:`.files`). Its points
hold instants and plain decimal digits and nothing else, and the types
hold that: a :class:`Series` refuses a point not spelled as this module
writes one, not parsable, past float's range or out of order, and a
:class:`History` refuses an empty name or an unspelled instant — so what
``advance`` builds is what ``read_history`` accepts, and what the page
parses never fails to parse. Nothing written is free text from a document
(the names and labels are this package's), so nothing in it is anyone's
address or key (``tests/test_history.py`` holds that). A file this build
cannot read is left as it is and named in ``exporter.json``
(``history_error``): a tick never overwrites a history it could not read.

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
    "HARVESTED_KINDS",
    "HISTORY_DIR",
    "HISTORY_SCHEMA_VERSION",
    "HOST_KIND",
    "MIN_POINTS",
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
# A history keeps at least this many points per series (one point is no curve).
MIN_POINTS = 2
# A point is ``(instant, value)``: the instant as :func:`times.iso` writes it
# (UTC, to the second), the value as plain decimal digits. The instants are
# one spelling, so "later" is a text comparison.
Point = tuple[str, str]
_INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


class HistoryError(RuntimeError):
    """A history file that is not one this build reads."""


class HarvestError(RuntimeError):
    """A document not shaped as this build harvests it; the message names the class, never the text."""


def _check_text(value: str, what: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is not text")


def _check_instant(value: str, what: str) -> None:
    """``value`` is an instant as this module writes one, and one a reader can parse."""
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise ValueError(f"{what} is not an instant as this build writes one")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{what} is not a calendar instant") from exc


def _check_value(value: str) -> None:
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise ValueError("a point's value is not decimal text as this build writes it")
    if not isfinite(float(value)):
        raise ValueError("a point's value is past float's range")


@dataclass(frozen=True)
class Series:
    """One curve: its label as the page titles it, its points oldest first.

    Holds what a reader and a chart rely on: every point is spelled as this
    module writes one and parses, the values are within float's range, and
    the instants strictly rise.
    """

    label: str
    points: tuple[Point, ...]

    def __post_init__(self) -> None:
        _check_text(self.label, "a series label")
        for point in self.points:
            if not isinstance(point, tuple) or len(point) != 2:
                raise ValueError("a point is (instant, value)")
            _check_instant(point[0], "a point's instant")
            _check_value(point[1])
        if any(
            later[0] <= earlier[0]
            for earlier, later in zip(self.points, self.points[1:], strict=False)
        ):
            raise ValueError("points do not rise")


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
        _check_text(self.line, "line")
        _check_text(self.kind, "kind")
        _check_text(self.run, "run")
        _check_instant(self.generated_at, "generated_at")
        if isinstance(self.max_points, bool) or not isinstance(self.max_points, int):
            raise ValueError("max_points is not an integer")
        if self.max_points < MIN_POINTS:
            raise ValueError(f"a history keeps at least {MIN_POINTS} points per series")
        for name, series in self.series.items():
            _check_text(name, "a series name")
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
    """One point per row that carries a value under ``value_key``, oldest first, one per second.

    A row with no value is skipped. Two rows in one second (the perp store
    stamps microseconds; a fill and the cycle's end can share a second) are
    the later one; a row earlier than the one before it (a clock step) is
    skipped — a :class:`Series` holds rising instants.
    """
    points: list[Point] = []
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        point = (instant(row.get(instant_key)), _decimal_text(value))
        if points and point[0] <= points[-1][0]:
            if point[0] == points[-1][0]:
                points[-1] = point
            continue
        points.append(point)
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
# The perp run's total pnl is its equity less the opening balance, so the
# equity curve says it; it is not kept twice.
_HYPERLIQUID = (
    ("equity", "account equity (USDC)", "account_equity"),
    ("notional", "position notional (USDC)", "total_position_notional"),
)
_UNISWAP = (("value", "value after the bar (quote)", "total_value"),)
_CARRY = (
    ("net", "net per day (USDC)", "net"),
    ("perp_equity", "perp leg equity (USDC)", "perp_equity"),
    ("spot_equity", "spot leg equity (USDC)", "spot_equity"),
)
_HOST = (
    ("disk_used_pct", "disk used (%)", "disk_used_pct"),
    ("memory_available_mb", "memory available (MB)", "memory_available_mb"),
    ("load_1m", "load average, 1 min", "load_1m"),
)
# The kinds a document is harvested for: the exporter's line kinds and the
# host (``tests`` hold the first three to the config's kinds).
HARVESTED_KINDS = frozenset({"hyperliquid", "uniswap", "carry", HOST_KIND})
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
            "net, cumulative (USDC)",
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
    raise HarvestError(f"a line of kind {kind!r} is not one this build harvests")


def harvest(kind: str, document: Mapping[str, Any]) -> dict[str, Series]:
    """The series a document of ``kind`` carries, each oldest first.

    :class:`HarvestError` for a document not shaped as this build reads it
    (named by the exception's class, never its text) and for a kind this
    build does not harvest (:data:`HARVESTED_KINDS`).
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
    """``history`` carried on by ``harvested``; a fresh one when there was none or the run or kind changed.

    A carry history takes the harvest whole (its newest ``max_points``);
    any other appends what is newer than its series' last point. A series
    the harvest does not name keeps what it has, cut to ``max_points`` (the
    setting may have been lowered since it was written). What is returned
    is a :class:`History` a reader accepts, by construction.
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


def _series(item: object) -> Series:
    """A stored series as a :class:`Series` (which holds the points to their spellings)."""
    if not isinstance(item, Mapping):
        raise HistoryError("a series is a mapping")
    raw = item.get("points")
    if not isinstance(raw, list):
        raise HistoryError("points is not a list")
    points: list[Point] = []
    for point in raw:
        if not isinstance(point, list) or len(point) != 2:
            raise HistoryError("a point is [instant, value]")
        points.append((point[0], point[1]))
    label = item.get("label")
    return Series(label if isinstance(label, str) else "", tuple(points))


def read_history(document: Mapping[str, Any]) -> History:
    """A loaded JSON object as a :class:`History`; ``HistoryError`` when it is not one this build reads."""
    if document.get("schema_version") != HISTORY_SCHEMA_VERSION:
        raise HistoryError(f"schema_version is not {HISTORY_SCHEMA_VERSION}")
    raw_series = document.get("series")
    if not isinstance(raw_series, Mapping):
        raise HistoryError("series is not a mapping")
    try:
        return History(
            line=document.get("line"),  # type: ignore[arg-type]  # the model checks
            kind=document.get("kind"),  # type: ignore[arg-type]
            run=document.get("run"),  # type: ignore[arg-type]
            generated_at=document.get("generated_at"),  # type: ignore[arg-type]
            max_points=document.get("max_points"),  # type: ignore[arg-type]
            series={
                name if isinstance(name, str) else "": _series(item)
                for name, item in raw_series.items()
            },
        )
    except ValueError as exc:  # the models' own checks
        raise HistoryError(str(exc)) from exc


def load_history(path: Path) -> History | None:
    """The history at ``path``, ``None`` when there is no file yet; ``HistoryError`` for one this build cannot read."""
    try:
        document = read_json_object(path)
    except ValueError as exc:
        raise HistoryError(str(exc)) from exc
    return None if document is None else read_history(document)
