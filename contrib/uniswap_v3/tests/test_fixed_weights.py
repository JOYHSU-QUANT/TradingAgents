"""The placeholder strategy: target 50/30/20 with a 0.05 band, at round prices.

Every portfolio below is worth 1000 USDC at WETH = 2000 and WBTC = 50000,
so a share can be read off the balance: 0.125 WETH is 250 USDC is 25%.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.types import Bar, Hold, MarketView, Portfolio, TargetWeights
from contrib.uniswap_v3.strategies.fixed_weights import FixedWeights

PRICES = {"WETH": Decimal("2000"), "WBTC": Decimal("50000")}
VIEW = MarketView((Bar(time=86_400, close_block=1, prices=PRICES, base_fee_wei=10**9),))
PARAMS = {"weights": {"USDC": "0.5", "WETH": "0.3", "WBTC": "0.2"}, "band": "0.05"}
STRATEGY = FixedWeights.from_params(PARAMS)


def _portfolio(usdc: str, weth: str, wbtc: str) -> Portfolio:
    return Portfolio(
        quote="USDC",
        balances={"USDC": Decimal(usdc), "WETH": Decimal(weth), "WBTC": Decimal(wbtc)},
        prices=PRICES,
    )


def test_from_params_reads_quoted_decimals():
    by_hand = FixedWeights(
        target=TargetWeights(
            {"USDC": Decimal("0.5"), "WETH": Decimal("0.3"), "WBTC": Decimal("0.2")}
        ),
        band=Decimal("0.05"),
    )
    assert FixedWeights.from_params(PARAMS) == by_hand


@pytest.mark.parametrize(
    ("usdc", "weth", "wbtc"),
    [
        ("500", "0.15", "0.004"),  # 50 / 30 / 20: on target
        ("530", "0.14", "0.0038"),  # 53 / 28 / 19: inside the band
        ("550", "0.125", "0.004"),  # 55 / 25 / 20: exactly on the band's edge
    ],
)
def test_a_portfolio_inside_the_band_is_held(usdc, weth, wbtc):
    assert STRATEGY.decide(VIEW, _portfolio(usdc, weth, wbtc)) == Hold()


@pytest.mark.parametrize(
    ("usdc", "weth", "wbtc"),
    [
        ("551", "0.1245", "0.004"),  # 55.1 / 24.9 / 20: USDC and WETH just outside
        ("1000", "0", "0"),  # all in the quote token
        ("500", "0.12", "0.0052"),  # 50 / 24 / 26: only the non-quote tokens drift
    ],
)
def test_a_portfolio_outside_the_band_is_sent_back_to_the_target(usdc, weth, wbtc):
    assert STRATEGY.decide(VIEW, _portfolio(usdc, weth, wbtc)) is STRATEGY.target


def test_a_zero_band_rebalances_on_any_drift_and_holds_on_target():
    strict = FixedWeights.from_params({**PARAMS, "band": 0})
    assert strict.decide(VIEW, _portfolio("500", "0.15", "0.004")) == Hold()
    assert strict.decide(VIEW, _portfolio("500.01", "0.149995", "0.004")) is strict.target


def test_a_portfolio_worth_nothing_is_held():
    assert STRATEGY.decide(VIEW, _portfolio("0", "0", "0")) == Hold()


def test_a_portfolio_with_other_tokens_than_the_targets_raises():
    two_tokens = Portfolio(
        quote="USDC",
        balances={"USDC": Decimal("500"), "WETH": Decimal("0.25")},
        prices={"WETH": Decimal("2000")},
    )
    with pytest.raises(ValueError, match="targets .* but the portfolio holds"):
        STRATEGY.decide(VIEW, two_tokens)


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({}, "takes exactly the params"),
        ({"weights": PARAMS["weights"]}, "takes exactly the params"),
        ({**PARAMS, "lookback": 5}, "takes exactly the params"),
        ({**PARAMS, "weights": ["USDC"]}, "weights must map token symbol"),
        ({**PARAMS, "weights": {"USDC": 0.5, "WETH": 0.5}}, "quoted decimal"),
        ({**PARAMS, "weights": {"USDC": True}}, "quoted decimal"),
        ({**PARAMS, "weights": {"USDC": "half", "WETH": "0.5"}}, "quoted decimal"),
        ({**PARAMS, "weights": {"USDC": "0.5", "WETH": "0.4"}}, "sum to exactly 1"),
        ({**PARAMS, "weights": {"USDC": "1.5", "WETH": "-0.5"}}, "non-negative"),
        ({**PARAMS, "band": 0.05}, "quoted decimal"),
        ({**PARAMS, "band": None}, "quoted decimal"),
        ({**PARAMS, "band": "1"}, r"band must be a Decimal in \[0, 1\)"),
        ({**PARAMS, "band": "-0.01"}, r"band must be a Decimal in \[0, 1\)"),
        ({**PARAMS, "band": "-0"}, r"band must be a Decimal in \[0, 1\)"),
        # Spellings ``Decimal`` would read but nobody means to write.
        ({**PARAMS, "band": "NaN"}, "quoted decimal"),
        ({**PARAMS, "band": " 0.05"}, "quoted decimal"),
        ({**PARAMS, "band": "0.05\n"}, "quoted decimal"),
        ({**PARAMS, "band": "0.0_5"}, "quoted decimal"),
        ({**PARAMS, "band": "5e-2"}, "quoted decimal"),
        ({**PARAMS, "band": "０.０５"}, "quoted decimal"),
        ({**PARAMS, 1: 2}, "takes exactly the params"),
        (["weights", "band"], "takes exactly the params"),
        (None, "takes exactly the params"),
    ],
)
def test_malformed_params_are_refused(params, match):
    with pytest.raises(ValueError, match=match):
        FixedWeights.from_params(params)


def test_whole_number_params_need_no_quotes():
    strategy = FixedWeights.from_params({"weights": {"USDC": 1}, "band": 0})
    assert strategy.target.weights == {"USDC": Decimal(1)}
