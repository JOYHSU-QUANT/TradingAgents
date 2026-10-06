"""The strategy registry: the placeholder, the rule strategy and the gated one, built by name."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.ports import Strategy
from contrib.uniswap_v3.strategies.ai_gated_weights import AiGatedWeights
from contrib.uniswap_v3.strategies.fixed_weights import FixedWeights
from contrib.uniswap_v3.strategies.registry import build_strategy, strategy_names
from contrib.uniswap_v3.strategies.trend_vol_weights import TrendVolWeights

PARAMS = {"weights": {"USDC": "0.5", "WETH": "0.5"}, "band": "0.05"}
TREND_PARAMS = {
    "trend_window": 50,
    "vol_window": 20,
    "bars_per_year": 365,
    "target_vol": "0.4",
    "max_weight": "0.5",
    "band": "0.05",
}
GATED_PARAMS = {
    "rule": TREND_PARAMS,
    "multipliers": {
        "Buy": "1",
        "Overweight": "0.75",
        "Hold": "0.5",
        "Underweight": "0.25",
        "Sell": "0",
    },
}


def test_the_registered_strategies_are_the_placeholder_the_rule_and_the_gated_one():
    assert strategy_names() == ("ai_gated_weights", "fixed_weights", "trend_vol_weights")


@pytest.mark.parametrize(
    ("name", "params", "built"),
    [
        ("fixed_weights", PARAMS, FixedWeights.from_params(PARAMS)),
        ("trend_vol_weights", TREND_PARAMS, TrendVolWeights.from_params(TREND_PARAMS)),
        ("ai_gated_weights", GATED_PARAMS, AiGatedWeights.from_params(GATED_PARAMS)),
    ],
)
def test_a_registered_name_builds_a_strategy_from_its_params(name, params, built):
    strategy = build_strategy(name, params)
    assert strategy == built
    assert isinstance(strategy, Strategy)


def test_an_unknown_name_is_refused_and_the_known_ones_are_listed():
    with pytest.raises(
        ValueError,
        match=(
            r"unknown strategy 'momentum'; known: "
            r"\['ai_gated_weights', 'fixed_weights', 'trend_vol_weights'\]"
        ),
    ):
        build_strategy("momentum", PARAMS)
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy(["fixed_weights"], PARAMS)  # type: ignore[arg-type]


def test_the_factorys_own_refusal_reaches_the_caller():
    with pytest.raises(ValueError, match="takes exactly the params"):
        build_strategy("fixed_weights", {})
