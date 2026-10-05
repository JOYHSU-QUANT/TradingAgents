"""The numbers that decide which swaps are asked for, how a virtual fill is taken, and how a fork signs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .types import Bar

__all__ = ["ExecutionSettings", "ForkSettings", "fill_block"]


def _is_amount(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and not value.is_signed()


@dataclass(frozen=True)
class ExecutionSettings:
    """What a rebalance trades, what it tolerates, and what a modelled or quoted fill assumes.

    - ``min_trade_value``: a transfer between two tokens worth less than
      this, in the quote token, is left out of a rebalance.
    - ``max_slippage``: how far below the bar's close price, after the
      pools' fees, a swap's output may fall. It sets every swap's
      ``min_amount_out``; ``Decimal("0.005")`` is 0.5%.
    - ``delay_blocks``: how many blocks after the first block of a bar's
      boundary a modelled or quoted fill is taken at.
    - ``model_slippage``: what the fill model takes off the close price on
      top of the pools' fees. It is at most ``max_slippage``, or the model
      would refuse every swap.
    - ``model_gas_units_per_hop``: the gas the fill model charges per pool
      crossed, priced at the bar's base fee.
    - ``quote_gas_overhead_units``: the gas a quoted fill adds, once per
      swap, to the quoter's estimate. The estimate covers the pools'
      swaps alone; a transaction's base cost, the router's own work and
      the transfer of the input token come on top of it.
    """

    min_trade_value: Decimal = Decimal("10")
    max_slippage: Decimal = Decimal("0.005")
    delay_blocks: int = 25
    model_slippage: Decimal = Decimal("0.0005")
    model_gas_units_per_hop: int = 150_000
    quote_gas_overhead_units: int = 50_000

    def __post_init__(self) -> None:
        if not _is_amount(self.min_trade_value):
            raise ValueError(
                f"min_trade_value must be a finite, non-negative Decimal, "
                f"got {self.min_trade_value!r}"
            )
        for name in ("max_slippage", "model_slippage"):
            value = getattr(self, name)
            if not _is_amount(value) or value >= 1:
                raise ValueError(f"{name} must be a Decimal in [0, 1), got {value!r}")
        if self.model_slippage > self.max_slippage:
            # Every modelled fill would fall below its swap's minimum and be refused.
            raise ValueError(
                f"model_slippage {self.model_slippage} must not be above max_slippage "
                f"{self.max_slippage}: the model would refuse every swap"
            )
        for name, minimum in (
            ("delay_blocks", 0),
            ("model_gas_units_per_hop", 1),
            ("quote_gas_overhead_units", 0),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}, got {value!r}")


def fill_block(bar: Bar, settings: ExecutionSettings) -> int:
    """The block the swaps decided on ``bar`` are taken at.

    ``settings.delay_blocks`` after the first block of the bar's boundary,
    which is the block after its close block. A modelled or quoted fill is
    priced at the end of it, and a fork run signs its swaps on a fork of
    it, so that all three meet the same pools.
    """
    return bar.close_block + 1 + settings.delay_blocks


# How many dev accounts anvil makes from its mnemonic.
_DEV_ACCOUNTS = 10


@dataclass(frozen=True)
class ForkSettings:
    """How a fork run signs: as which of anvil's dev accounts, and with how long a deadline.

    - ``account``: the dev account, 0 to 9, the swaps are signed by.
    - ``deadline_seconds``: how long after the node's clock a sent swap may
      still be mined.
    """

    account: int = 0
    deadline_seconds: int = 300

    def __post_init__(self) -> None:
        account, deadline = self.account, self.deadline_seconds
        if isinstance(account, bool) or not isinstance(account, int) or not (
            0 <= account < _DEV_ACCOUNTS
        ):
            raise ValueError(
                f"account must be an integer from 0 to {_DEV_ACCOUNTS - 1}, got {account!r}"
            )
        if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < 1:
            raise ValueError(
                f"deadline_seconds must be an integer of at least 1, got {deadline!r}"
            )
