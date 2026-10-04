"""A scripted chain for the bar tests: blocks twelve seconds apart, and two pools.

Block ``n`` is at ``GENESIS_TIME + 12 * n``, so with the default genesis a
day boundary falls exactly on a block. Every pool answers ``slot0`` and
``observe`` with the node's defaults unless a test has set something else
for that pool and block. A test changes the chain by assigning to the
node's attributes between reads.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from web3 import Web3

from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.domain.bars import Finality, PoolBar
from contrib.uniswap_v3.domain.types import Pool
from contrib.uniswap_v3.tests.fakes.rpc import (
    ScriptedProvider,
    block_hash,
    block_result,
    encoded,
)

__all__ = [
    "BTC_TICK",
    "DAY",
    "DEFAULT_TICK",
    "FIRST_DAY",
    "GENESIS_TIME",
    "UP_HALF_TICK",
    "FakeNode",
    "block_at",
    "block_hash",
    "pool_bar",
    "put_day",
    "sqrt_price_at",
]

DAY = 86_400
# 2024-01-01 00:00:00 UTC, which block 1,000 opens.
FIRST_DAY = 1_704_067_200
GENESIS_TIME = FIRST_DAY - 12 * 1_000
# Near 2,000 USDC per WETH in the USDC/WETH pool, and near 15 WETH per WBTC
# in the WBTC/WETH pool.
DEFAULT_TICK = 200_311
BTC_TICK = 257_300

_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]


def block_at(time: int) -> int:
    """The first block at or after ``time`` on a chain with regular block times."""
    return -(-(time - GENESIS_TIME) // 12)

_SLOT0 = "0x" + Web3.keccak(text="slot0()")[:4].hex()
_OBSERVE = "0x" + Web3.keccak(text="observe(uint32[])")[:4].hex()
_SLOT0_TYPES = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]


def sqrt_price_at(tick: int) -> int:
    """A ``sqrtPriceX96`` inside ``tick``: the square root of ``1.0001 ** tick``, in Q64.96."""
    with localcontext() as context:
        context.prec = 80
        return int((Decimal("1.0001") ** tick).sqrt() * 2**96)


def pool_bar(
    pool: Pool = _USDC_WETH, *, time: int = FIRST_DAY, tick: int | None = None, **changes: Any
) -> PoolBar:
    """A final reading of ``pool`` at ``time``, priced at ``tick``, with ``changes`` over it.

    Left out, the tick is :data:`DEFAULT_TICK` for the USDC/WETH pool and
    :data:`BTC_TICK` for any other.
    """
    if tick is None:
        tick = DEFAULT_TICK if pool == _USDC_WETH else BTC_TICK
    fields = {
        "chain_id": ETHEREUM_MAINNET,
        "pool": pool.address,
        "interval_seconds": DAY,
        "time": time,
        "close_block": 999,
        "close_block_hash": block_hash(999),
        "close_block_time": time - 12,
        "sqrt_price_x96": sqrt_price_at(tick),
        "tick": tick,
        "twap_tick": tick,
        "twap_window_seconds": 1_800,
        "base_fee_wei": 7 * 10**9,
        "finality": Finality.FINAL,
    }
    return PoolBar(**{**fields, **changes})


# A tick this much lower prices WETH, and WBTC through it, half as high again.
UP_HALF_TICK = DEFAULT_TICK - 4_055


def put_day(
    store: Any,
    day: int,
    *,
    eth_tick: int = DEFAULT_TICK,
    pools: tuple[Pool, ...] = (_USDC_WETH, _WBTC_WETH),
    **wbtc_changes: Any,
) -> None:
    """Store the readings of ``pools`` on the day ``day`` days after :data:`FIRST_DAY`.

    They close on the last block before the day's boundary. The USDC/WETH
    pool is priced at ``eth_tick``, and ``wbtc_changes`` go over the
    WBTC/WETH pool's reading.
    """
    time = FIRST_DAY + day * DAY
    close = block_at(time) - 1
    at = {
        "time": time,
        "close_block": close,
        "close_block_hash": block_hash(close),
        "close_block_time": time - 12,
    }
    readings = []
    if _USDC_WETH in pools:
        readings.append(pool_bar(_USDC_WETH, tick=eth_tick, **at))
    if _WBTC_WETH in pools:
        readings.append(pool_bar(_WBTC_WETH, **{**at, **wbtc_changes}))
    store.insert_bars(readings)


class FakeNode:
    """The chain a :class:`ScriptedProvider` answers from.

    - ``head``: the latest block's number.
    - ``slot0`` and ``twap_tick``: ``(pool address in lowercase, block)`` to
      what the pool answers there, in place of the defaults.
    - ``reverts``: the ``(pool address in lowercase, block)`` pairs whose
      ``observe`` reverts, and ``slot0_reverts`` those whose ``slot0`` does;
      ``revert_message`` is what the node says when one does.
    - ``errors``: ``block`` to the JSON-RPC error every ``eth_call`` at that
      block gets.
    - ``hashes`` and ``times``: ``block`` to a hash or timestamp in place of
      the regular one.
    - ``base_fee``: every block's base fee; ``None`` for a chain before London.
    """

    def __init__(self, *, head: int = 20_000) -> None:
        self.head = head
        # Near 2,000 USDC per WETH in the USDC/WETH pool's own order.
        self.default_slot0 = (sqrt_price_at(DEFAULT_TICK), DEFAULT_TICK)
        self.default_twap_tick = DEFAULT_TICK
        self.slot0: dict[tuple[str, int], tuple[int, int]] = {}
        self.twap_tick: dict[tuple[str, int], int] = {}
        self.reverts: set[tuple[str, int]] = set()
        self.slot0_reverts: set[tuple[str, int]] = set()
        self.revert_message = "execution reverted: OLD"
        self.errors: dict[int, dict[str, Any]] = {}
        self.hashes: dict[int, str] = {}
        self.times: dict[int, int] = {}
        self.base_fee: int | None = 7 * 10**9
        self.provider = ScriptedProvider(self._respond)

    def time_of(self, block: int) -> int:
        return self.times.get(block, GENESIS_TIME + 12 * block)

    def calls_at(self, block: int) -> int:
        """How many ``eth_call`` requests have named ``block``."""
        return sum(
            1
            for method, params in self.provider.requests
            if method == "eth_call" and int(params[1], 16) == block
        )

    def _revert(self) -> dict[str, Any]:
        return {"error": {"code": 3, "message": self.revert_message}}

    def _respond(self, method: str, params: Any) -> dict[str, Any]:
        if method == "eth_getBlockByNumber":
            number = self.head if params[0] == "latest" else int(params[0], 16)
            if number > self.head:
                return {"result": None}
            block = block_result(number, self.time_of(number), base_fee=self.base_fee)
            block["hash"] = self.hashes.get(number, block["hash"])
            return {"result": block}
        if method == "eth_call":
            call, block = params[0], int(params[1], 16)
            if block in self.errors:
                return {"error": self.errors[block]}
            key = (call["to"].lower(), block)
            if call["data"].startswith(_SLOT0):
                if key in self.slot0_reverts:
                    return self._revert()
                sqrt_price_x96, tick = self.slot0.get(key, self.default_slot0)
                return {
                    "result": encoded(_SLOT0_TYPES, [sqrt_price_x96, tick, 0, 1, 1, 0, True])
                }
            if call["data"].startswith(_OBSERVE):
                if key in self.reverts:
                    return self._revert()
                # The window is the first of the two ages asked for.
                window = int(call["data"][10 + 64 * 2 : 10 + 64 * 3], 16)
                mean = self.twap_tick.get(key, self.default_twap_tick)
                return {"result": encoded(["int56[]", "uint160[]"], [[0, mean * window], [0, 0]])}
        raise AssertionError(f"the fake node has no answer for {method} {params!r}")
