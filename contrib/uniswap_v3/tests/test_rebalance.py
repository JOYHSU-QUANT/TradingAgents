"""What the strategies share: the params and decimal checks, the weight places, the band, and the trigger.

Every portfolio below is worth 1000 USDC at the fakes' prices, WETH = 2000
and WBTC = 40000, so a share can be read off the balance: 0.005 WBTC is
200 USDC is 20%.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.types import Hold, TargetWeights
from contrib.uniswap_v3.strategies.rebalance import (
    floor_weight,
    rebalance_or_hold,
    require_band,
    require_decimal,
    require_params,
    weights_with_quote,
)
from contrib.uniswap_v3.tests.fakes.engine import PRICES, ledger, weights

TARGET = weights("0.5", "0.3", "0.2")
BAND = Decimal("0.05")


def _portfolio(usdc: str, weth: str, wbtc: str):
    return ledger(usdc, weth, wbtc).portfolio("USDC", PRICES)


def test_require_params_hands_back_a_mapping_with_exactly_the_expected_keys():
    params = {"a": 1, "b": "2"}
    assert require_params("x", params, frozenset({"a", "b"})) is params


@pytest.mark.parametrize(
    ("params", "got"),
    [
        ({"a": 1}, "['a']"),
        ({"a": 1, "b": 2, "c": 3}, "['a', 'b', 'c']"),
        ({1: "x", "b": 2}, "['1', 'b']"),
        (["a", "b"], "['a', 'b']"),
        (None, "None"),
    ],
)
def test_require_params_names_the_strategy_the_expected_keys_and_what_it_got(params, got):
    message = re.escape(f"x takes exactly the params ['a', 'b'], got {got}")
    with pytest.raises(ValueError, match=message):
        require_params("x", params, frozenset({"a", "b"}))


@pytest.mark.parametrize("band", [Decimal("0"), Decimal("0.05"), Decimal("0.9999")])
def test_a_band_in_the_unit_interval_passes(band):
    require_band(band)


@pytest.mark.parametrize(
    "band",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-0"),
        Decimal("-0.01"),
        Decimal("1"),
        0.05,
        "0.05",
    ],
)
def test_a_band_outside_the_unit_interval_or_not_a_decimal_is_refused(band):
    with pytest.raises(ValueError, match=r"band must be a Decimal in \[0, 1\)"):
        require_band(band)


@pytest.mark.parametrize(
    ("usdc", "weth", "wbtc"),
    [
        ("500", "0.15", "0.005"),  # 50 / 30 / 20: on target
        ("550", "0.125", "0.005"),  # 55 / 25 / 20: exactly on the band's edge
        ("0", "0", "0"),  # worth nothing: no shares to compare
    ],
)
def test_a_portfolio_inside_the_band_is_held(usdc, weth, wbtc):
    assert rebalance_or_hold(_portfolio(usdc, weth, wbtc), TARGET, BAND) == Hold()


@pytest.mark.parametrize(
    ("usdc", "weth", "wbtc"),
    [
        ("551", "0.1245", "0.005"),  # 55.1 / 24.9 / 20: USDC just over, WETH just under
        ("440", "0.165", "0.00575"),  # 44 / 33 / 23: only USDC is out, and under its target
        ("560", "0.135", "0.00425"),  # 56 / 27 / 17: only USDC is out, and over its target
        ("1000", "0", "0"),  # all in the quote token
    ],
)
def test_a_portfolio_outside_the_band_gets_the_target_itself(usdc, weth, wbtc):
    assert rebalance_or_hold(_portfolio(usdc, weth, wbtc), TARGET, BAND) is TARGET


def test_a_target_naming_a_token_the_portfolio_does_not_hold_raises():
    two_tokens = Ledger(
        balances={"USDC": Decimal("500"), "WETH": Decimal("0.25")}, gas_eth=Decimal(1)
    ).portfolio("USDC", {"WETH": PRICES["WETH"]})
    # The token the portfolio lacks is compared first, before any drift is found.
    wbtc_first = TargetWeights(
        {"WBTC": Decimal("0.2"), "USDC": Decimal("0.5"), "WETH": Decimal("0.3")}
    )
    with pytest.raises(ValueError, match="does not hold 'WBTC'"):
        rebalance_or_hold(two_tokens, wbtc_first, BAND)


@pytest.mark.parametrize(
    ("value", "within"),
    [(Decimal("0.5"), lambda v: v > 0), (Decimal(1), lambda v: 0 < v <= 1)],
)
def test_a_finite_decimal_within_its_bounds_passes(value, within):
    require_decimal(value, "x", within, "above 0")


@pytest.mark.parametrize(
    "value", [Decimal(0), Decimal("NaN"), Decimal("Infinity"), 0.5, "0.5", None]
)
def test_a_decimal_outside_its_bounds_or_not_a_finite_decimal_is_refused(value):
    with pytest.raises(ValueError, match=re.escape(f"x must be a Decimal above 0, got {value!r}")):
        require_decimal(value, "x", lambda v: v > 0, "above 0")


def test_a_weight_is_cut_down_to_four_places():
    assert floor_weight(Decimal("0.123456")) == Decimal("0.1234")
    assert floor_weight(Decimal("0.99999")) == Decimal("0.9999")
    assert floor_weight(Decimal("0.5")) == Decimal("0.5000")


def test_the_quote_gets_exactly_what_the_cut_weights_leave():
    assert weights_with_quote({"WETH": Decimal("0.6832"), "WBTC": Decimal("0.3167")}, "USDC") == (
        weights("0.0001", "0.6832", "0.3167")
    )
    assert weights_with_quote({"WETH": Decimal("0.5"), "WBTC": Decimal("0.5")}, "USDC") == (
        weights("0", "0.5", "0.5")
    )
    assert weights_with_quote({}, "USDC") == TargetWeights({"USDC": Decimal(1)})
    # Weights that add up to more than 1 leave the quote nothing to hold, and are refused.
    with pytest.raises(ValueError, match="non-negative"):
        weights_with_quote({"WETH": Decimal("0.7"), "WBTC": Decimal("0.4")}, "USDC")
