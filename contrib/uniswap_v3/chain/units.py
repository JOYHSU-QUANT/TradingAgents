"""Whole-token ``Decimal`` amounts to a contract's raw integers, and back.

Both directions are exact. An amount with more decimal places than its token
has is refused rather than rounded: the chain could not carry it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from ..domain.types import Token

__all__ = ["from_raw", "to_raw"]

_UINT256_MAX: Final = 2**256 - 1
# A uint256 has 78 digits, so a larger leading digit cannot fit either way.
_MAX_MAGNITUDE: Final = 77


def to_raw(token: Token, amount: Decimal) -> int:
    """``amount`` whole ``token`` in the token's smallest unit."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount.is_signed():
        raise ValueError(f"an amount is a finite, non-negative Decimal, got {amount!r}")
    if amount == 0:
        return 0
    if abs(amount.adjusted()) > _MAX_MAGNITUDE:
        raise ValueError(f"{amount!r} is outside what a token amount can be")
    # As a ratio of integers, so no decimal context takes part.
    numerator, denominator = amount.as_integer_ratio()
    raw, remainder = divmod(numerator * 10**token.decimals, denominator)
    if remainder:
        raise ValueError(f"{amount} has more decimal places than {token.symbol}'s {token.decimals}")
    if raw > _UINT256_MAX:
        raise ValueError(f"{amount} {token.symbol} does not fit a uint256")
    return raw


def from_raw(token: Token, raw: int) -> Decimal:
    """``raw`` smallest units of ``token`` as a whole-token amount."""
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= _UINT256_MAX:
        raise ValueError(f"a raw amount is an integer that fits a uint256, got {raw!r}")
    # Built from its digits: no decimal context is involved, so none rounds it.
    return Decimal((0, Decimal(raw).as_tuple().digits, -token.decimals))
