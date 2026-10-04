"""The model executor: fills worked out from the bar alone."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.execution import ExecutionSettings
from contrib.uniswap_v3.domain.types import Bar, Fill, Rejection, SwapIntent
from contrib.uniswap_v3.engine.executors import ModelExecutor
from contrib.uniswap_v3.ports import Executor
from contrib.uniswap_v3.tests.fakes.engine import (
    GWEI,
    USDC,
    USDC_WETH,
    WBTC_WETH,
    WETH,
    bar,
)

D = Decimal
_SETTINGS = ExecutionSettings(
    model_slippage=D("0.001"), model_gas_units_per_hop=100_000, delay_blocks=25
)


def _executor(settings: ExecutionSettings = _SETTINGS) -> ModelExecutor:
    return ModelExecutor("USDC", settings)


def test_the_model_executor_is_an_executor():
    assert isinstance(_executor(), Executor)


def test_a_one_hop_fill_is_the_close_price_less_the_fee_and_the_modelled_slippage():
    swap = SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49"))
    fill = _executor().execute(swap, bar(weth="2000"))
    # 1.5 WETH, less 0.05%, less 0.1%.
    assert fill == Fill(swap, D("1.49775075"), D("0.001"), 1_026)


def test_a_two_hop_fill_pays_both_fees_and_twice_the_gas():
    swap = SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("2000"), D("0.0497"))
    fill = _executor().execute(swap, bar(wbtc="40000", base_fee_gwei=10))
    # 0.05 WBTC, less 0.05% twice, less 0.1%, cut to WBTC's eight places.
    assert fill == Fill(swap, D("0.04990006"), D("0.002"), 1_026)


def test_a_fill_into_the_quote_token_is_priced_from_the_token_sold():
    swap = SwapIntent(WETH, (USDC_WETH,), D("2"), D("0"))
    fill = _executor().execute(swap, bar(weth="2500"))
    # 5000 USDC, less 0.05%, less 0.1%.
    assert isinstance(fill, Fill)
    assert fill.amount_out == D("4992.5025")


def test_a_fill_is_dated_the_delay_after_the_first_block_of_the_boundary():
    swap = SwapIntent(USDC, (USDC_WETH,), D("100"), D("0"))
    for delay, block in ((0, 3_001), (25, 3_026)):
        fill = _executor(ExecutionSettings(delay_blocks=delay)).execute(swap, bar(day=2))
        assert isinstance(fill, Fill)
        assert fill.block == block


def test_gas_is_exact_however_odd_the_base_fee():
    swap = SwapIntent(USDC, (USDC_WETH,), D("100"), D("0"))
    odd = Bar(time=1, close_block=1, prices={"WETH": D("2000"), "WBTC": D("1")}, base_fee_wei=7 * GWEI + 1)
    fill = _executor().execute(swap, odd)
    assert isinstance(fill, Fill)
    assert fill.gas_cost_eth == D("0.0007000000001")


def test_a_swap_whose_minimum_the_model_does_not_reach_is_refused():
    # The model takes 0.1% off; the swap tolerates nothing below the close less the fee.
    swap = SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49925"))
    answer = _executor().execute(swap, bar())
    assert answer == Rejection(
        swap, "the modelled output of 1.49775075 WETH is below the swap's minimum of 1.49925"
    )


def test_a_swap_too_small_to_deliver_anything_is_refused():
    swap = SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("0.0001"), D("0"))
    answer = _executor().execute(swap, bar())
    assert isinstance(answer, Rejection)
    assert "the modelled output of 0 WBTC" in answer.reason


def test_a_bar_that_does_not_price_a_token_of_the_swap_raises():
    swap = SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("100"), D("0"))
    unpriced = Bar(time=1, close_block=1, prices={"WETH": D("2000")}, base_fee_wei=GWEI)
    with pytest.raises(ValueError, match="no price for WBTC"):
        _executor().execute(swap, unpriced)
