"""The two virtual executors: fills worked out from the bar alone, and fills quoted by the pools."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.chain.errors import BlockNotFound, MalformedResponse, RpcUnavailable
from contrib.uniswap_v3.domain.execution import ExecutionSettings
from contrib.uniswap_v3.domain.records import FillSource
from contrib.uniswap_v3.domain.types import Bar, Fill, Rejection, SwapIntent
from contrib.uniswap_v3.engine.executors import ModelExecutor, QuoteExecutor, fill_block
from contrib.uniswap_v3.ports import Executor, GasOracle, NoQuote, Quoter
from contrib.uniswap_v3.tests.fakes.engine import (
    GWEI,
    USDC,
    USDC_WETH,
    WBTC_WETH,
    WETH,
    FixedGas,
    ScriptedQuoter,
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


# --- the quote executor ----------------------------------------------------

_ONE_HOP = SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49"))


def _quoting(answer, settings: ExecutionSettings = _SETTINGS):
    """A quote executor over a quoter that gives ``answer``, at a base fee of 10 gwei."""
    quoter = ScriptedQuoter(lambda *asked: answer)
    gas = FixedGas(10 * GWEI)
    return QuoteExecutor(quoter, gas, settings), quoter, gas


def test_the_quote_executor_is_an_executor_over_a_quoter_and_a_gas_oracle():
    executor, quoter, gas = _quoting((D("1.495"), 90_000))
    assert isinstance(executor, Executor)
    assert isinstance(quoter, Quoter) and isinstance(gas, GasOracle)
    assert (executor.source, _executor().source) == (FillSource.QUOTER, FillSource.MODEL)


def test_a_quoted_fill_is_the_quote_at_the_bars_fill_block_with_the_overhead_on_its_gas():
    executor, quoter, gas = _quoting((D("1.495"), 90_000))
    fill = executor.execute(_ONE_HOP, bar())
    # The 90,000 gas of the quote and 50,000 on top, at the fill block's 10 gwei.
    assert fill == Fill(_ONE_HOP, D("1.495"), D("0.0014"), 1_026)
    assert fill_block(bar(), _SETTINGS) == 1_026
    assert quoter.asked == [(USDC, (USDC_WETH,), D("3000"), 1_026)]
    assert gas.blocks == [1_026]


def test_a_quoted_fill_is_dated_as_the_model_dates_one():
    for settings in (_SETTINGS, ExecutionSettings(delay_blocks=0)):
        quoted = _quoting((D("1.495"), 90_000), settings)[0].execute(_ONE_HOP, bar(3))
        modelled = _executor(settings).execute(_ONE_HOP, bar(3))
        assert quoted.block == modelled.block == fill_block(bar(3), settings)


def test_a_two_hop_swap_is_one_quote_and_adds_the_overhead_once():
    swap = SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("4000"), D("0.09"))
    settings = ExecutionSettings(quote_gas_overhead_units=20_000)
    executor, quoter, _ = _quoting((D("0.0995"), 180_000), settings)
    fill = executor.execute(swap, bar())
    assert (fill.amount_out, fill.gas_cost_eth) == (D("0.0995"), D("0.002"))
    assert quoter.asked == [(USDC, (USDC_WETH, WBTC_WETH), D("4000"), 1_026)]


def test_a_quote_below_the_swaps_minimum_is_refused():
    executor, _, gas = _quoting((D("1.48999"), 90_000))
    answer = executor.execute(_ONE_HOP, bar())
    assert isinstance(answer, Rejection) and answer.swap == _ONE_HOP
    assert "at block 1026 is below the swap's minimum of 1.49" in answer.reason
    # Nothing is priced for a swap that is refused.
    assert gas.blocks == []


def test_a_quote_at_the_swaps_minimum_fills():
    assert _quoting((D("1.49"), 90_000))[0].execute(_ONE_HOP, bar()).amount_out == D("1.49")


def test_a_quote_the_pools_have_no_answer_to_refuses_the_swap():
    error = NoQuote("the pool ran out")
    answer = _quoting(error)[0].execute(_ONE_HOP, bar())
    assert isinstance(answer, Rejection)
    assert answer.reason == f"the quote at block 1026 has no answer ({error})"


@pytest.mark.parametrize(
    "error", [RpcUnavailable("down"), BlockNotFound("not yet"), MalformedResponse("garbled")]
)
def test_a_read_that_failed_is_raised_and_refuses_nothing(error):
    with pytest.raises(type(error)):
        _quoting(error)[0].execute(_ONE_HOP, bar())


def test_a_quote_of_nothing_is_refused_even_by_a_swap_with_no_minimum():
    swap = SwapIntent(USDC, (USDC_WETH,), D("3000"), D(0))
    answer = _quoting((D(0), 90_000))[0].execute(swap, bar())
    assert isinstance(answer, Rejection)
    assert answer.reason == "the quote at block 1026 is of no WETH at all"
