"""The rules every experiment is calibrated against — strategies whose scores are known in advance.

Plan §6.6: before an optimiser is pointed at a scorer, the scorer has to be
seen giving bad strategies bad scores. Three of the four calibration rules can
be written in this package's own language, and they are written here once, as
documents, so ``calibrate`` measures exactly what the tests measure:

- **buy_and_hold** — ``close > 0``, no exit, all of equity as margin. A price
  is never at or below zero, so the entry holds at every bar; the window
  flattens at its end. Expect exposure ``(bars − 1) / bars`` (the first bar of
  every window is flat, decided 2026-09-14) and a gross return close to the
  window's price move from its second bar's open.
- **always_flat** — ``close > 1e9``, a threshold no BTC close has reached.
  Every figure is 0, and none is NaN.
- **high_turnover_noise** — ``close > 0`` with ``max_bars: 1``. In at an open,
  out at the next, flat for a bar (an exit wins over a same-side entry), in
  again: about half the bars exposed and a round trip every two bars. Its
  gross is whatever the market did on the bars it held; its net is that less
  the cost of every round trip, so net must sit well below gross.

The fourth, **seeded random entries**, cannot be written in this language —
nothing in the vocabulary is random, on purpose — so it is measured in
``tests/test_calibration.py`` on a synthetic driftless market, where any rule
that ignores the future has an expected gross return of zero.

None of these is ever recorded as a trial. A baseline is not a hypothesis,
and filing one would raise the promote threshold for every real rule measured
after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .dsl import StrategySpec, parse_spec
from .metrics import SegmentMetrics

__all__ = ["BASELINES", "baseline_specs", "describe_calibration"]

_ALL_OF_EQUITY: Final = {"mode": "fixed_margin_fraction", "fraction": 1.0}
_ALWAYS: Final = [{"left": "close", "op": ">", "right": 0}]

BASELINES: Final[dict[str, dict[str, object]]] = {
    "buy_and_hold": {
        "family": "breakout",
        "entry": {"long": _ALWAYS},
        "sizing": _ALL_OF_EQUITY,
    },
    "always_flat": {
        "family": "breakout",
        "entry": {"long": [{"left": "close", "op": ">", "right": 1e9}]},
        "sizing": _ALL_OF_EQUITY,
    },
    "high_turnover_noise": {
        "family": "breakout",
        "entry": {"long": _ALWAYS},
        "exit": {"max_bars": 1},
        "sizing": _ALL_OF_EQUITY,
    },
}


def baseline_specs() -> tuple[tuple[str, StrategySpec], ...]:
    """Every baseline, parsed — through the parser, so a baseline is held to the same language."""
    return tuple((name, parse_spec(document)) for name, document in BASELINES.items())


def describe_calibration(rows: Sequence[tuple[str, Sequence[SegmentMetrics]]]) -> list[str]:
    """One line per baseline per window: the figures a calibration is read by.

    Gross and net side by side, because the gap between them is what the
    noise baseline exists to show; exposure and trades, because they are what
    the buy-and-hold and flat baselines are checked on.
    """
    lines = []
    for name, segments in rows:
        for metrics in segments:
            lines.append(
                f"{name} {metrics.segment.name.value}: gross {metrics.gross.total_return:+.2%}, "
                f"net {metrics.net.total_return:+.2%} (sharpe {metrics.net.sharpe:.2f}), "
                f"exposure {metrics.exposure:.1%}, {metrics.trades} trades, "
                f"turnover {metrics.turnover:.1f}x"
                + (" — RUINED" if metrics.ruined else "")
            )
    return lines
