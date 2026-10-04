"""What a run writes down: the run itself, and one decision per bar with its fills and valuation.

A decision is keyed by its run and its bar's boundary, which is what keeps a
bar from being decided twice. It also keeps what the bar looked like when it
was decided (its close block, that block's hash, its finality, whether it
was suspect), because a stored bar's flags and finality are worked out when
it is read and can differ later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from .bars import Finality
from .ledger import Ledger
from .types import Fill, RunMode, TargetWeights

__all__ = [
    "BarSeen",
    "Decision",
    "FillRecord",
    "Outcome",
    "RunRecord",
    "StepRecord",
    "Valuation",
]


class Outcome(str, Enum):
    """How the engine's step on one bar ended."""

    # The bar was suspect: the strategy was not asked and nothing was traded.
    SKIPPED_SUSPECT = "skipped_suspect"
    # The strategy answered ``Hold``.
    HOLD = "hold"
    # The strategy gave a target, and no swap toward it was worth making.
    NO_TRADE = "no_trade"
    # Every swap toward the target filled and was applied.
    FILLED = "filled"
    # A swap was refused, or the ledger could not take the fills: nothing was applied.
    REJECTED = "rejected"


def _is_time(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class BarSeen:
    """What the store said of a bar's close block at the moment it was decided."""

    close_block_hash: str
    finality: Finality

    def __post_init__(self) -> None:
        if not isinstance(self.close_block_hash, str) or not self.close_block_hash:
            raise ValueError(f"close_block_hash must be a string, got {self.close_block_hash!r}")
        if not isinstance(self.finality, Finality):
            raise ValueError(f"finality must be a Finality, got {self.finality!r}")


@dataclass(frozen=True)
class Decision:
    """One bar's decision. ``seen`` is ``None`` for a bar that came from no store."""

    time: int
    outcome: Outcome
    close_block: int
    target: TargetWeights | None = None
    reason: str | None = None
    seen: BarSeen | None = None

    def __post_init__(self) -> None:
        if not _is_time(self.time) or not _is_time(self.close_block):
            raise ValueError("time and close_block must be non-negative integers")
        if not isinstance(self.outcome, Outcome):
            raise ValueError(f"outcome must be an Outcome, got {self.outcome!r}")
        targeted = self.outcome in (Outcome.NO_TRADE, Outcome.FILLED, Outcome.REJECTED)
        if targeted != isinstance(self.target, TargetWeights):
            raise ValueError(
                f"a decision carries a target exactly when the strategy gave one: "
                f"{self.outcome.value} with target {self.target!r}"
            )
        if (self.outcome is Outcome.REJECTED) != (self.reason is not None):
            raise ValueError(f"a rejected decision, and no other, carries a reason: {self!r}")
        if self.seen is not None and not isinstance(self.seen, BarSeen):
            raise ValueError(f"seen must be a BarSeen, got {self.seen!r}")

    @property
    def suspect(self) -> bool:
        """Whether the bar was suspect when decided: a suspect bar is skipped, and no other is."""
        return self.outcome is Outcome.SKIPPED_SUSPECT


@dataclass(frozen=True)
class Valuation:
    """The ledger after a bar's step, and its traded balances valued at the bar's prices.

    ``total_value`` is in the quote token and leaves the gas balance out.
    """

    time: int
    ledger: Ledger
    prices: Mapping[str, Decimal]
    total_value: Decimal

    def __post_init__(self) -> None:
        if not _is_time(self.time):
            raise ValueError(f"time must be a non-negative integer, got {self.time!r}")
        if not isinstance(self.ledger, Ledger):
            raise ValueError(f"ledger must be a Ledger, got {self.ledger!r}")
        if not isinstance(self.total_value, Decimal) or not self.total_value.is_finite():
            raise ValueError(f"total_value must be a finite Decimal, got {self.total_value!r}")
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))


@dataclass(frozen=True)
class StepRecord:
    """Everything one step writes, to be stored together or not at all."""

    decision: Decision
    valuation: Valuation
    fills: tuple[Fill, ...] = ()

    def __post_init__(self) -> None:
        if self.valuation.time != self.decision.time:
            raise ValueError(
                f"the valuation at {self.valuation.time} is not of the decision at "
                f"{self.decision.time}"
            )
        if not isinstance(self.fills, tuple) or bool(self.fills) != (
            self.decision.outcome is Outcome.FILLED
        ):
            raise ValueError("a step carries fills exactly when its decision is filled")


@dataclass(frozen=True)
class FillRecord:
    """A stored fill, read back: symbols and pool addresses in place of the swap's values."""

    time: int
    leg: int
    token_in: str
    token_out: str
    route: tuple[str, ...]
    amount_in: Decimal
    min_amount_out: Decimal
    amount_out: Decimal
    gas_cost_eth: Decimal
    block: int


@dataclass(frozen=True)
class RunRecord:
    """One run: its mode, the config it was started under, and what it started with.

    ``config`` is the config's snapshot (:func:`~..config.config_snapshot`)
    and ``ledger`` the opening balances.
    """

    run_id: str
    mode: RunMode
    chain_id: int
    quote: str
    strategy: str
    config: str
    ledger: Ledger
    created_at: int

    def __post_init__(self) -> None:
        for name in ("run_id", "quote", "strategy", "config"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.mode, RunMode):
            raise ValueError(f"mode must be a RunMode, got {self.mode!r}")
        if not _is_time(self.chain_id) or not _is_time(self.created_at):
            raise ValueError("chain_id and created_at must be non-negative integers")
        if not isinstance(self.ledger, Ledger):
            raise ValueError(f"ledger must be a Ledger, got {self.ledger!r}")
        if self.quote not in self.ledger.balances:
            raise ValueError(
                f"the opening balances {sorted(self.ledger.balances)} must include the "
                f"quote token {self.quote!r}"
            )
