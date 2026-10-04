"""Routes through the configured pools, and the swaps that close a portfolio's gap to its target."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.execution import ExecutionSettings
from contrib.uniswap_v3.domain.routing import amount_out_at, find_route, plan_swaps
from contrib.uniswap_v3.domain.types import Pool, Portfolio, SwapIntent, Token
from contrib.uniswap_v3.tests.fakes.engine import (
    PRICES,
    USDC,
    USDC_WETH,
    WBTC,
    WBTC_WETH,
    WETH,
    weights,
)

D = Decimal
_POOLS = (USDC_WETH, WBTC_WETH)


def _portfolio(usdc: str, weth: str, wbtc: str, prices=PRICES) -> Portfolio:
    return Portfolio(
        quote="USDC", balances={"USDC": D(usdc), "WETH": D(weth), "WBTC": D(wbtc)}, prices=prices
    )


def _plan(portfolio: Portfolio, target, **settings) -> tuple[SwapIntent, ...]:
    return plan_swaps(
        portfolio,
        target,
        tokens=(USDC, WETH, WBTC),
        pools=_POOLS,
        settings=ExecutionSettings(**settings),
    )


# --- routes ----------------------------------------------------------------


def test_a_route_is_the_pool_between_two_tokens_or_the_two_through_weth():
    assert find_route(_POOLS, USDC, WETH) == (USDC_WETH,)
    assert find_route(_POOLS, WETH, USDC) == (USDC_WETH,)
    assert find_route(_POOLS, USDC, WBTC) == (USDC_WETH, WBTC_WETH)
    assert find_route(_POOLS, WBTC, USDC) == (WBTC_WETH, USDC_WETH)


def test_a_route_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="no pool path joins USDC to WBTC"):
        find_route((USDC_WETH,), USDC, WBTC)
    with pytest.raises(ValueError, match="two different tokens, got USDC twice"):
        find_route(_POOLS, USDC, USDC)


def test_where_two_paths_exist_the_route_is_the_shorter():
    # No such pool is in the tables; a config would refuse the loop it makes.
    direct = Pool("0x" + "1" * 40, WBTC, USDC, 3000)
    assert find_route((*_POOLS, direct), USDC, WBTC) == (direct,)


def test_the_output_at_a_bars_prices_takes_each_pools_fee_once():
    # 3000 USDC at 2000 is 1.5 WETH, less 0.05%.
    assert amount_out_at(USDC, (USDC_WETH,), D("3000"), quote="USDC", prices=PRICES) == D(
        "1.49925"
    )
    # 2000 USDC at 40000 is 0.05 WBTC, less 0.05% twice.
    assert amount_out_at(
        USDC, (USDC_WETH, WBTC_WETH), D("2000"), quote="USDC", prices=PRICES
    ) == D("0.0499500125")
    # Into the quote token: 0.5 WBTC is 20000 USDC before the two fees.
    assert amount_out_at(
        WBTC, (WBTC_WETH, USDC_WETH), D("0.5"), quote="USDC", prices=PRICES
    ) == D("19980.005")


# --- plans -----------------------------------------------------------------


def test_one_token_over_its_target_is_sold_straight_into_each_one_under():
    swaps = _plan(_portfolio("10000", "0", "0"), weights("0.5", "0.3", "0.2"))
    # The larger gap first; each minimum is the output at the close, less 0.5%.
    assert swaps == (
        SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49175375")),
        SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("2000"), D("0.04970026")),
    )


def test_two_tokens_over_their_target_are_each_sold_into_the_one_under():
    # Worth 2000 + 4000 + 4000; the target is all of it in USDC.
    swaps = _plan(_portfolio("2000", "2", "0.1"), weights("1", "0", "0"))
    assert [(swap.token_in, swap.route, swap.amount_in) for swap in swaps] == [
        # A tie in value goes to the symbol that sorts first.
        (WBTC, (WBTC_WETH, USDC_WETH), D("0.1")),
        (WETH, (USDC_WETH,), D("2")),
    ]
    # 4000 USDC less 0.05% twice, then less 0.5%; and less 0.05% once, then 0.5%.
    assert [swap.min_amount_out for swap in swaps] == [D("3976.020995"), D("3978.01")]


def test_a_token_is_sold_or_bought_in_one_rebalance_and_never_both():
    # WETH is over, USDC and WBTC under: nothing passes through a third balance.
    swaps = _plan(_portfolio("1000", "4", "0.025"), weights("0.3", "0.4", "0.3"))
    assert {swap.token_in for swap in swaps} == {WETH}
    assert [(swap.token_out, swap.amount_in) for swap in swaps] == [
        (USDC, D("1")),
        (WBTC, D("1")),
    ]


def test_a_target_of_zero_sells_the_whole_balance_and_leaves_no_dust():
    # A price with no short decimal form: value / price would not give the balance back.
    prices = {"WETH": D("1999.999999999999999997"), "WBTC": D("40000")}
    held = "1.234567890123456789"
    (swap,) = _plan(_portfolio("0", held, "0", prices), weights("1", "0", "0"))
    assert swap.amount_in == D(held)


def test_amounts_fit_their_tokens_decimal_places_and_together_sell_down_to_the_target():
    # A third of 1000 USDC goes to each of the others; USDC has six places.
    swaps = _plan(
        _portfolio("1000", "0", "0"), weights("0.3333333333", "0.3333333334", "0.3333333333")
    )
    # The first is cut; the seller's last takes what is left above the 333.333333 kept.
    assert [swap.amount_in for swap in swaps] == [D("333.333333"), D("333.333334")]


def test_a_transfer_worth_less_than_the_minimum_is_left_out():
    portfolio = _portfolio("5009", "2.4955", "0")
    target = weights("0.5", "0.5", "0")
    # 9 USDC over, 9 USDC of WETH under.
    assert _plan(portfolio, target) == ()
    (swap,) = _plan(portfolio, target, min_trade_value=D("9"))
    assert (swap.token_in, swap.amount_in) == (USDC, D("9"))


def test_a_portfolio_at_its_target_or_worth_nothing_needs_no_swap():
    assert _plan(_portfolio("5000", "1.5", "0.05"), weights("0.5", "0.3", "0.2")) == ()
    assert _plan(_portfolio("0", "0", "0"), weights("0.5", "0.3", "0.2")) == ()


def test_the_tolerated_slippage_sets_every_minimum():
    (swap,) = _plan(
        _portfolio("3000", "0", "0"),
        weights("0", "1", "0"),
        max_slippage=D("0"),
        model_slippage=D("0"),
    )
    assert swap.min_amount_out == D("1.49925")
    (swap,) = _plan(
        _portfolio("3000", "0", "0"), weights("0", "1", "0"), max_slippage=D("0.1")
    )
    assert swap.min_amount_out == D("1.349325")


def test_a_plan_is_the_same_whatever_order_the_tokens_are_given_in():
    portfolio = _portfolio("10000", "0", "0")
    target = weights("0.5", "0.25", "0.25")
    one = plan_swaps(
        portfolio, target, tokens=(USDC, WETH, WBTC), pools=_POOLS, settings=ExecutionSettings()
    )
    other = plan_swaps(
        portfolio, target, tokens=(WBTC, WETH, USDC), pools=_POOLS, settings=ExecutionSettings()
    )
    assert one == other
    # The tie goes to WBTC, which sorts before WETH.
    assert [swap.token_out for swap in one] == [WBTC, WETH]


def test_a_portfolio_or_target_over_other_tokens_is_refused():
    dai = Token("DAI", "0x" + "6" * 40, 18)
    with pytest.raises(ValueError, match="both must be the tokens"):
        plan_swaps(
            _portfolio("1", "0", "0"),
            weights("1", "0", "0"),
            tokens=(USDC, WETH, dai),
            pools=_POOLS,
            settings=ExecutionSettings(),
        )


# --- settings --------------------------------------------------------------


def test_the_default_settings_are_the_ones_the_example_config_spells_out():
    settings = ExecutionSettings()
    assert settings.min_trade_value == D("10")
    assert settings.max_slippage == D("0.005")
    assert settings.delay_blocks == 25
    assert settings.model_slippage == D("0.0005")
    assert settings.model_gas_units_per_hop == 150_000
    # The model may take off exactly what a swap tolerates.
    assert ExecutionSettings(max_slippage=D("0.001"), model_slippage=D("0.001")).delay_blocks == 25


@pytest.mark.parametrize(
    ("settings", "match"),
    [
        ({"min_trade_value": D("-1")}, "min_trade_value"),
        ({"min_trade_value": 10}, "min_trade_value"),
        ({"min_trade_value": D("NaN")}, "min_trade_value"),
        ({"max_slippage": D("1")}, r"max_slippage must be a Decimal in \[0, 1\)"),
        ({"max_slippage": D("-0.1")}, "max_slippage"),
        ({"model_slippage": D("1.5")}, "model_slippage"),
        ({"model_slippage": 0.5}, "model_slippage"),
        ({"delay_blocks": -1}, "delay_blocks must be an integer of at least 0"),
        ({"delay_blocks": True}, "delay_blocks"),
        ({"model_gas_units_per_hop": 0}, "model_gas_units_per_hop must be an integer of at least 1"),
        ({"model_gas_units_per_hop": 1.5}, "model_gas_units_per_hop"),
        (
            {"max_slippage": D("0.005"), "model_slippage": D("0.0051")},
            "model_slippage 0.0051 must not be above max_slippage 0.005",
        ),
        # The default model slippage is above a tolerance of nothing.
        ({"max_slippage": D("0")}, "the model would refuse every swap"),
    ],
)
def test_malformed_settings_are_refused(settings, match):
    with pytest.raises(ValueError, match=match):
        ExecutionSettings(**settings)
