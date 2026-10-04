"""The run metrics, against a run worked by hand."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.metrics import Curve, MetricsError, run_metrics
from contrib.uniswap_v3.domain.records import FillRecord, Valuation
from contrib.uniswap_v3.tests.fakes.engine import USDC_WETH

D = Decimal
_POOL = USDC_WETH.address
# The pool keeps 0.05% of what is sold into it.
_FEES = {_POOL: D("0.0005")}


def _ledger(usdc: str, weth: str, gas: str = "1") -> Ledger:
    return Ledger(balances={"USDC": D(usdc), "WETH": D(weth)}, gas_eth=D(gas))


def _valuation(time: int, usdc: str, weth: str, price: str, gas: str = "1") -> Valuation:
    ledger = _ledger(usdc, weth, gas)
    prices = {"WETH": D(price)}
    return Valuation(
        time=time,
        ledger=ledger,
        prices=prices,
        total_value=ledger.portfolio("USDC", prices).total_value,
    )


def _fill(time: int, sold: str, amount_in: str, amount_out: str, gas: str) -> FillRecord:
    bought = "WETH" if sold == "USDC" else "USDC"
    return FillRecord(
        time=time,
        leg=0,
        token_in=sold,
        token_out=bought,
        route=(_POOL,),
        amount_in=D(amount_in),
        min_amount_out=D("0"),
        amount_out=D(amount_out),
        gas_cost_eth=D(gas),
        block=time,
    )


# Four bars, WETH at 2000, 2500, 1500 and 2000 USDC.
#
# Bar 1 sells 4000 USDC: the pool keeps 2, so 3998 is left to buy with, and
# 1.998 WETH comes out, worth 3996: 2 of slippage. Gas is 0.001 ETH, 2 USDC.
# Bar 4 sells 0.998 WETH, worth 1996: the pool keeps 0.998, 1995.002 is left,
# and 1994 USDC comes out: 1.002 of slippage. Gas is 0.002 ETH, 4 USDC.
_VALUATIONS = [
    _valuation(1, "6000", "1.998", "2000", gas="0.999"),  # 9996
    _valuation(2, "6000", "1.998", "2500", gas="0.999"),  # 10995
    _valuation(3, "6000", "1.998", "1500", gas="0.999"),  # 8997
    _valuation(4, "7994", "1", "2000", gas="0.997"),  # 9994
]
_FILLS = [
    _fill(1, "USDC", "4000", "1.998", "0.001"),
    _fill(4, "WETH", "0.998", "1994", "0.002"),
]


def _metrics(**changes):
    arguments = {
        "quote": "USDC",
        "gas_token": "WETH",
        "opening": _ledger("10000", "0"),
        "valuations": _VALUATIONS,
        "fills": _FILLS,
        "fee_rates": _FEES,
    }
    return run_metrics(**{**arguments, **changes})


def test_a_run_worked_by_hand():
    metrics = _metrics()
    assert metrics.bars == 4
    assert (metrics.rebalances, metrics.swaps) == (2, 2)

    # Equity is the valuation less the gas paid so far: 9994, 10993, 8995, 9988.
    assert metrics.strategy.start == D("10000")
    assert metrics.strategy.end == D("9988")
    assert metrics.strategy.total_return == D("-0.0012")
    # The fall from 10993 to 8995.
    assert metrics.strategy.max_drawdown == D("1998") / D("10993")

    assert metrics.costs.pool_fees == D("2.998")
    assert metrics.costs.slippage == D("3.002")
    assert metrics.costs.gas == D("6")
    assert metrics.costs.gas_eth == D("0.003")
    # The run ends at the price it started at, so the costs are all it lost.
    assert metrics.costs.total == D("12") == metrics.strategy.start - metrics.strategy.end

    assert metrics.traded_value == D("5996")
    # Over the mean of the four equities, 9992.5.
    assert metrics.turnover == D("5996") / D("9992.5")


def test_a_swap_through_two_pools_pays_both_fees_and_a_bar_adds_up_its_swaps_gas():
    other = "0x" + "12" * 20
    # 4000 USDC through a 0.05% and a 0.3% pool: 2 kept, then 11.994 of the 3998 left.
    two_hop = FillRecord(
        time=1,
        leg=0,
        token_in="USDC",
        token_out="WETH",
        route=(_POOL, other),
        amount_in=D("4000"),
        min_amount_out=D("0"),
        amount_out=D("1.99"),
        gas_cost_eth=D("0.001"),
        block=1,
    )
    second = replace(_fill(1, "USDC", "1000", "0.4995", "0.002"), leg=1)
    metrics = _metrics(
        valuations=[_valuation(1, "5000", "2.4895", "2000", gas="0.997")],
        fills=[two_hop, second],
        fee_rates={_POOL: D("0.0005"), other: D("0.003")},
    )
    assert metrics.costs.pool_fees == D("13.994") + D("0.5")
    # 3986.006 left of the first swap against 3980 out, and 999.5 against 999.
    assert metrics.costs.slippage == D("6.006") + D("0.5")
    # Both swaps' gas, 0.003 ETH at 2000, comes off the one bar's equity.
    assert metrics.costs.gas == D("6")
    assert metrics.strategy.end == D("9979") - D("6")
    assert (metrics.rebalances, metrics.swaps) == (1, 2)


def test_the_gas_balance_is_no_part_of_equity():
    # Ten times the gas float, and the same gas spent: the same curve.
    richer = [
        replace(
            valuation, ledger=replace(valuation.ledger, gas_eth=valuation.ledger.gas_eth + D("9"))
        )
        for valuation in _VALUATIONS
    ]
    assert _metrics(valuations=richer, opening=_ledger("10000", "0", gas="10")) == _metrics()


def test_the_two_comparisons_start_where_the_run_starts():
    metrics = _metrics(
        opening=_ledger("5000", "2.5"),
        valuations=[
            _valuation(1, "5000", "2.5", "2000"),
            _valuation(2, "5000", "2.5", "2500"),
            _valuation(3, "5000", "2.5", "1500"),
        ],
        fills=[],
    )
    # 2.5 WETH and 5000 USDC, left alone: 10000, 11250, 8750.
    assert metrics.opening_held == Curve(
        start=D("10000"), end=D("8750"), max_drawdown=D("2500") / D("11250")
    )
    assert metrics.opening_held.total_return == D("-0.125")
    assert metrics.all_quote == Curve(start=D("10000"), end=D("10000"), max_drawdown=D("0"))
    assert metrics.all_quote.total_return == 0
    # A run that never traded is its opening balances left alone.
    assert metrics.strategy == metrics.opening_held
    assert (metrics.rebalances, metrics.swaps, metrics.traded_value, metrics.turnover) == (
        0,
        0,
        D("0"),
        D("0"),
    )
    assert metrics.costs.total == 0


def test_a_fill_priced_above_the_close_has_negative_slippage():
    # 4000 USDC buys 2 WETH, worth 4000 at the close: 2 more than the fees left.
    metrics = _metrics(
        valuations=[_valuation(1, "6000", "2", "2000")],
        fills=[_fill(1, "USDC", "4000", "2", "0")],
    )
    assert metrics.costs.pool_fees == D("2")
    assert metrics.costs.slippage == D("-2")
    assert metrics.costs.total == 0
    assert metrics.strategy.total_return == 0


def test_gas_paid_in_the_quote_token_is_valued_as_it_is():
    metrics = _metrics(
        quote="WETH",
        opening=Ledger(balances={"WETH": D("10"), "USDC": D("0")}, gas_eth=D("1")),
        valuations=[
            Valuation(
                time=1,
                ledger=Ledger(balances={"WETH": D("10"), "USDC": D("0")}, gas_eth=D("1")),
                prices={"USDC": D("0.0005")},
                total_value=D("10"),
            )
        ],
        fills=[],
    )
    assert metrics.strategy == Curve(start=D("10"), end=D("10"), max_drawdown=D("0"))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        (
            # Ten ETH of gas, 20000 USDC, against a portfolio worth half that.
            {"fills": [_fill(1, "USDC", "4000", "1.998", "10")]},
            "the run's mean equity is not positive",
        ),
        ({"valuations": []}, "no valuation to measure it on"),
        ({"valuations": [_VALUATIONS[1], _VALUATIONS[0]]}, "follows the one at"),
        ({"valuations": [_VALUATIONS[0], _VALUATIONS[0]]}, "follows the one at"),
        ({"valuations": _VALUATIONS[1:]}, "the fill at 1 is of a bar that has no valuation"),
        ({"fee_rates": {}}, "crosses the pool .* whose fee is not known"),
        ({"gas_token": "ETH"}, "the valuation at 1 has no price for ETH"),
        (
            {"opening": Ledger(balances={"USDC": D("1"), "WBTC": D("1")}, gas_eth=D("0"))},
            "the opening balances cannot be valued at 1",
        ),
        (
            {"opening": _ledger("0", "0"), "fills": []},
            "the opening balances are worth nothing at the first bar",
        ),
    ],
)
def test_records_that_cannot_be_measured_are_refused(changes, match):
    with pytest.raises(MetricsError, match=match):
        _metrics(**changes)
