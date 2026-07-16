"""Typed views of the mutable ``current_*`` state (pure, all-Decimal).

The paper accounting layer reads and writes two materialized tables — the
per-symbol :class:`PositionState` (``current_positions``) and the account-level
:class:`AccountLedger` (``current_account_state``). These dataclasses are the
in-memory shape the accounting math works with; the repository (de)serializes
them to/from SQLite (Decimals stored as TEXT). Derived values that depend on the
*current mark price* (equity, margin, effective leverage) are recomputed at
snapshot time from these ledgers plus a mark, never stored raw here.

:class:`Side` is the persistence layer's fill/order direction vocabulary — the
storage form of the ``fills.side`` / ``orders.side`` columns. It mirrors the
codebase's enum convention (``TargetSide``, ``DecisionMode``, ...) so a typo like
``"Buy"`` is rejected at the write boundary, not deep inside the fill math.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

# Re-exported so the persistence layer's writers/consumers can pin the same
# money-math context without each reaching into the domain layer; the canonical
# definition (and rationale) lives with the base money math in margin.py.
from ..domains.perp.margin import DECIMAL_CONTEXT

__all__ = ["AccountLedger", "DECIMAL_CONTEXT", "PositionState", "Side"]


class Side(str, Enum):
    """A fill/order direction as stored in SQLite (``"buy"`` / ``"sell"``)."""

    BUY = "buy"
    SELL = "sell"

    @classmethod
    def parse(cls, value: str | Side) -> Side:
        """Coerce a raw string (or pass an enum through), failing loud on a typo."""
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"fill side must be 'buy' or 'sell', got {value!r}") from None


@dataclass(frozen=True)
class PositionState:
    """A symbol's materialized position: signed size, average entry, realized PnL.

    ``size`` is signed (positive long, negative short); ``0`` means flat, and a
    flat position carries no ``entry_price`` (``None``). ``realized_pnl`` is the
    symbol's cumulative fill-realized PnL and is retained across a flat interval
    (a closed position keeps its realized total for the next open), which is why a
    flat state is a ``size = 0`` row here rather than an absent one.
    """

    coin: str
    size: Decimal
    entry_price: Decimal | None
    realized_pnl: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not self.coin or not self.coin.strip():
            raise ValueError("PositionState.coin must be a non-empty string")
        if self.size == 0:
            if self.entry_price is not None:
                raise ValueError("a flat PositionState (size 0) carries no entry_price")
        else:
            if self.entry_price is None:
                raise ValueError("a non-flat PositionState must carry an entry_price")
            if self.entry_price <= 0:
                raise ValueError(f"PositionState.entry_price must be > 0, got {self.entry_price}")

    @property
    def is_flat(self) -> bool:
        return self.size == 0

    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def is_short(self) -> bool:
        return self.size < 0

    @classmethod
    def flat(cls, coin: str, realized_pnl: Decimal = Decimal(0)) -> PositionState:
        return cls(coin=coin, size=Decimal(0), entry_price=None, realized_pnl=realized_pnl)


@dataclass(frozen=True)
class AccountLedger:
    """The account-level accumulators — the paper ledger's source of truth.

    ``wallet_balance`` already folds in realized PnL, fees and funding (execution
    §6.1/§6.5: realized PnL, fees and funding are posted to ``wallet_balance``),
    so ``account_equity = wallet_balance + total_unrealized_pnl`` and nothing is
    double-counted. ``realized_pnl`` / ``total_fees`` / ``net_funding_pnl`` are
    kept alongside as reportable running totals (audit + replay reconciliation).
    """

    wallet_balance: Decimal
    realized_pnl: Decimal = Decimal(0)
    total_fees: Decimal = Decimal(0)
    net_funding_pnl: Decimal = Decimal(0)

    # ``total_fees`` is deliberately NOT constrained ``>= 0``. It is a cumulative
    # accumulator whose sign is a property of the FEE MODEL, not a money invariant:
    # a live maker rebate is a NEGATIVE exchange fee (phase3-spec §15 — the exchange
    # is the truth source), so a run whose rebates outweigh its costs legitimately
    # carries a negative total. Rejecting it here would crash the live fill ingester
    # on a fill the exchange really executed, roll the transaction back, and then
    # crash again on every REST-backfill retry — wedging ingestion over a value that
    # is simply true.
    #
    # The paper model's non-negativity is unaffected, and is still enforced where it
    # belongs — at the paper fill boundary: ``compute_fill_effect`` rejects a negative
    # ``fee_rate`` and ``FillEffect`` rejects a negative ``fee``, so a paper run can
    # never accumulate one. (Its live counterpart ``LiveFillEffect`` documents the
    # rebate as legitimate — this guard used to contradict it.)
