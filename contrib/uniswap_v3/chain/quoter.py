"""Exact-input quotes from QuoterV2, over one pool or a path of them.

QuoterV2's quote functions are not ``view``: each runs the swap inside a
revert that QuoterV2 itself catches, and returns what the swap would have
given. They are only ever reached with ``eth_call``, which sends nothing. A
quote names its block, so a backtest can ask an archive node what the same
swap would have returned then.

No price limit is set, so a pool is swapped against for as long as it has
liquidity. If it runs out, the pool stops at the end of its price range and
QuoterV2 reports what the part it did swap returned; that is refused here
(:class:`~.errors.InsufficientLiquidity`), since the quote would not be for
the amount asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from ..constants import QUOTER_V2
from ..domain.prices import MAX_SQRT_RATIO, MIN_SQRT_RATIO
from ..domain.types import Pool, Token
from .errors import InsufficientLiquidity, MalformedResponse, RpcConfigError
from .rpc import Rpc
from .units import from_raw, to_raw

__all__ = ["Quote", "quote_exact_input"]

# From v3-periphery's IQuoterV2:
# https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/IQuoterV2.sol
_QUOTER_ABI: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
    {
        "name": "quoteExactInput",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "path", "type": "bytes"},
            {"name": "amountIn", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96AfterList", "type": "uint160[]"},
            {"name": "initializedTicksCrossedList", "type": "uint32[]"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
)


@dataclass(frozen=True)
class Quote:
    """What an exact-input swap would have returned at the end of ``block``.

    Amounts are whole tokens. ``amount_out`` is after the pool fees and can
    be zero for a dust input. ``gas_estimate`` is the gas the pools' swaps
    used inside the quoter, in gas units. A real swap costs more: the
    transaction's base cost, the router's own work, the transfer of the
    input token and any token approval come on top.
    """

    token_in: Token
    token_out: Token
    amount_in: Decimal
    amount_out: Decimal
    gas_estimate: int
    block: int


def _tokens_along(token_in: Token, route: Sequence[Pool]) -> list[Token]:
    """``token_in`` and then the token each pool of ``route`` hands on."""
    if not route:
        raise ValueError("a route holds at least one pool")
    tokens = [token_in]
    for pool in route:
        if not isinstance(pool, Pool):
            raise ValueError(f"a route holds Pool values, got {pool!r}")
        if tokens[-1] == pool.token0:
            tokens.append(pool.token1)
        elif tokens[-1] == pool.token1:
            tokens.append(pool.token0)
        else:
            raise ValueError(
                f"the route reaches {pool.token0.symbol}/{pool.token1.symbol} holding "
                f"{tokens[-1].symbol}, which that pool does not trade"
            )
    return tokens


def _encode_path(tokens: Sequence[Token], route: Sequence[Pool]) -> bytes:
    """The packed path v3-periphery reads: token, then (3-byte fee, token) per hop."""
    path = bytes.fromhex(tokens[0].address[2:])
    for pool, token in zip(route, tokens[1:], strict=True):
        path += pool.fee.to_bytes(3, "big") + bytes.fromhex(token.address[2:])
    return path


def quote_exact_input(
    rpc: Rpc, token_in: Token, route: Sequence[Pool], amount_in: Decimal, *, block: int
) -> Quote:
    """Quote selling ``amount_in`` of ``token_in`` through ``route`` at the end of ``block``.

    ``route`` is the pools in the order they are crossed. A route the pools
    cannot fill reverts, which raises :class:`~.errors.CallReverted`.
    """
    tokens = _tokens_along(token_in, route)
    if tokens[-1] == token_in:
        raise ValueError(f"the route ends in {token_in.symbol}, the token it started with")
    raw_in = to_raw(token_in, amount_in)
    if raw_in == 0:
        raise ValueError("amount_in must be above zero")
    if rpc.chain_id not in QUOTER_V2:
        raise RpcConfigError(f"no QuoterV2 address is known for chain {rpc.chain_id}")
    quoter = QUOTER_V2[rpc.chain_id]

    if len(route) == 1:
        params = (token_in.address, tokens[-1].address, raw_in, route[0].fee, 0)
        result = rpc.call(quoter, _QUOTER_ABI, "quoteExactInputSingle", (params,), block=block)
    else:
        path = _encode_path(tokens, route)
        result = rpc.call(quoter, _QUOTER_ABI, "quoteExactInput", (path, raw_in), block=block)

    # Decoded by the ABI above: four outputs, the first and last uint256, the
    # second one price for a single pool and a list of them for a path.
    raw_out, after, _, gas_estimate = result
    for pool, sqrt_price_x96 in zip(route, [after] if len(route) == 1 else after, strict=True):
        # Where a swap with no price limit stops when nothing is left to
        # swap against: one step inside TickMath's bounds.
        if not MIN_SQRT_RATIO + 1 < sqrt_price_x96 < MAX_SQRT_RATIO - 1:
            raise InsufficientLiquidity(
                f"the pool {pool.token0.symbol}/{pool.token1.symbol} ({pool.address}) ran out "
                f"of liquidity at block {block} before {amount_in} {token_in.symbol} was swapped"
            )
    if gas_estimate <= 0:
        raise MalformedResponse(
            f"QuoterV2 at block {block} returned a gas estimate of {gas_estimate!r}"
        )
    return Quote(
        token_in=token_in,
        token_out=tokens[-1],
        amount_in=amount_in,
        amount_out=from_raw(tokens[-1], raw_out),
        gas_estimate=gas_estimate,
        block=block,
    )
