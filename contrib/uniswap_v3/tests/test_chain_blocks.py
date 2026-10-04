"""Time to block: against the recording, and against small chains built here."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.chain.blocks import ChainBlockLocator
from contrib.uniswap_v3.chain.errors import BlockNotFound, MalformedResponse, RpcConfigError
from contrib.uniswap_v3.ports import BlockLocator
from contrib.uniswap_v3.tests.fakes.rpc import (
    ReplayProvider,
    ScriptedProvider,
    block_result,
    rpc_over,
)
from contrib.uniswap_v3.tests.fixtures import BAR_TIME, CASSETTE


def _chain(
    timestamps: list[int], lies: dict[int, int] | None = None, *, missing: range = range(0)
) -> ScriptedProvider:
    """A node whose block ``n`` is at ``timestamps[n]``; the last one is the head.

    While ``lies`` lists a block, the node claims that time for it instead.
    The node has no block in ``missing``.
    """
    lies = {} if lies is None else lies

    def respond(method, params):
        assert method == "eth_getBlockByNumber"
        number = len(timestamps) - 1 if params[0] == "latest" else int(params[0], 16)
        if number >= len(timestamps) or number in missing:
            return {"result": None}
        return {"result": block_result(number, lies.get(number, timestamps[number]))}

    return ScriptedProvider(respond)


def _numbered(provider: ScriptedProvider) -> list[int]:
    """The block numbers asked for by number, in order."""
    return [int(params[0], 16) for _, params in provider.requests if params[0] != "latest"]


def _uneven(count: int) -> list[int]:
    """Strictly increasing timestamps with gaps of 1 to 40 seconds."""
    timestamps, now = [], 1_000
    for number in range(count):
        timestamps.append(now)
        now += 1 + (number * 37) % 40
    return timestamps


def test_the_locator_is_a_block_locator():
    rpc, _ = rpc_over(_chain([10, 20]))
    assert isinstance(ChainBlockLocator(rpc), BlockLocator)


def test_the_recorded_bar_boundary_is_block_18251965():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    block = ChainBlockLocator(rpc).first_block_at_or_after(BAR_TIME)
    assert block == 18_251_965
    # The definition, read back: this block is at or after the time and the
    # one before it is not.
    assert rpc.header(block - 1).timestamp < BAR_TIME <= rpc.header(block).timestamp


@pytest.mark.parametrize("finality_depth", [0, 64])
def test_every_time_on_a_small_chain_matches_a_linear_scan(finality_depth):
    timestamps = _uneven(150)
    rpc, _ = rpc_over(_chain(timestamps))
    # One locator for every time, so later searches start from kept blocks.
    locator = ChainBlockLocator(rpc, finality_depth=finality_depth)
    for time in range(timestamps[0] - 2, timestamps[-1] + 1):
        expected = next(n for n, at in enumerate(timestamps) if at >= time)
        assert locator.first_block_at_or_after(time) == expected, time


def test_a_time_at_or_before_the_first_block_is_block_zero():
    rpc, _ = rpc_over(_chain([10, 20, 30]))
    locator = ChainBlockLocator(rpc, finality_depth=0)
    assert locator.first_block_at_or_after(0) == 0
    assert locator.first_block_at_or_after(10) == 0
    assert locator.first_block_at_or_after(11) == 1


def test_a_time_the_chain_has_not_reached_raises():
    rpc, _ = rpc_over(_chain([10, 20, 30]))
    locator = ChainBlockLocator(rpc)
    assert locator.first_block_at_or_after(30) == 2
    with pytest.raises(BlockNotFound, match="no block at or after 31 yet: the latest, block 2"):
        locator.first_block_at_or_after(31)


def test_final_blocks_are_read_once_and_narrow_the_next_search():
    provider = _chain(_uneven(1_000))
    rpc, _ = rpc_over(provider)
    locator = ChainBlockLocator(rpc, finality_depth=64)
    time = _uneven(1_000)[400]

    assert locator.first_block_at_or_after(time) == 400
    first = _numbered(provider)
    assert len(first) == len(set(first)) and len(first) <= 11

    provider.requests.clear()
    assert locator.first_block_at_or_after(time) == 400
    # Blocks 399 and 400 are kept, so only the head is asked for.
    assert _numbered(provider) == []
    assert [params[0] for _, params in provider.requests] == ["latest"]


def test_blocks_near_the_head_are_read_again():
    timestamps = _uneven(100)
    provider = _chain(timestamps)
    rpc, _ = rpc_over(provider)
    locator = ChainBlockLocator(rpc, finality_depth=64)

    assert locator.first_block_at_or_after(timestamps[90]) == 90
    first = _numbered(provider)
    provider.requests.clear()
    assert locator.first_block_at_or_after(timestamps[90]) == 90
    # Every block the first search read past the final one (35) is read again.
    assert [n for n in _numbered(provider) if n > 35] == [n for n in first if n > 35]
    assert all(n > 35 for n in _numbered(provider))


@pytest.mark.parametrize(
    ("timestamps", "time"),
    [
        # Two blocks at the same second.
        ([10, 20, 30, 30, 50], 35),
        # A block later than the one after it.
        ([10, 20, 60, 40, 50], 45),
    ],
)
def test_block_times_out_of_order_raise(timestamps, time):
    rpc, _ = rpc_over(_chain(timestamps))
    with pytest.raises(MalformedResponse, match="block times must increase"):
        ChainBlockLocator(rpc, finality_depth=0).first_block_at_or_after(time)


def test_a_search_that_failed_its_check_keeps_nothing_it_read():
    timestamps = _uneven(1_000)
    lies = {249: timestamps[900]}
    rpc, _ = rpc_over(_chain(timestamps, lies))
    locator = ChainBlockLocator(rpc, finality_depth=0)
    with pytest.raises(MalformedResponse, match="block times must increase"):
        locator.first_block_at_or_after(timestamps[300])
    # The node recovers. Block 249's false time must not bound a later search.
    lies.clear()
    for block in (300, 260, 400, 600, 100):
        assert locator.first_block_at_or_after(timestamps[block]) == block


def test_kept_blocks_are_held_against_what_a_later_search_reads():
    timestamps = _uneven(1_000)
    lies: dict[int, int] = {}
    rpc, _ = rpc_over(_chain(timestamps, lies))
    locator = ChainBlockLocator(rpc, finality_depth=0)
    assert locator.first_block_at_or_after(timestamps[400]) == 400
    # A block kept from that search bounds the next one from below. Every
    # block read past it now claims a time before it, in order among themselves.
    lies.update({number: number for number in range(500, 999)})
    with pytest.raises(MalformedResponse, match="block times must increase"):
        locator.first_block_at_or_after(timestamps[700])

    # And one bounds a search from above: the blocks read under it now claim
    # times after it, in order among themselves and before the head.
    lies.clear()
    lies.update({number: timestamps[600] + number - 250 for number in range(250, 374)})
    with pytest.raises(MalformedResponse, match="block times must increase"):
        locator.first_block_at_or_after(timestamps[300])


def test_a_node_that_lacks_a_final_block_is_a_setup_fault():
    timestamps = _uneven(1_000)
    # Only the last 128 blocks are kept, as on a node that prunes history.
    rpc, _ = rpc_over(_chain(timestamps, missing=range(0, 872)))
    with pytest.raises(RpcConfigError, match="does not keep block 499, which is final"):
        ChainBlockLocator(rpc).first_block_at_or_after(timestamps[300])


def test_a_node_that_lacks_a_block_near_the_head_is_behind_and_worth_another_try():
    timestamps = _uneven(1_000)
    # The node asked for a block trails the one that reported the head (999).
    rpc, _ = rpc_over(_chain(timestamps, missing=range(990, 999)))
    locator = ChainBlockLocator(rpc, finality_depth=64)
    with pytest.raises(BlockNotFound, match="does not have block 99"):
        locator.first_block_at_or_after(timestamps[995])
    # Block 935 is the last final one: one deeper and it is the node's history.
    rpc, _ = rpc_over(_chain(timestamps, missing=range(935, 936)))
    locator = ChainBlockLocator(rpc, finality_depth=64)
    assert locator.first_block_at_or_after(timestamps[990]) == 990
    with pytest.raises(RpcConfigError, match="does not keep block 935, which is final"):
        locator.first_block_at_or_after(timestamps[935])


@pytest.mark.parametrize("time", [-1, 1.5, "10", True])
def test_a_time_is_a_non_negative_integer(time):
    rpc, _ = rpc_over(_chain([10, 20]))
    with pytest.raises(ValueError, match="epoch seconds"):
        ChainBlockLocator(rpc).first_block_at_or_after(time)


@pytest.mark.parametrize("depth", [-1, 1.5, True])
def test_a_finality_depth_is_a_non_negative_integer(depth):
    rpc, _ = rpc_over(_chain([10, 20]))
    with pytest.raises(ValueError, match="finality_depth"):
        ChainBlockLocator(rpc, finality_depth=depth)
