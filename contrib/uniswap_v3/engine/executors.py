"""Executors that fill a swap without a chain."""

from __future__ import annotations

from decimal import Decimal

from ..domain.decimal_context import DECIMAL_CONTEXT, EXACT_CONTEXT, floor_to_places, plain
from ..domain.execution import ExecutionSettings
from ..domain.routing import amount_out_at
from ..domain.types import ETH_DECIMALS, Bar, Fill, Rejection, SwapIntent

__all__ = ["ModelExecutor"]


class ModelExecutor:
    """An :class:`~..ports.Executor` that fills from the bar it is handed, and reads nothing else.

    The output is what the route returns at the bar's close prices after the
    pools' fees, less ``settings.model_slippage``, cut to the output token's
    decimal places. Gas is ``settings.model_gas_units_per_hop`` per pool
    crossed, at the bar's base fee, with no priority fee. The fill is dated
    ``settings.delay_blocks`` after the first block of the bar's boundary,
    which is the block after its close block.
    """

    def __init__(self, quote: str, settings: ExecutionSettings) -> None:
        self._quote = quote
        self._settings = settings

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        """Fill ``swap`` at ``bar``'s prices, or refuse it when that falls short of its minimum."""
        token_out = swap.token_out
        if self._quote in bar.prices:
            # A bar prices every token but the quote: this executor was built for another.
            raise ValueError(
                f"the bar at {bar.time} prices {self._quote}, which the executor takes for "
                f"the quote token"
            )
        for token in (swap.token_in, token_out):
            if token.symbol != self._quote and token.symbol not in bar.prices:
                raise ValueError(f"the bar at {bar.time} has no price for {token.symbol}")
        at_close = amount_out_at(
            swap.token_in, swap.route, swap.amount_in, quote=self._quote, prices=bar.prices
        )
        modelled = floor_to_places(
            DECIMAL_CONTEXT.multiply(
                at_close, DECIMAL_CONTEXT.subtract(Decimal(1), self._settings.model_slippage)
            ),
            token_out.decimals,
        )
        if modelled == 0 or modelled < swap.min_amount_out:
            return Rejection(
                swap,
                f"the modelled output of {plain(modelled)} {token_out.symbol} is below the "
                f"swap's minimum of {plain(swap.min_amount_out)}",
            )
        gas_wei = self._settings.model_gas_units_per_hop * len(swap.route) * bar.base_fee_wei
        return Fill(
            swap=swap,
            amount_out=modelled,
            # An integer of wei shifted eighteen places, under the context that
            # traps a result it would have had to round.
            gas_cost_eth=Decimal(gas_wei).scaleb(-ETH_DECIMALS, context=EXACT_CONTEXT),
            block=bar.close_block + 1 + self._settings.delay_blocks,
        )
