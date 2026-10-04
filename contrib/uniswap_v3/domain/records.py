"""What a run writes down: the run itself, and one decision per bar with its fills and valuation.

A decision is keyed by its run and its bar's boundary, which is what keeps a
bar from being decided twice. It also keeps what the bar looked like when it
was decided (its close block, that block's hash, its finality, whether it
was suspect), because a stored bar's flags and finality are worked out when
it is read and can differ later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Final

from .bars import Finality
from .ledger import Ledger
from .types import Fill, RunMode, TargetWeights

__all__ = [
    "REASON_CODES",
    "BarSeen",
    "Decision",
    "FillRecord",
    "FillSource",
    "Outcome",
    "RejectionCode",
    "RunRecord",
    "SkipCode",
    "StepRecord",
    "Suspicion",
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
    # A swap was refused, or the gas balance did not cover the fills: nothing was applied.
    REJECTED = "rejected"


class RejectionCode(str, Enum):
    """Why a rebalance was rejected, for code to tell apart; the reason says it in words."""

    # The executor refused a swap: what this bar's market gave.
    EXECUTOR = "executor"
    # The gas balance did not cover the fills' gas. Nothing tops it up.
    GAS = "gas"


class SkipCode(str, Enum):
    """Why a suspect bar was skipped, for code to tell apart; the reason names every cause.

    A bar can be suspect for more than one cause, and the code is the
    gravest of them: a :class:`~.bars.SuspectCause`, under the same value.
    """

    # A reading's close block turned out not to be on the final chain.
    REORGED = "reorged"
    # The pools' readings do not agree on the close block.
    CLOSE_BLOCK_MISMATCH = "close_block_mismatch"
    # A pool's close price is further from its TWAP than the limit allows.
    TWAP_DEVIATION = "twap_deviation"
    # The bar came marked suspect, and nothing said why.
    UNSPECIFIED = "unspecified"


_BLOCK_HASH: Final = re.compile(r"0x[0-9a-f]{64}")
_ADDRESS: Final = re.compile(r"0x[0-9a-fA-F]{40}")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class BarSeen:
    """What the store said of a bar's close block at the moment it was decided."""

    close_block: int
    close_block_hash: str
    finality: Finality

    def __post_init__(self) -> None:
        if not _is_count(self.close_block):
            raise ValueError(
                f"close_block must be a non-negative integer, got {self.close_block!r}"
            )
        if not isinstance(self.close_block_hash, str) or not _BLOCK_HASH.fullmatch(
            self.close_block_hash
        ):
            raise ValueError(
                f"close_block_hash must be 0x followed by 64 lowercase hex digits, "
                f"got {self.close_block_hash!r}"
            )
        if not isinstance(self.finality, Finality):
            raise ValueError(f"finality must be a Finality, got {self.finality!r}")


# The outcomes that say why, and the codes each says it in.
REASON_CODES: Final[Mapping[Outcome, type[RejectionCode] | type[SkipCode]]] = MappingProxyType(
    {Outcome.REJECTED: RejectionCode, Outcome.SKIPPED_SUSPECT: SkipCode}
)


@dataclass(frozen=True)
class Suspicion:
    """Why a bar is suspect, as it was worked out when the bar was read.

    A stored bar's flags and finality can differ later, so a decision that
    skips the bar keeps this.
    """

    code: SkipCode
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, SkipCode):
            raise ValueError(f"code must be a SkipCode, got {self.code!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError(f"reason must be a non-empty string, got {self.reason!r}")


@dataclass(frozen=True)
class Decision:
    """One bar's decision.

    A rejected decision and a skipped one, and no other, say why: ``reason``
    in words and ``reason_code`` for code, a :class:`RejectionCode` for the
    one and a :class:`SkipCode` for the other. ``seen`` is ``None`` for a
    bar that came from no store, and otherwise describes the block
    ``close_block`` names.
    """

    time: int
    outcome: Outcome
    close_block: int
    target: TargetWeights | None = None
    reason: str | None = None
    reason_code: RejectionCode | SkipCode | None = None
    seen: BarSeen | None = None

    def __post_init__(self) -> None:
        if not _is_count(self.time) or not _is_count(self.close_block):
            raise ValueError("time and close_block must be non-negative integers")
        if not isinstance(self.outcome, Outcome):
            raise ValueError(f"outcome must be an Outcome, got {self.outcome!r}")
        targeted = self.outcome in (Outcome.NO_TRADE, Outcome.FILLED, Outcome.REJECTED)
        if targeted != isinstance(self.target, TargetWeights):
            raise ValueError(
                f"a decision carries a target exactly when the strategy gave one: "
                f"{self.outcome.value} with target {self.target!r}"
            )
        code_type = REASON_CODES.get(self.outcome)
        explained = code_type is not None
        if explained != (self.reason is not None) or explained != (self.reason_code is not None):
            raise ValueError(
                f"a rejected or a skipped decision, and no other, carries a reason and a "
                f"reason code: {self.outcome.value} with {self.reason!r} and "
                f"{self.reason_code!r}"
            )
        if code_type is not None and (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or not isinstance(self.reason_code, code_type)
        ):
            raise ValueError(
                f"a reason is a non-empty string and the reason code of a "
                f"{self.outcome.value} decision a {code_type.__name__}, "
                f"got {self.reason!r} and {self.reason_code!r}"
            )
        if self.seen is not None:
            if not isinstance(self.seen, BarSeen):
                raise ValueError(f"seen must be a BarSeen, got {self.seen!r}")
            if self.seen.close_block != self.close_block:
                raise ValueError(
                    f"seen describes block {self.seen.close_block}, and the bar closed on "
                    f"{self.close_block}"
                )

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
        if not _is_count(self.time):
            raise ValueError(f"time must be a non-negative integer, got {self.time!r}")
        if not isinstance(self.ledger, Ledger):
            raise ValueError(f"ledger must be a Ledger, got {self.ledger!r}")
        if not isinstance(self.total_value, Decimal) or not self.total_value.is_finite():
            raise ValueError(f"total_value must be a finite Decimal, got {self.total_value!r}")
        if not isinstance(self.prices, Mapping):
            raise ValueError(f"prices must map token symbol to price, got {self.prices!r}")
        for symbol, price in self.prices.items():
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise ValueError(
                    f"prices[{symbol!r}] must be a finite, positive Decimal, got {price!r}"
                )
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))


@dataclass(frozen=True)
class StepRecord:
    """Everything one step writes, to be stored together or not at all."""

    decision: Decision
    valuation: Valuation
    fills: tuple[Fill, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decision, Decision) or not isinstance(self.valuation, Valuation):
            raise ValueError("a step holds a Decision and a Valuation")
        if self.valuation.time != self.decision.time:
            raise ValueError(
                f"the valuation at {self.valuation.time} is not of the decision at "
                f"{self.decision.time}"
            )
        if not isinstance(self.fills, tuple) or bool(self.fills) != (
            self.decision.outcome is Outcome.FILLED
        ):
            raise ValueError("a step carries fills exactly when its decision is filled")
        for fill in self.fills:
            if not isinstance(fill, Fill):
                raise ValueError(f"a step's fills are Fill values, got {fill!r}")


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

    def __post_init__(self) -> None:
        for name in ("time", "leg", "block"):
            if not _is_count(getattr(self, name)):
                raise ValueError(
                    f"{name} must be a non-negative integer, got {getattr(self, name)!r}"
                )
        for name in ("token_in", "token_out"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a token symbol, got {value!r}")
        if self.token_in == self.token_out:
            raise ValueError(f"a fill swaps two different tokens, got {self.token_in!r} twice")
        if (
            not isinstance(self.route, tuple)
            or not self.route
            or not all(isinstance(pool, str) and _ADDRESS.fullmatch(pool) for pool in self.route)
        ):
            raise ValueError(
                f"route must be a non-empty tuple of pool addresses, got {self.route!r}"
            )
        for name, positive in (
            ("amount_in", True),
            ("amount_out", True),
            ("min_amount_out", False),
            ("gas_cost_eth", False),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value.is_signed()
                or (positive and value == 0)
            ):
                raise ValueError(f"{name} is not an amount a fill can have: {value!r}")
        if self.amount_out < self.min_amount_out:
            raise ValueError(
                f"amount_out {self.amount_out} is below min_amount_out {self.min_amount_out}"
            )


class FillSource(str, Enum):
    """Where a run's fills come from. A run keeps to one, from its first bar to its last."""

    MODEL = "model"
    QUOTER = "quoter"


@dataclass(frozen=True)
class RunRecord:
    """One run: its mode, the config it was started under, and what it started with.

    ``config`` is the config's snapshot (:func:`~..config.config_snapshot`),
    ``ledger`` the opening balances, and ``fills`` where its fills come
    from. A paper run fills from quotes: a model has nothing to say about
    the chain the run is reading.
    """

    run_id: str
    mode: RunMode
    chain_id: int
    quote: str
    strategy: str
    config: str
    ledger: Ledger
    created_at: int
    fills: FillSource = FillSource.MODEL

    def __post_init__(self) -> None:
        for name in ("run_id", "quote", "strategy", "config"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.mode, RunMode):
            raise ValueError(f"mode must be a RunMode, got {self.mode!r}")
        if not isinstance(self.fills, FillSource):
            raise ValueError(f"fills must be a FillSource, got {self.fills!r}")
        if self.mode is RunMode.PAPER and self.fills is not FillSource.QUOTER:
            raise ValueError(
                f"a paper run fills from quotes, not from the {self.fills.value}"
            )
        if not _is_count(self.chain_id) or not _is_count(self.created_at):
            raise ValueError("chain_id and created_at must be non-negative integers")
        if not isinstance(self.ledger, Ledger):
            raise ValueError(f"ledger must be a Ledger, got {self.ledger!r}")
        if self.quote not in self.ledger.balances:
            raise ValueError(
                f"the opening balances {sorted(self.ledger.balances)} must include the "
                f"quote token {self.quote!r}"
            )
