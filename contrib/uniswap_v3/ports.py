"""Ports (interfaces) between the engine and whatever a run mode wires behind it.

The engine step is the same in every :class:`~.domain.types.RunMode`; what
differs is the adapter behind each of these ``Protocol`` classes. A backtest
replays stored bars under a scripted clock and fills from a model, a paper
run reads the chain and fills from quotes, and a fork or live run signs.
:class:`Strategy` is the only way a strategy enters the package.

Structural typing: an implementation does not subclass these, it only needs
matching method signatures. Times are epoch seconds (UTC), as block
timestamps are.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .domain.types import (
    Bar,
    Fill,
    Hold,
    MarketView,
    Portfolio,
    Rejection,
    SwapIntent,
    TargetWeights,
)

__all__ = [
    "BarSource",
    "BlockLocator",
    "Clock",
    "Executor",
    "GasOracle",
    "Strategy",
]


@runtime_checkable
class Strategy(Protocol):
    """Decides what the portfolio's weights should be at the bar being decided."""

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The target weights, or :class:`~.domain.types.Hold` to leave the portfolio alone.

        ``view`` ends at the bar being decided and ``portfolio`` is valued at
        that bar's prices. A strategy that cannot decide raises; it does not
        guess.
        """
        ...


@runtime_checkable
class BarSource(Protocol):
    """Gives the bar at a boundary."""

    def bar_at(self, time: int) -> Bar | None:
        """The bar whose boundary is ``time``, or ``None`` when there is none yet."""
        ...


@runtime_checkable
class Executor(Protocol):
    """Turns one swap into a fill, or says why not."""

    def execute(self, swap: SwapIntent, at_block: int) -> Fill | Rejection:
        """Fill ``swap`` at ``at_block``, or refuse it with a reason."""
        ...


@runtime_checkable
class BlockLocator(Protocol):
    """Maps a time to a block."""

    def first_block_at_or_after(self, time: int) -> int:
        """The number of the first block whose timestamp is at or after ``time``.

        An implementation that cannot tell raises; it does not estimate.
        """
        ...


@runtime_checkable
class GasOracle(Protocol):
    """Reads what gas cost at a block."""

    def base_fee_wei(self, block: int) -> int:
        """The base fee per gas of ``block``, in wei.

        An implementation that cannot read it raises; it does not estimate.
        """
        ...


@runtime_checkable
class Clock(Protocol):
    """The engine's notion of now; a backtest advances a scripted one bar by bar."""

    def now(self) -> int:
        """The current time."""
        ...
