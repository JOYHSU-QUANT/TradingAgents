"""What a run's valuations and fills add up to: return, drawdown, turnover and costs.

Everything is in the run's quote token and is valued at the bars' close
prices, the prices the valuations carry.

- **Equity** at a bar is the value of the traded balances less the gas the
  run has paid up to that bar, each fill's gas valued at its own bar's price
  of the gas token. The gas balance itself is not part of equity: it is a
  float that sits beside the portfolio, and counting it would mix ETH's
  price moves into the result. Gas spent is a cost, and this is where it
  shows.
- The curves start from the opening balances valued at the first bar's
  prices, before that bar's step, so the costs of a rebalance on the first
  bar count against the return.
- A swap's **pool fees** are the share of its input the pools on its route
  keep. Its **slippage** is whatever else its output falls short of the
  input by, both valued at the bar's close: the fill model's slippage, the
  cut to the output token's decimal places, and, for a fill priced later
  than the close, the price's move in between, which can make it negative.
  The two sum to the value the swap lost.
- **Turnover** is the value sold across all swaps over the mean equity.
- Two curves stand beside the run's for comparison: the opening balances
  left untouched, and the opening value held in the quote token.

The valuations handed in are the ones to measure on. A caller leaves out
those of suspect bars, whose prices are not to be trusted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .decimal_context import DECIMAL_CONTEXT
from .ledger import Ledger
from .records import FillRecord, Valuation

__all__ = ["Costs", "Curve", "MetricsError", "RunMetrics", "run_metrics"]

_ZERO = Decimal(0)
_ONE = Decimal(1)


class MetricsError(ValueError):
    """The run's records cannot be measured."""


@dataclass(frozen=True)
class Curve:
    """A value over the run's bars: where it started and ended, and its deepest fall.

    ``max_drawdown`` is the largest fall from an earlier peak, as a fraction
    of that peak: ``Decimal("0.25")`` is a fall of 25%.
    """

    start: Decimal
    end: Decimal
    max_drawdown: Decimal

    @property
    def total_return(self) -> Decimal:
        """The change from start to end, as a fraction of the start."""
        return DECIMAL_CONTEXT.subtract(DECIMAL_CONTEXT.divide(self.end, self.start), _ONE)


@dataclass(frozen=True)
class Costs:
    """What the run's swaps cost, in the quote token; ``gas_eth`` is the gas in ETH."""

    pool_fees: Decimal
    slippage: Decimal
    gas: Decimal
    gas_eth: Decimal

    @property
    def total(self) -> Decimal:
        """Pool fees, slippage and gas together."""
        return DECIMAL_CONTEXT.add(DECIMAL_CONTEXT.add(self.pool_fees, self.slippage), self.gas)


@dataclass(frozen=True)
class RunMetrics:
    """A run measured over ``bars`` valuations."""

    bars: int
    strategy: Curve
    opening_held: Curve
    all_quote: Curve
    rebalances: int
    swaps: int
    traded_value: Decimal
    turnover: Decimal
    costs: Costs


def _curve(values: Sequence[Decimal]) -> Curve:
    peak = values[0]
    deepest = _ZERO
    for value in values:
        peak = max(peak, value)
        fall = DECIMAL_CONTEXT.divide(DECIMAL_CONTEXT.subtract(peak, value), peak)
        deepest = max(deepest, fall)
    return Curve(start=values[0], end=values[-1], max_drawdown=deepest)


def _price(prices: Mapping[str, Decimal], symbol: str, quote: str, time: int) -> Decimal:
    if symbol == quote:
        return _ONE
    if symbol not in prices:
        raise MetricsError(f"the valuation at {time} has no price for {symbol}")
    return prices[symbol]


def _value(ledger: Ledger, quote: str, valuation: Valuation) -> Decimal:
    try:
        return ledger.portfolio(quote, valuation.prices).total_value
    except ValueError as exc:
        raise MetricsError(
            f"the opening balances cannot be valued at {valuation.time} ({exc})"
        ) from exc


def run_metrics(
    *,
    quote: str,
    gas_token: str,
    opening: Ledger,
    valuations: Sequence[Valuation],
    fills: Sequence[FillRecord],
    fee_rates: Mapping[str, Decimal],
) -> RunMetrics:
    """Measure a run from its opening balances, its valuations and its fills.

    ``valuations`` are oldest first and ``fills`` are those of the bars
    they are of. ``gas_token`` is the symbol whose price is the price of
    one ETH of gas, and ``fee_rates`` maps a pool's address to its fee as a
    fraction of the input.
    """
    if not valuations:
        raise MetricsError("the run has no valuation to measure it on")
    for earlier, later in zip(valuations, valuations[1:], strict=False):
        if later.time <= earlier.time:
            raise MetricsError(f"the valuation at {later.time} follows the one at {earlier.time}")
    prices_at = {valuation.time: valuation.prices for valuation in valuations}

    multiply, add, subtract = (
        DECIMAL_CONTEXT.multiply,
        DECIMAL_CONTEXT.add,
        DECIMAL_CONTEXT.subtract,
    )
    pool_fees = slippage = gas = gas_eth = traded = _ZERO
    gas_at: dict[int, Decimal] = {}
    for fill in fills:
        if fill.time not in prices_at:
            raise MetricsError(f"the fill at {fill.time} is of a bar that has no valuation")
        prices = prices_at[fill.time]
        value_in = multiply(fill.amount_in, _price(prices, fill.token_in, quote, fill.time))
        value_out = multiply(fill.amount_out, _price(prices, fill.token_out, quote, fill.time))
        after_fees = value_in
        for pool in fill.route:
            if pool not in fee_rates:
                raise MetricsError(
                    f"the fill at {fill.time} crosses the pool {pool}, whose fee is not known"
                )
            after_fees = multiply(after_fees, subtract(_ONE, fee_rates[pool]))
        fill_gas = multiply(fill.gas_cost_eth, _price(prices, gas_token, quote, fill.time))
        traded = add(traded, value_in)
        pool_fees = add(pool_fees, subtract(value_in, after_fees))
        slippage = add(slippage, subtract(after_fees, value_out))
        gas = add(gas, fill_gas)
        gas_eth = add(gas_eth, fill.gas_cost_eth)
        gas_at[fill.time] = add(gas_at.get(fill.time, _ZERO), fill_gas)

    base = _value(opening, quote, valuations[0])
    if base <= 0:
        raise MetricsError("the opening balances are worth nothing at the first bar")
    equity = [base]
    held = [base]
    gas_so_far = equity_sum = _ZERO
    for valuation in valuations:
        gas_so_far = add(gas_so_far, gas_at.get(valuation.time, _ZERO))
        equity.append(subtract(valuation.total_value, gas_so_far))
        equity_sum = add(equity_sum, equity[-1])
        held.append(_value(opening, quote, valuation))
    mean_equity = DECIMAL_CONTEXT.divide(equity_sum, Decimal(len(valuations)))
    if mean_equity <= 0:
        raise MetricsError("the run's mean equity is not positive, so it has no turnover")

    return RunMetrics(
        bars=len(valuations),
        strategy=_curve(equity),
        opening_held=_curve(held),
        all_quote=Curve(start=base, end=base, max_drawdown=_ZERO),
        rebalances=len(gas_at),
        swaps=len(fills),
        traded_value=traded,
        turnover=DECIMAL_CONTEXT.divide(traded, mean_equity),
        costs=Costs(pool_fees=pool_fees, slippage=slippage, gas=gas, gas_eth=gas_eth),
    )
