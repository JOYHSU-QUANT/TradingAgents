"""Executors whose fills are virtual: modelled from the bar, or quoted by the pools.

Both date a fill at the same block, :func:`fill_block`, so a run filled by
one can be set beside a run filled by the other.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain.decimal_context import DECIMAL_CONTEXT, EXACT_CONTEXT, floor_to_places, plain
from ..domain.execution import ExecutionSettings
from ..domain.records import FillSource
from ..domain.routing import amount_out_at
from ..domain.types import ETH_DECIMALS, Bar, Fill, Rejection, SwapIntent
from ..ports import GasOracle, NoQuote, Quoter

__all__ = ["ModelExecutor", "QuoteExecutor", "fill_block"]


def fill_block(bar: Bar, settings: ExecutionSettings) -> int:
    """The block a virtual fill decided on ``bar`` is taken at.

    ``settings.delay_blocks`` after the first block of the bar's boundary,
    which is the block after its close block.
    """
    return bar.close_block + 1 + settings.delay_blocks


def _gas_cost_eth(gas_wei: int) -> Decimal:
    """An integer of wei as ETH, under the context that traps a result it would have to round."""
    return Decimal(gas_wei).scaleb(-ETH_DECIMALS, context=EXACT_CONTEXT)


class ModelExecutor:
    """An :class:`~..ports.Executor` that fills from the bar it is handed, and reads nothing else.

    The output is what the route returns at the bar's close prices after the
    pools' fees, less ``settings.model_slippage``, cut to the output token's
    decimal places. Gas is ``settings.model_gas_units_per_hop`` per pool
    crossed, at the bar's base fee, with no priority fee. The fill is dated
    :func:`fill_block`.
    """

    source = FillSource.MODEL

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
            gas_cost_eth=_gas_cost_eth(gas_wei),
            block=fill_block(bar, self._settings),
        )


class QuoteExecutor:
    """An :class:`~..ports.Executor` that fills with what the pools would have returned.

    The swap is quoted at the end of :func:`fill_block`, a block fixed by
    the bar and not by when the quote is asked for: a paper run and a
    backtest that decide the same bar ask about the same block. The output
    is the quote's. Gas is the quoter's estimate plus
    ``settings.quote_gas_overhead_units``, at that block's base fee, with
    no priority fee.

    The swap is refused when the quote is below its minimum or of
    nothing, and when the pools give no answer for it
    (:class:`~..ports.NoQuote`). Any other failure of the read is raised
    as it is: it says nothing about the swap, and the bar is left
    undecided.
    """

    source = FillSource.QUOTER

    def __init__(self, quoter: Quoter, gas: GasOracle, settings: ExecutionSettings) -> None:
        self._quoter = quoter
        self._gas = gas
        self._settings = settings

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        """Fill ``swap`` with its quote at ``bar``'s fill block, or refuse it."""
        block = fill_block(bar, self._settings)
        token_out = swap.token_out
        try:
            amount_out, gas_units = self._quoter.quote(
                swap.token_in, swap.route, swap.amount_in, block=block
            )
        except NoQuote as exc:
            return Rejection(swap, f"the quote at block {block} has no answer ({exc})")
        if amount_out == 0 or amount_out < swap.min_amount_out:
            return Rejection(
                swap,
                f"the quote of {plain(amount_out)} {token_out.symbol} at block {block} is "
                f"below the swap's minimum of {plain(swap.min_amount_out)}",
            )
        gas_wei = (gas_units + self._settings.quote_gas_overhead_units) * self._gas.base_fee_wei(
            block
        )
        return Fill(
            swap=swap, amount_out=amount_out, gas_cost_eth=_gas_cost_eth(gas_wei), block=block
        )
