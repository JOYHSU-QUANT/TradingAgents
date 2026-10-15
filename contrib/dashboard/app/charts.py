"""A chart of one history series, as inline SVG the template prints (no script, plan §4).

The page's sparkline (:mod:`.documents`) draws a document's tail with no
axes; a chart draws a history series (:mod:`contrib.dashboard.history`)
against time — the x axis is the instants, not the index, so a gap in the
series is a gap on the page — with the series' highest and lowest values
on the left and its first and last instants below. Everything the
template needs is in the model, coordinates included, so the geometry
lives here alone; the template prints it and adds nothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from ..times import show_when, span_text

__all__ = ["MAX_DRAWN", "Chart", "Label", "chart"]

# The box, in the SVG's own units; the stylesheet scales it to the card.
_WIDTH = 640
_HEIGHT = 200
# The plot area: room on the left for the value labels, below for the instants.
_LEFT = 76
_RIGHT = _WIDTH - 8
_TOP = 16
_BOTTOM = 170
_LABEL_GAP = 10
_X_LABEL_Y = _BOTTOM + 22
# A polyline longer than this is thinned before it is printed: a card is a
# few hundred pixels wide, and a point per pixel is as much as it can show.
MAX_DRAWN = 1280

_Point = tuple[datetime, Decimal]


@dataclass(frozen=True)
class Label:
    """One ``<text>`` of the chart: what it says, where, anchored how."""

    text: str
    x: int
    y: int
    anchor: Literal["start", "end"]
    cls: Literal["x", "y"]


@dataclass(frozen=True)
class Chart:
    """One series as the template prints it."""

    title: str
    points: str
    axes: str
    labels: tuple[Label, ...]
    first: str
    last: str
    count: int
    span: str
    viewbox: str = field(init=False, default=f"0 0 {_WIDTH} {_HEIGHT}")


# A label never shows more than this many decimals (the file keeps them all).
_LABEL_DECIMALS = Decimal("0.01")


def _label(value: Decimal) -> str:
    """``value`` as a label: plain digits, at most two decimals (``10234.56``, never ``1E+4``)."""
    if value == value.to_integral_value():
        return format(value, "f")
    return format(value.quantize(_LABEL_DECIMALS, rounding=ROUND_HALF_EVEN), "f")


def _thinned(points: Sequence[_Point]) -> list[_Point]:
    """Every k-th of ``points`` so that at most :data:`MAX_DRAWN` (plus the last, always kept) are drawn."""
    if len(points) <= MAX_DRAWN:
        return list(points)
    step = math.ceil(len(points) / MAX_DRAWN)
    kept = list(points[::step])
    if kept[-1] is not points[-1]:
        kept.append(points[-1])
    return kept


def chart(points: Sequence[_Point], title: str) -> Chart | None:
    """The chart of ``points`` (oldest first, instants strictly rising); ``None`` with fewer than two.

    The values are plotted as floats (a stored value is within float's
    range, the history's reader holds it) and labelled to two decimals; the
    extremes are the whole series', thinned or not; a flat series runs
    through the middle of the box. The instants are taken as rising (a
    stored series' are); ``ValueError`` when the first and the last are not.
    """
    if len(points) < 2:
        return None
    drawn = _thinned(points)
    first_at, last_at = drawn[0][0], drawn[-1][0]
    span = last_at - first_at
    if span <= timedelta(0):
        raise ValueError("a chart's instants rise")
    amounts = [value for _, value in points]
    lowest, highest = min(amounts), max(amounts)
    low, high = float(lowest), float(highest)
    width = _RIGHT - _LEFT
    height = _BOTTOM - _TOP

    def x_of(moment: datetime) -> float:
        return _LEFT + width * ((moment - first_at) / span)

    def y_of(value: Decimal) -> float:
        if high == low:
            return _TOP + height / 2
        return _BOTTOM - height * ((float(value) - low) / (high - low))

    labels = (
        Label(_label(highest), _LEFT - _LABEL_GAP, _TOP + 4, "end", "y"),
        Label(_label(lowest), _LEFT - _LABEL_GAP, _BOTTOM, "end", "y"),
        Label(show_when(first_at), _LEFT, _X_LABEL_Y, "start", "x"),
        Label(show_when(last_at), _RIGHT, _X_LABEL_Y, "end", "x"),
    )
    return Chart(
        title=title,
        points=" ".join(f"{x_of(at):.1f},{y_of(value):.1f}" for at, value in drawn),
        axes=f"{_LEFT},{_TOP} {_LEFT},{_BOTTOM} {_RIGHT},{_BOTTOM}",
        labels=labels,
        first=_label(drawn[0][1]),
        last=_label(drawn[-1][1]),
        count=len(points),
        span=span_text(span),
    )
