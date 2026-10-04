"""Reading the pools at a bar boundary, and checking a reading against the final chain."""

from __future__ import annotations

from dataclasses import replace

import pytest

from contrib.uniswap_v3.chain.bars import confirm_pool_bars, read_pool_bars
from contrib.uniswap_v3.chain.blocks import FINALITY_DEPTH, ChainBlockLocator
from contrib.uniswap_v3.chain.errors import (
    BlockNotFound,
    CallReverted,
    MalformedResponse,
    RpcConfigError,
    RpcRejected,
)
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.domain.bars import BarSettings, Finality, PoolBar
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK,
    DAY,
    DEFAULT_TICK,
    FIRST_DAY,
    FakeNode,
    block_at,
    block_hash,
    sqrt_price_at,
)
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_POOLS = (_USDC_WETH, _WBTC_WETH)
_SETTINGS = BarSettings()
# The last block before the first day's boundary.
_CLOSE = block_at(FIRST_DAY) - 1


def _read(node: FakeNode, time: int = FIRST_DAY, *, pools=_POOLS, settings=_SETTINGS, final=None):
    rpc, _ = rpc_over(node.provider, attempts=1)
    final_block = node.head - FINALITY_DEPTH if final is None else final
    return read_pool_bars(
        rpc, ChainBlockLocator(rpc), pools, time, settings=settings, final_block=final_block
    )


def _stored(node: FakeNode, time: int = FIRST_DAY) -> tuple[PoolBar, ...]:
    """What a run read at ``time`` while its blocks could still be replaced."""
    return _read(node, time, final=0)


def _confirm(node: FakeNode, pending, *, final=None):
    rpc, _ = rpc_over(node.provider, attempts=1)
    return confirm_pool_bars(rpc, pending, final_block=node.head - FINALITY_DEPTH if final is None else final)


# --- read_pool_bars --------------------------------------------------------


def test_a_boundary_is_read_at_the_last_block_before_it():
    node = FakeNode()
    node.slot0[(_WBTC_WETH.address.lower(), _CLOSE)] = (sqrt_price_at(BTC_TICK), BTC_TICK)
    node.twap_tick[(_WBTC_WETH.address.lower(), _CLOSE)] = 257_250

    usdc_weth, wbtc_weth = _read(node)

    assert usdc_weth == PoolBar(
        chain_id=ETHEREUM_MAINNET,
        pool=_USDC_WETH.address,
        interval_seconds=DAY,
        time=FIRST_DAY,
        close_block=_CLOSE,
        close_block_hash=block_hash(_CLOSE),
        close_block_time=FIRST_DAY - 12,
        sqrt_price_x96=sqrt_price_at(DEFAULT_TICK),
        tick=DEFAULT_TICK,
        twap_tick=DEFAULT_TICK,
        twap_window_seconds=1_800,
        base_fee_wei=7 * 10**9,
        finality=Finality.FINAL,
    )
    assert wbtc_weth == replace(
        usdc_weth,
        pool=_WBTC_WETH.address,
        sqrt_price_x96=sqrt_price_at(BTC_TICK),
        tick=BTC_TICK,
        twap_tick=257_250,
    )
    # Two calls per pool, all at the close block and none at the boundary block.
    assert node.calls_at(_CLOSE) == 4
    assert node.calls_at(_CLOSE + 1) == 0


def test_a_boundary_between_two_blocks_closes_on_the_earlier_one():
    # Hourly bars on blocks twelve seconds apart, moved six seconds off the boundary.
    node = FakeNode()
    hour = FIRST_DAY + 3_600
    opening = block_at(hour)
    for block in range(opening - 2, opening + 3):
        node.times[block] = node.time_of(block) + 6
    (reading,) = _read(node, hour, pools=(_USDC_WETH,), settings=BarSettings(interval_seconds=3_600))
    assert (reading.close_block, reading.close_block_time) == (opening - 1, hour - 6)
    assert reading.interval_seconds == 3_600


def test_the_settings_twap_window_is_the_one_asked_for_and_recorded():
    node = FakeNode()
    (reading,) = _read(node, pools=(_USDC_WETH,), settings=BarSettings(twap_window_seconds=600))
    assert reading.twap_window_seconds == 600
    (observe,) = [
        params[0]["data"]
        for method, params in node.provider.requests
        if method == "eth_call" and "883bdbfd" in params[0]["data"]
    ]
    assert int(observe[10 + 128 : 10 + 192], 16) == 600


def test_a_boundary_whose_block_could_still_be_replaced_is_read_as_pending():
    node = FakeNode()
    # The boundary block itself has to be final, not only the close block.
    at_the_edge = _read(node, final=_CLOSE + 1)
    one_short = _read(node, final=_CLOSE)
    assert {reading.finality for reading in at_the_edge} == {Finality.FINAL}
    assert {reading.finality for reading in one_short} == {Finality.PENDING}


def test_a_boundary_the_chain_has_not_reached_is_not_read():
    node = FakeNode(head=block_at(FIRST_DAY) - 1)
    with pytest.raises(BlockNotFound, match="no block at or after"):
        _read(node)
    assert node.calls_at(_CLOSE) == 0


def test_a_block_before_london_has_no_base_fee_and_is_refused():
    node = FakeNode()
    node.base_fee = None
    with pytest.raises(MalformedResponse, match=f"block {_CLOSE} has no base fee"):
        _read(node)
    assert node.calls_at(_CLOSE) == 0


def test_an_oracle_that_does_not_reach_back_the_window_reverts():
    node = FakeNode()
    node.reverts.add((_WBTC_WETH.address.lower(), _CLOSE))
    with pytest.raises(CallReverted):
        _read(node)


def test_a_slot0_that_reverts_is_a_wrong_answer_and_not_a_missing_one():
    node = FakeNode()
    node.slot0_reverts.add((_WBTC_WETH.address.lower(), _CLOSE))
    with pytest.raises(MalformedResponse, match="slot0 of .* reverted at block"):
        _read(node)


class _FixedLocator:
    """A locator that answers every time with one block."""

    def __init__(self, block: int) -> None:
        self._block = block

    def first_block_at_or_after(self, time: int) -> int:
        return self._block


def test_a_locator_whose_block_does_not_fit_the_boundary_is_refused():
    node = FakeNode()
    rpc, _ = rpc_over(node.provider, attempts=1)

    def read(block: int):
        return read_pool_bars(
            rpc, _FixedLocator(block), _POOLS, FIRST_DAY, settings=_SETTINGS, final_block=node.head
        )

    # The block before the one it names is itself at the boundary, not before it.
    with pytest.raises(MalformedResponse, match="should be the last before the boundary"):
        read(_CLOSE + 2)
    with pytest.raises(MalformedResponse, match="no block comes before the boundary"):
        read(0)
    assert node.calls_at(_CLOSE + 1) == 0


def test_a_node_error_on_a_pool_read_is_raised_as_it_is():
    node = FakeNode()
    node.errors[_CLOSE] = {"code": -32000, "message": "something went wrong"}
    with pytest.raises(RpcRejected):
        _read(node)


def test_a_final_block_the_node_lacks_is_a_setup_error_and_a_recent_one_is_not():
    behind = {"code": -32000, "message": "header not found"}
    node = FakeNode()
    node.errors[_CLOSE] = behind
    with pytest.raises(RpcConfigError, match=f"does not keep block {_CLOSE}, which is final"):
        _read(node)
    with pytest.raises(BlockNotFound):
        _read(node, final=_CLOSE - 1)


# --- confirm_pool_bars -----------------------------------------------------


def test_a_reading_whose_block_is_on_the_final_chain_is_confirmed():
    node = FakeNode()
    pending = _stored(node)
    assert {reading.finality for reading in pending} == {Finality.PENDING}
    before = len(node.provider.requests)

    confirmed = _confirm(node, pending)

    assert confirmed == tuple(replace(reading, finality=Finality.FINAL) for reading in pending)
    # The two pools share their blocks: the close block and the one after, once each.
    assert [params[0] for _, params in node.provider.requests[before:]] == [
        hex(_CLOSE),
        hex(_CLOSE + 1),
    ]


def test_a_reading_whose_boundary_block_is_not_final_yet_is_left_pending():
    node = FakeNode()
    pending = _stored(node)
    before = len(node.provider.requests)
    assert _confirm(node, pending, final=_CLOSE) == ()
    assert len(node.provider.requests) == before
    assert len(_confirm(node, pending, final=_CLOSE + 1)) == 2


def test_a_reading_from_a_block_the_chain_dropped_is_reorged():
    node = FakeNode()
    pending = _stored(node)
    node.hashes[_CLOSE] = "0x" + "ab" * 32
    assert {reading.finality for reading in _confirm(node, pending)} == {Finality.REORGED}


def test_a_reading_whose_block_changed_its_time_is_reorged():
    node = FakeNode()
    pending = _stored(node)
    node.times[_CLOSE] = FIRST_DAY - 11
    assert {reading.finality for reading in _confirm(node, pending)} == {Finality.REORGED}


def test_a_reading_that_turned_out_not_to_be_the_last_before_the_boundary_is_reorged():
    # The block after the close block was replaced by one before the boundary.
    node = FakeNode()
    hour = FIRST_DAY + 3_600
    opening = block_at(hour)
    node.times[opening] = hour + 6
    (pending,) = _read(
        node, hour, pools=(_USDC_WETH,), settings=BarSettings(interval_seconds=3_600), final=0
    )
    assert pending.close_block == opening - 1
    node.times[opening] = hour - 6
    (checked,) = _confirm(node, (pending,))
    assert checked.finality is Finality.REORGED


def test_confirming_against_a_node_without_the_final_block_is_a_setup_error():
    node = FakeNode()
    pending = _stored(node)
    node.head = _CLOSE - 1
    rpc, _ = rpc_over(node.provider, attempts=1)
    with pytest.raises(RpcConfigError, match="which is final"):
        confirm_pool_bars(rpc, pending, final_block=_CLOSE + 1)
