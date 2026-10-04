"""Whole-token amounts to raw integers and back, exactly."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from contrib.uniswap_v3.chain.units import from_raw, to_raw
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, TOKENS

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]


@pytest.mark.parametrize(
    ("token", "amount", "raw"),
    [
        (_USDC, "1000", 1_000_000_000),
        (_USDC, "0.000001", 1),
        (_USDC, "1.500000000", 1_500_000),
        (_USDC, "1E+3", 1_000_000_000),
        (_USDC, "0", 0),
        (_USDC, "0E-30", 0),
        (_WETH, "1.5", 15 * 10**17),
        (_WETH, "0.000000000000000001", 1),
        (_WETH, "123456789012345678901234567890.123456789012345678", 123456789012345678901234567890123456789012345678),
    ],
)
def test_to_raw_scales_by_the_tokens_decimals(token, amount, raw):
    assert to_raw(token, Decimal(amount)) == raw


@pytest.mark.parametrize(
    ("token", "amount", "message"),
    [
        (_USDC, Decimal("0.0000001"), "more decimal places than USDC's 6"),
        (_WETH, Decimal("1E-19"), "more decimal places than WETH's 18"),
        (_USDC, Decimal("-1"), "non-negative"),
        (_USDC, Decimal("-0"), "non-negative"),
        (_USDC, Decimal("Infinity"), "finite"),
        (_USDC, Decimal("1E+78"), "outside what a token amount can be"),
        (_USDC, Decimal("1E-78"), "outside what a token amount can be"),
        (_USDC, Decimal(f"{str(2**256)[:-6]}.{str(2**256)[-6:]}"), "does not fit a uint256"),
        (_USDC, 1, "Decimal"),
        (_USDC, 1.0, "Decimal"),
    ],
)
def test_to_raw_refuses_what_the_chain_cannot_carry(token, amount, message):
    with pytest.raises(ValueError, match=message):
        to_raw(token, amount)


def test_from_raw_is_exact_whatever_the_ambient_context():
    raw = 2**256 - 1
    with localcontext() as ambient:
        ambient.prec = 5
        whole = from_raw(_WETH, raw)
    # All 78 digits, the last 18 after the point.
    assert str(whole) == f"{str(raw)[:-18]}.{str(raw)[-18:]}"
    assert to_raw(_WETH, whole) == raw
    assert str(from_raw(_USDC, 1)) == "0.000001"
    assert str(from_raw(_USDC, 0)) == "0.000000"


@pytest.mark.parametrize("raw", [-1, 2**256, 1.0, "1", True])
def test_from_raw_refuses_what_is_not_a_uint256(raw):
    with pytest.raises(ValueError, match="fits a uint256"):
        from_raw(_USDC, raw)
