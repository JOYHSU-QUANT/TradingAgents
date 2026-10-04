"""Uniswap v3 pool prices as whole-token ``Decimal`` values.

A pool states its price two ways, both as raw units of ``token1`` per raw
unit of ``token0``: ``sqrtPriceX96``, the square root of that ratio as a
Q64.96 fixed-point integer, and a tick, where the ratio is ``1.0001 ** tick``.
Both functions here undo the fixed point, scale by the two tokens' decimals,
and answer in the direction the caller names: how many whole units of the
pool's other token one whole ``base`` token is worth.

The ``sqrtPriceX96`` conversion is exact rational arithmetic rounded once
into the package's decimal context. The tick conversion raises 1.0001 to the
tick at 60 working digits and then rounds into the same context.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Final

from .decimal_context import DECIMAL_CONTEXT
from .types import Pool, Token

__all__ = [
    "MAX_SQRT_RATIO",
    "MAX_TICK",
    "MIN_SQRT_RATIO",
    "MIN_TICK",
    "price_from_sqrt_price_x96",
    "price_from_tick",
]

# The bounds v3-core's ``TickMath`` enforces. A pool's ``sqrtPriceX96`` is at
# least ``MIN_SQRT_RATIO`` and strictly below ``MAX_SQRT_RATIO``.
MIN_TICK: Final = -887272
MAX_TICK: Final = 887272
MIN_SQRT_RATIO: Final = 4295128739
MAX_SQRT_RATIO: Final = 1461446703485210103287273052203988822378723970342

_Q192: Final = 2**192
_TICK_BASE: Final = Decimal("1.0001")
# The package's context, widened: the tick power is worked at 60 digits.
_WORKING: Final = DECIMAL_CONTEXT.copy()
_WORKING.prec = 60


def _base_is_token0(pool: Pool, base: Token) -> bool:
    if base == pool.token0:
        return True
    if base == pool.token1:
        return False
    raise ValueError(
        f"{base.symbol} is not in the pool {pool.token0.symbol}/{pool.token1.symbol} "
        f"({pool.address})"
    )


def price_from_sqrt_price_x96(pool: Pool, sqrt_price_x96: int, *, base: Token) -> Decimal:
    """The price of one whole ``base`` token, in the pool's other token, at ``sqrt_price_x96``."""
    if (
        isinstance(sqrt_price_x96, bool)
        or not isinstance(sqrt_price_x96, int)
        or not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO
    ):
        raise ValueError(
            f"sqrtPriceX96 must be an integer in [{MIN_SQRT_RATIO}, {MAX_SQRT_RATIO}), "
            f"got {sqrt_price_x96!r}"
        )
    token1_per_token0 = Fraction(sqrt_price_x96**2, _Q192) * Fraction(
        10**pool.token0.decimals, 10**pool.token1.decimals
    )
    price = token1_per_token0 if _base_is_token0(pool, base) else 1 / token1_per_token0
    return DECIMAL_CONTEXT.divide(Decimal(price.numerator), Decimal(price.denominator))


def price_from_tick(pool: Pool, tick: int, *, base: Token) -> Decimal:
    """The price of one whole ``base`` token, in the pool's other token, at ``tick``."""
    if isinstance(tick, bool) or not isinstance(tick, int) or not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError(f"tick must be an integer in [{MIN_TICK}, {MAX_TICK}], got {tick!r}")
    token1_per_token0 = _WORKING.scaleb(
        _WORKING.power(_TICK_BASE, tick), pool.token0.decimals - pool.token1.decimals
    )
    price = (
        token1_per_token0
        if _base_is_token0(pool, base)
        else _WORKING.divide(Decimal(1), token1_per_token0)
    )
    return DECIMAL_CONTEXT.plus(price)
