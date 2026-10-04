"""The strategy registry: one placeholder, built by name."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.ports import Strategy
from contrib.uniswap_v3.strategies.fixed_weights import FixedWeights
from contrib.uniswap_v3.strategies.registry import build_strategy, strategy_names

PARAMS = {"weights": {"USDC": "0.5", "WETH": "0.5"}, "band": "0.05"}


def test_the_only_registered_strategy_is_the_placeholder():
    # Constraint of the package: no strategy but the placeholder ships here.
    assert strategy_names() == ("fixed_weights",)


def test_a_registered_name_builds_a_strategy_from_its_params():
    strategy = build_strategy("fixed_weights", PARAMS)
    assert strategy == FixedWeights.from_params(PARAMS)
    assert isinstance(strategy, Strategy)


def test_an_unknown_name_is_refused_and_the_known_ones_are_listed():
    with pytest.raises(ValueError, match=r"unknown strategy 'momentum'; known: \['fixed_weights'\]"):
        build_strategy("momentum", PARAMS)
    with pytest.raises(ValueError, match="unknown strategy"):
        build_strategy(["fixed_weights"], PARAMS)  # type: ignore[arg-type]


def test_the_factorys_own_refusal_reaches_the_caller():
    with pytest.raises(ValueError, match="takes exactly the params"):
        build_strategy("fixed_weights", {})
