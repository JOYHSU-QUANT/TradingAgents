"""The strategy registry: the placeholder and the rule strategy, built by name."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.ports import Strategy
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


def test_the_registered_strategies_are_the_placeholder_and_the_rule():
    assert strategy_names() == ("fixed_weights", "trend_vol_weights")


@pytest.mark.parametrize(
    ("name", "params", "built"),
    [
        ("fixed_weights", PARAMS, FixedWeights.from_params(PARAMS)),
        ("trend_vol_weights", TREND_PARAMS, TrendVolWeights.from_params(TREND_PARAMS)),
    ],
)
def test_a_registered_name_builds_a_strategy_from_its_params(name, params, built):
    strategy = build_strategy(name, params)
    assert strategy == built
    assert isinstance(strategy, Strategy)


def test_an_unknown_name_is_refused_and_the_known_ones_are_listed():
    with pytest.raises(
        ValueError,
        match=r"unknown strategy 'momentum'; known: \['fixed_weights', 'trend_vol_weights'\]",
    ):
        build_strategy("momentum", PARAMS)
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy(["fixed_weights"], PARAMS)  # type: ignore[arg-type]


def test_the_factorys_own_refusal_reaches_the_caller():
    with pytest.raises(ValueError, match="takes exactly the params"):
        build_strategy("fixed_weights", {})
