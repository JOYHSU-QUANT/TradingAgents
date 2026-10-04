"""The numbers that decide which swaps are asked for and how a model fills them."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["ExecutionSettings"]


def _is_amount(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and not value.is_signed()


@dataclass(frozen=True)
class ExecutionSettings:
    """What a rebalance trades, what it tolerates, and what the fill model assumes.

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
    """

    min_trade_value: Decimal = Decimal("10")
    max_slippage: Decimal = Decimal("0.005")
    delay_blocks: int = 25
    model_slippage: Decimal = Decimal("0.0005")
    model_gas_units_per_hop: int = 150_000

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
        for name, minimum in (("delay_blocks", 0), ("model_gas_units_per_hop", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}, got {value!r}")
