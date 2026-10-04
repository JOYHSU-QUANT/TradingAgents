"""Pool price conversion, against hand-set values and an independent formula.

The hand-set cases choose a ``sqrtPriceX96`` whose price is a round number,
so the expected value is written down rather than computed. The others
compare against a formula written differently from the code's: the inverse
ratio as one division for ``sqrtPriceX96``, and ``exp(tick * ln 1.0001)``
for ticks, where the code uses an integer power.
"""

from __future__ import annotations

from decimal import Context, Decimal, localcontext
from fractions import Fraction

import pytest

from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.prices import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    price_from_sqrt_price_x96,
    price_from_tick,
)
from contrib.uniswap_v3.domain.types import Pool, Token

USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]

# A pool whose tokens share their decimals, so the raw ratio is the price.
A = Token("A", "0x" + "0" * 39 + "1", 18)
B = Token("B", "0x" + "0" * 39 + "2", 18)
A_B = Pool("0x" + "0" * 39 + "3", A, B, 3000)

Q96 = 2**96


def _close(got: Decimal, expected: Decimal, *, digits: int) -> bool:
    """``got`` agrees with ``expected`` to ``digits`` significant digits, compared exactly."""
    return abs(Fraction(got) - Fraction(expected)) <= Fraction(expected) / 10**digits


@pytest.mark.parametrize(
    ("pool", "sqrt_price_x96", "base", "expected"),
    [
        # sqrt = 25_000, so 6.25e8 wei per raw USDC: 0.000625 WETH per USDC.
        (USDC_WETH, 25_000 * Q96, WETH, Decimal("1600")),
        (USDC_WETH, 25_000 * Q96, USDC, Decimal("0.000625")),
        # sqrt = 400_000, so 1.6e11 wei per satoshi: 16 WETH per WBTC.
        (WBTC_WETH, 400_000 * Q96, WBTC, Decimal("16")),
        (WBTC_WETH, 400_000 * Q96, WETH, Decimal("0.0625")),
        (A_B, Q96, A, Decimal("1")),
        (A_B, Q96, B, Decimal("1")),
    ],
)
def test_a_round_sqrt_price_gives_the_round_price(pool, sqrt_price_x96, base, expected):
    assert price_from_sqrt_price_x96(pool, sqrt_price_x96, base=base) == expected


@pytest.mark.parametrize("sqrt_price_x96", [MIN_SQRT_RATIO, 1987654321098765432109876543210987])
def test_an_inexact_sqrt_price_is_rounded_once_to_28_digits(sqrt_price_x96):
    with localcontext(Context(prec=28)):
        usdc_per_weth = Decimal(2**192 * 10**12) / Decimal(sqrt_price_x96**2)
    got = price_from_sqrt_price_x96(USDC_WETH, sqrt_price_x96, base=WETH)
    assert got == usdc_per_weth
    assert len(got.as_tuple().digits) <= 28


def test_the_two_directions_are_reciprocals():
    sqrt_price_x96 = 1987654321098765432109876543210987
    one_way = price_from_sqrt_price_x96(USDC_WETH, sqrt_price_x96, base=WETH)
    other_way = price_from_sqrt_price_x96(USDC_WETH, sqrt_price_x96, base=USDC)
    with localcontext(Context(prec=60)):
        assert _close(one_way * other_way, Decimal(1), digits=26)


@pytest.mark.parametrize(
    "sqrt_price_x96", [MIN_SQRT_RATIO - 1, MAX_SQRT_RATIO, 0, -Q96, True, float(Q96), str(Q96)]
)
def test_a_sqrt_price_outside_the_pool_range_is_refused(sqrt_price_x96):
    with pytest.raises(ValueError, match="sqrtPriceX96 must be an integer in"):
        price_from_sqrt_price_x96(A_B, sqrt_price_x96, base=A)


def test_a_base_token_outside_the_pool_is_refused():
    with pytest.raises(ValueError, match="WBTC is not in the pool USDC/WETH"):
        price_from_sqrt_price_x96(USDC_WETH, Q96, base=WBTC)
    with pytest.raises(ValueError, match="WBTC is not in the pool USDC/WETH"):
        price_from_tick(USDC_WETH, 0, base=WBTC)


@pytest.mark.parametrize(
    ("tick", "base", "expected"),
    [
        (0, A, Decimal("1")),
        (1, A, Decimal("1.0001")),
        (2, A, Decimal("1.00020001")),
        (-1, B, Decimal("1.0001")),
    ],
)
def test_a_small_tick_gives_the_power_of_1_0001(tick, base, expected):
    assert price_from_tick(A_B, tick, base=base) == expected


def _by_exp_and_ln(pool: Pool, tick: int, base: Token) -> Decimal:
    with localcontext(Context(prec=50)):
        token1_per_token0 = (Decimal(tick) * Decimal("1.0001").ln()).exp() * Decimal(10) ** (
            pool.token0.decimals - pool.token1.decimals
        )
        return token1_per_token0 if base == pool.token0 else 1 / token1_per_token0


@pytest.mark.parametrize(
    ("pool", "tick", "base", "low", "high"),
    [
        (USDC_WETH, 202511, WETH, "1600", "1610"),
        (USDC_WETH, 202511, USDC, "0.00062", "0.000625"),
        (WBTC_WETH, 256949, WBTC, "14.4", "14.5"),
        (WBTC_WETH, 256949, WETH, "0.069", "0.0695"),
        (A_B, MAX_TICK, A, "3.4e38", "3.5e38"),
        (A_B, MIN_TICK, A, "2.9e-39", "3.0e-39"),
    ],
)
def test_a_tick_price_matches_the_independent_formula(pool, tick, base, low, high):
    got = price_from_tick(pool, tick, base=base)
    assert _close(got, _by_exp_and_ln(pool, tick, base), digits=25)
    assert Decimal(low) < got < Decimal(high)
    assert len(got.as_tuple().digits) <= 28


def test_the_tick_and_sqrt_price_conversions_agree_at_the_chains_own_pair():
    # v3-core's TickMath maps MIN_TICK to MIN_SQRT_RATIO, a 33-bit integer,
    # so the two can only agree to about nine digits.
    assert _close(
        price_from_tick(A_B, MIN_TICK, base=A),
        price_from_sqrt_price_x96(A_B, MIN_SQRT_RATIO, base=A),
        digits=9,
    )


@pytest.mark.parametrize("tick", [MIN_TICK - 1, MAX_TICK + 1, True, 1.0, "1"])
def test_a_tick_outside_the_range_is_refused(tick):
    with pytest.raises(ValueError, match="tick must be an integer in"):
        price_from_tick(A_B, tick, base=A)


def test_the_ambient_decimal_context_does_not_change_a_price():
    sqrt_price_x96 = 1987654321098765432109876543210987
    by_sqrt = price_from_sqrt_price_x96(USDC_WETH, sqrt_price_x96, base=WETH)
    by_tick = price_from_tick(USDC_WETH, 202511, base=WETH)
    with localcontext(Context(prec=5)):
        assert price_from_sqrt_price_x96(USDC_WETH, sqrt_price_x96, base=WETH) == by_sqrt
        assert price_from_tick(USDC_WETH, 202511, base=WETH) == by_tick
