"""A pool's price at a block: ``slot0`` for the spot, ``observe`` for the TWAP.

Both come back raw (``sqrtPriceX96`` and ticks); :mod:`..domain.prices`
turns them into whole-token prices. Reading at block ``N`` gives the pool's
state at the end of block ``N``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ..domain.prices import MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK
from ..domain.types import Pool
from .errors import MalformedResponse
from .rpc import Rpc

__all__ = ["DEFAULT_TWAP_WINDOW_SECONDS", "Slot0", "read_slot0", "read_twap_tick"]

DEFAULT_TWAP_WINDOW_SECONDS: Final = 1800
# ``observe`` takes its ages as uint32.
_MAX_WINDOW_SECONDS: Final = 2**32 - 1

# From v3-core's IUniswapV3PoolState and IUniswapV3PoolDerivedState:
# https://github.com/Uniswap/v3-core/tree/main/contracts/interfaces/pool
_POOL_ABI: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
    {
        "name": "observe",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "secondsAgos", "type": "uint32[]"}],
        "outputs": [
            {"name": "tickCumulatives", "type": "int56[]"},
            {"name": "secondsPerLiquidityCumulativeX128s", "type": "uint160[]"},
        ],
    },
)


@dataclass(frozen=True)
class Slot0:
    """A pool's spot price at the end of ``block``, as the pool states it."""

    block: int
    sqrt_price_x96: int
    tick: int


def read_slot0(rpc: Rpc, pool: Pool, block: int) -> Slot0:
    """``pool``'s ``sqrtPriceX96`` and tick at the end of ``block``."""
    # Decoded by the ABI above: seven outputs, the first two integers.
    sqrt_price_x96, tick, *_ = rpc.call(pool.address, _POOL_ABI, "slot0", block=block)
    if not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO or not MIN_TICK <= tick <= MAX_TICK:
        raise MalformedResponse(
            f"slot0 of {pool.address} at block {block} holds sqrtPriceX96 {sqrt_price_x96!r} "
            f"and tick {tick!r}, which no initialised pool can"
        )
    return Slot0(block=block, sqrt_price_x96=sqrt_price_x96, tick=tick)


def read_twap_tick(
    rpc: Rpc, pool: Pool, block: int, *, window_seconds: int = DEFAULT_TWAP_WINDOW_SECONDS
) -> int:
    """The time-weighted mean tick of ``pool`` over the ``window_seconds`` ending at ``block``.

    Rounded toward negative infinity, as v3-periphery's ``OracleLibrary``
    rounds it. A pool whose oracle does not reach back that far reverts,
    which raises :class:`~.errors.CallReverted`.
    """
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or not 0 < window_seconds <= _MAX_WINDOW_SECONDS
    ):
        raise ValueError(
            f"window_seconds must be an integer from 1 to {_MAX_WINDOW_SECONDS}, "
            f"got {window_seconds!r}"
        )
    result = rpc.call(pool.address, _POOL_ABI, "observe", ([window_seconds, 0],), block=block)
    # The ABI fixes the element type, not how many elements come back.
    try:
        (then, now), _ = result
    except ValueError:
        raise MalformedResponse(f"observe of {pool.address} returned {result!r}") from None
    tick = (now - then) // window_seconds
    if not MIN_TICK <= tick <= MAX_TICK:
        raise MalformedResponse(
            f"observe of {pool.address} at block {block} gives a mean tick of {tick}, "
            f"outside [{MIN_TICK}, {MAX_TICK}]"
        )
    return tick
