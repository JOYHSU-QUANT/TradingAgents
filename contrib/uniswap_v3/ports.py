"""Ports (interfaces) between the engine and whatever a run mode wires behind it.

The engine step is the same in every :class:`~.domain.types.RunMode`; what
differs is the adapter behind each of these ``Protocol`` classes. A backtest
replays stored bars and fills from a model, a paper run reads the chain and
fills from quotes, and a fork or live run signs.
:class:`Strategy` is the only way a strategy enters the package, and
:class:`Journal` is where every mode writes what it decided.

Structural typing: an implementation does not subclass these, it only needs
matching method signatures. Times are epoch seconds (UTC), as block
timestamps are.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .domain.ledger import Ledger
from .domain.records import Decision, FillSource, RunRecord, StepRecord
from .domain.types import (
    Bar,
    Fill,
    Hold,
    MarketView,
    Pool,
    Portfolio,
    Rejection,
    SwapIntent,
    TargetWeights,
    Token,
)

__all__ = [
    "BarSource",
    "BlockLocator",
    "Clock",
    "Executor",
    "GasOracle",
    "Journal",
    "NoQuote",
    "Quoter",
    "Strategy",
]


@runtime_checkable
class Strategy(Protocol):
    """Decides what the portfolio's weights should be at the bar being decided."""

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The target weights, or :class:`~.domain.types.Hold` to leave the portfolio alone.

        ``view`` ends at the bar being decided and ``portfolio`` is valued at
        that bar's prices. The answer must depend on those two arguments and
        nothing else: no clock, no random number, no state kept from an
        earlier call, nothing read from outside. The same bars and the same
        portfolio then give the same answer in a backtest, a paper run and a
        rerun, which the engine relies on and does not check.

        A strategy that cannot decide raises; it does not guess. The raise
        stops the run at that bar, which is left undecided and can be
        decided once the cause is fixed.
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

    @property
    def source(self) -> FillSource:
        """Where this executor's fills come from. A run keeps it, and no other carries the run on."""
        ...

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        """Fill ``swap``, decided on ``bar``, or refuse it with a reason.

        The executor chooses the block: a modelled or quoted fill is taken a
        fixed number of blocks after the bar's boundary, and a signed one
        lands where the chain puts it. The fill says which block it was. A
        swap that would deliver less than its ``min_amount_out`` is refused.

        The engine applies a rebalance's fills only when every swap of it
        filled, and drops the fills it already has when a later swap is
        refused. That is sound only while a fill changes nothing outside
        the ledger.
        """
        ...


@runtime_checkable
class Journal(Protocol):
    """Where a run's decisions are kept, and its balances with them."""

    def insert_run(self, run: RunRecord) -> None:
        """Start ``run``; a run with its id that is already there raises."""
        ...

    def run(self, run_id: str) -> RunRecord | None:
        """The run ``run_id``, when there is one."""
        ...

    def decision(self, run_id: str, time: int) -> Decision | None:
        """The run's decision on the bar at ``time``, when it has made one."""
        ...

    def last_decided(self, run_id: str) -> int | None:
        """The boundary of the latest bar the run has decided, when it has decided any."""
        ...

    def ledger(self, run_id: str) -> Ledger:
        """The run's balances after its latest decision, or its opening ones before any."""
        ...

    def record(self, run_id: str, step: StepRecord) -> None:
        """Write one step's decision, fills and valuation: all of them, or none."""
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


class NoQuote(Exception):
    """The pools give no answer for this swap at this block.

    The quote reverts with a reason of a pool's, or a pool runs dry.
    """


@runtime_checkable
class Quoter(Protocol):
    """Asks the pools what a swap would have returned at a block."""

    def quote(
        self, token_in: Token, route: Sequence[Pool], amount_in: Decimal, *, block: int
    ) -> tuple[Decimal, int]:
        """What selling ``amount_in`` of ``token_in`` through ``route`` returns at the end of ``block``.

        The answer is the output in whole tokens, after the pools' fees,
        and the gas the pools' swaps used, in gas units. A swap the pools
        give no answer for raises :class:`NoQuote`. An implementation that
        cannot read the answer raises what it has; it does not estimate.
        """
        ...


@runtime_checkable
class Clock(Protocol):
    """The notion of now, for a mode that waits on the clock; a backtest has no use for one."""

    def now(self) -> int:
        """The current time."""
        ...
