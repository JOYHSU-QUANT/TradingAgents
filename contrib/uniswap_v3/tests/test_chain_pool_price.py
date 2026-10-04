"""Pool reads: the recorded block, and replies built here for the edges."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.chain.errors import MalformedResponse
from contrib.uniswap_v3.chain.pool_price import Slot0, read_slot0, read_twap_tick
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.prices import (
    MAX_SQRT_RATIO,
    MIN_SQRT_RATIO,
    price_from_sqrt_price_x96,
    price_from_tick,
)
from contrib.uniswap_v3.tests.fakes.rpc import (
    ReplayProvider,
    ScriptedProvider,
    answering,
    encoded,
    rpc_over,
)
from contrib.uniswap_v3.tests.fixtures import BLOCK, CASSETTE

_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]

_SLOT0_TYPES = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]


def _slot0(sqrt_price_x96: int, tick: int) -> ScriptedProvider:
    return answering({"result": encoded(_SLOT0_TYPES, [sqrt_price_x96, tick, 0, 1, 1, 0, True])})


def _observe(then: int, now: int) -> ScriptedProvider:
    return answering({"result": encoded(["int56[]", "uint160[]"], [[then, now], [0, 0]])})


def test_slot0_of_both_pools_at_the_recorded_block():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    usdc_weth = read_slot0(rpc, _USDC_WETH, BLOCK)
    wbtc_weth = read_slot0(rpc, _WBTC_WETH, BLOCK)
    assert usdc_weth == Slot0(
        block=BLOCK, sqrt_price_x96=1950377547303575102371563899111474, tick=202234
    )
    assert wbtc_weth == Slot0(
        block=BLOCK, sqrt_price_x96=31479216896307737165207620713757138, tick=257863
    )
    # 2023-08-26: ETH near 1,650 USDC and BTC near 15.8 ETH.
    eth = price_from_sqrt_price_x96(_USDC_WETH, usdc_weth.sqrt_price_x96, base=_WETH)
    btc = price_from_sqrt_price_x96(_WBTC_WETH, wbtc_weth.sqrt_price_x96, base=_WBTC)
    assert Decimal(1645) < eth < Decimal(1655)
    assert Decimal("15.7") < btc < Decimal("15.9")
    # The tick is the price rounded down to a tick, in the pool's own order.
    for pool, state in ((_USDC_WETH, usdc_weth), (_WBTC_WETH, wbtc_weth)):
        exact = price_from_sqrt_price_x96(pool, state.sqrt_price_x96, base=pool.token0)
        assert (
            price_from_tick(pool, state.tick, base=pool.token0)
            <= exact
            < price_from_tick(pool, state.tick + 1, base=pool.token0)
        )


def test_the_half_hour_twap_tick_of_both_pools_at_the_recorded_block():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    assert read_twap_tick(rpc, _USDC_WETH, BLOCK) == 202234
    assert read_twap_tick(rpc, _WBTC_WETH, BLOCK) == 257863


def test_the_twap_asks_for_the_window_and_now():
    provider = _observe(0, 1800 * 7)
    rpc, _ = rpc_over(provider)
    assert read_twap_tick(rpc, _USDC_WETH, 7, window_seconds=1800) == 7
    [(_, params)] = provider.requests
    # observe(uint32[]) with [1800, 0]: the selector, then the encoded array.
    assert params[0]["data"].endswith(encoded(["uint32[]"], [[1800, 0]])[2:])


@pytest.mark.parametrize(
    ("then", "now", "window", "expected"),
    [
        (100, 100 + 600 * 5, 600, 5),
        (0, 1799, 1800, 0),
        # A negative mean that does not divide evenly rounds down, away from zero.
        (0, -1, 1800, -1),
        (0, -1800, 1800, -1),
        (0, -1801, 1800, -2),
        (-500, -500 - 60 * 3, 60, -3),
    ],
)
def test_the_twap_tick_rounds_toward_negative_infinity(then, now, window, expected):
    rpc, _ = rpc_over(_observe(then, now))
    assert read_twap_tick(rpc, _USDC_WETH, 7, window_seconds=window) == expected


@pytest.mark.parametrize("window", [0, -1, 2**32, 1800.0, True])
def test_a_twap_window_is_a_positive_uint32(window):
    rpc, _ = rpc_over(_observe(0, 0))
    with pytest.raises(ValueError, match="window_seconds"):
        read_twap_tick(rpc, _USDC_WETH, 7, window_seconds=window)


@pytest.mark.parametrize("cumulatives", [[], [5], [1, 2, 3]])
def test_an_observe_reply_without_exactly_two_readings_is_refused(cumulatives):
    reply = encoded(["int56[]", "uint160[]"], [cumulatives, [0] * len(cumulatives)])
    rpc, _ = rpc_over(answering({"result": reply}))
    with pytest.raises(MalformedResponse, match="observe of 0x88e6.* returned"):
        read_twap_tick(rpc, _USDC_WETH, 7)


def test_a_mean_tick_no_pool_can_have_is_refused():
    rpc, _ = rpc_over(_observe(0, 887_273 * 1800))
    with pytest.raises(MalformedResponse, match="mean tick of 887273"):
        read_twap_tick(rpc, _USDC_WETH, 7)


@pytest.mark.parametrize(
    ("sqrt_price_x96", "tick"),
    [
        # An uninitialised pool.
        (0, 0),
        (MIN_SQRT_RATIO - 1, 0),
        (MAX_SQRT_RATIO, 0),
        (2**96, 887_273),
        (2**96, -887_273),
    ],
)
def test_a_slot0_no_initialised_pool_can_hold_is_refused(sqrt_price_x96, tick):
    rpc, _ = rpc_over(_slot0(sqrt_price_x96, tick))
    with pytest.raises(MalformedResponse, match="which no initialised pool can"):
        read_slot0(rpc, _USDC_WETH, 7)


def test_slot0_at_the_bounds_is_read():
    rpc, _ = rpc_over(_slot0(MIN_SQRT_RATIO, -887_272))
    assert read_slot0(rpc, _USDC_WETH, 7) == Slot0(7, MIN_SQRT_RATIO, -887_272)
    rpc, _ = rpc_over(_slot0(MAX_SQRT_RATIO - 1, 887_272))
    assert read_slot0(rpc, _USDC_WETH, 7) == Slot0(7, MAX_SQRT_RATIO - 1, 887_272)
