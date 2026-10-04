"""Backfill: what it reads, what it skips, and what each kind of failure does to a run."""

from __future__ import annotations

from dataclasses import replace

import pytest

from contrib.uniswap_v3.backfill import (
    BackfillPlan,
    BackfillRangeError,
    BackfillSummary,
    backfill,
    plan_backfill,
)
from contrib.uniswap_v3.chain.errors import MalformedResponse, RpcRejected, RpcUnavailable
from contrib.uniswap_v3.config import StrategySpec, UniswapConfig
from contrib.uniswap_v3.constants import EARLIEST_BAR_TIME, ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.bars import BarSettings, Finality
from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.tests.fakes.node import DAY, FIRST_DAY, FakeNode, block_at
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

_TOKENS = TOKENS[ETHEREUM_MAINNET]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_CONFIG = UniswapConfig(
    chain_id=ETHEREUM_MAINNET,
    quote=_TOKENS["USDC"],
    tokens=(_TOKENS["USDC"], _TOKENS["WETH"], _TOKENS["WBTC"]),
    pools=(_USDC_WETH, _WBTC_WETH),
    strategy=StrategySpec(name="fixed_weights", params={}),
)
_ONE_POOL = replace(_CONFIG, tokens=(_TOKENS["USDC"], _TOKENS["WETH"]), pools=(_USDC_WETH,))
# The fake chain's head is in the third day: three boundaries have passed.
_DAYS = [FIRST_DAY, FIRST_DAY + DAY, FIRST_DAY + 2 * DAY]
_LAST = _DAYS[-1]


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _run(node: FakeNode, store, *, config=_CONFIG, start=FIRST_DAY, end=_LAST, lines=None):
    rpc, _ = rpc_over(node.provider, attempts=1)
    report = (lambda time, text: None) if lines is None else (lambda time, text: lines.append((time, text)))
    return backfill(rpc, store, config, start=start, end=end, report=report)


def _times(store, pool=_USDC_WETH) -> set[int]:
    return store.bar_times(ETHEREUM_MAINNET, pool.address, DAY, start=0, end=2**40)


def _rows(store) -> int:
    return sum(
        store.extent(ETHEREUM_MAINNET, pool.address, DAY)[0] for pool in (_USDC_WETH, _WBTC_WETH)
    )


# --- the plan --------------------------------------------------------------


def test_the_plan_lists_each_boundary_and_the_pools_it_lacks(store):
    assert plan_backfill(store, _CONFIG, start=FIRST_DAY, end=_LAST) == BackfillPlan(
        boundaries=3, missing=tuple((time, (_USDC_WETH, _WBTC_WETH)) for time in _DAYS)
    )
    # The range ends at the last boundary at or before its end.
    short = plan_backfill(store, _CONFIG, start=FIRST_DAY, end=_LAST - 1)
    assert [time for time, _ in short.missing] == _DAYS[:2]
    assert plan_backfill(store, _CONFIG, start=FIRST_DAY, end=FIRST_DAY).boundaries == 1


def test_the_plan_estimates_the_requests_from_the_pools_each_boundary_lacks(store):
    assert plan_backfill(store, _CONFIG, start=FIRST_DAY, end=_LAST).requests == 3 * (14 + 4)
    assert plan_backfill(store, _ONE_POOL, start=FIRST_DAY, end=FIRST_DAY).requests == 14 + 2


def test_the_plan_follows_the_configured_interval(store):
    hourly = replace(_CONFIG, bars=BarSettings(interval_seconds=3_600))
    plan = plan_backfill(store, hourly, start=FIRST_DAY, end=FIRST_DAY + 7_200)
    assert [time for time, _ in plan.missing] == [FIRST_DAY, FIRST_DAY + 3_600, FIRST_DAY + 7_200]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (FIRST_DAY + 1, _LAST, "must start on a bar boundary"),
        (EARLIEST_BAR_TIME[ETHEREUM_MAINNET] - DAY, _LAST, "bars on chain 1 start at"),
        (_LAST, FIRST_DAY, "before it starts"),
    ],
)
def test_a_range_that_cannot_be_backfilled_is_refused(store, start, end, message):
    with pytest.raises(BackfillRangeError, match=message):
        plan_backfill(store, _CONFIG, start=start, end=end)
    node = FakeNode()
    with pytest.raises(BackfillRangeError, match=message):
        _run(node, store, start=start, end=end)
    assert node.provider.requests == []


def test_the_earliest_bar_is_itself_a_boundary_of_a_one_day_bar(store):
    earliest = EARLIEST_BAR_TIME[ETHEREUM_MAINNET]
    assert earliest % DAY == 0
    assert plan_backfill(store, _CONFIG, start=earliest, end=earliest).boundaries == 1


# --- a run -----------------------------------------------------------------


def test_a_run_writes_every_pools_reading_at_every_boundary(store):
    node, lines = FakeNode(), []
    summary = _run(node, store, lines=lines)

    assert summary == BackfillSummary(
        written=3, already_stored=0, unanswered=(), not_reached=(), confirmed=0, reorged=0
    )
    assert _times(store) == _times(store, _WBTC_WETH) == set(_DAYS)
    for time in _DAYS:
        reading = store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, DAY, time)
        assert reading.close_block == block_at(time) - 1
        assert reading.finality is Finality.FINAL
    assert lines == [(time, f"block {block_at(time) - 1}, final") for time in _DAYS]


def test_a_second_run_reads_nothing_and_writes_nothing(store):
    node = FakeNode()
    _run(node, store)
    before = len(node.provider.requests)

    summary = _run(node, store)

    assert (summary.written, summary.already_stored) == (0, 3)
    assert _rows(store) == 6
    # The head, for the readings that might be waiting on finality, and nothing else.
    assert node.provider.requests[before:] == [("eth_getBlockByNumber", ("latest", False))]


def test_a_run_reads_only_what_the_store_lacks(store):
    node = FakeNode()
    _run(node, store, start=FIRST_DAY + DAY, end=FIRST_DAY + DAY)
    summary = _run(node, store)
    assert (summary.written, summary.already_stored) == (2, 1)
    assert node.calls_at(block_at(FIRST_DAY + DAY) - 1) == 4


def test_a_pool_added_to_the_config_is_read_alone_at_a_boundary_already_stored(store):
    node = FakeNode()
    _run(node, store, config=_ONE_POOL)
    assert _times(store, _WBTC_WETH) == set()

    summary = _run(node, store)

    assert (summary.written, summary.already_stored) == (3, 0)
    assert _times(store, _WBTC_WETH) == set(_DAYS)
    # Two calls for the first pool on the first run, two for the second pool now.
    assert node.calls_at(block_at(FIRST_DAY) - 1) == 4


def test_boundaries_the_chain_has_not_reached_are_reported_and_not_read(store):
    node = FakeNode()
    summary = _run(node, store, end=FIRST_DAY + 4 * DAY)
    assert summary.written == 3
    assert summary.not_reached == (FIRST_DAY + 3 * DAY, FIRST_DAY + 4 * DAY)
    assert _times(store) == set(_DAYS)


def test_the_latest_boundary_is_stored_pending_while_its_block_can_be_replaced(store):
    node = FakeNode(head=block_at(_LAST) + 10)
    _run(node, store)
    finalities = [
        store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, DAY, time).finality for time in _DAYS
    ]
    assert finalities == [Finality.FINAL, Finality.FINAL, Finality.PENDING]


def test_a_store_taken_over_another_twap_window_is_not_added_to(store):
    node = FakeNode()
    _run(node, store, start=FIRST_DAY, end=FIRST_DAY)
    before = len(node.provider.requests)
    shorter = replace(_CONFIG, bars=BarSettings(twap_window_seconds=600))

    with pytest.raises(StoreError, match=r"WBTC/WETH-500|USDC/WETH-500") as refusal:
        _run(node, store, config=shorter)

    assert "a TWAP over [1800] seconds, and the config asks for 600" in str(refusal.value)
    assert _rows(store) == 2
    assert len(node.provider.requests) == before
    # The same window under another interval is another series, and is not in the way.
    hourly = replace(_CONFIG, bars=BarSettings(interval_seconds=3_600, twap_window_seconds=600))
    assert _run(node, store, config=hourly, start=FIRST_DAY, end=FIRST_DAY).written == 1


def test_a_connection_to_another_chain_than_the_configs_is_refused(store):
    node = FakeNode()
    rpc, _ = rpc_over(node.provider, chain_id=5)
    with pytest.raises(ValueError, match="chain 5 and the config is for 1"):
        backfill(rpc, store, _CONFIG, start=FIRST_DAY, end=_LAST)


# --- failures --------------------------------------------------------------


def test_a_revert_leaves_its_boundary_unwritten_and_the_run_goes_on(store):
    node, lines = FakeNode(), []
    close = block_at(FIRST_DAY + DAY) - 1
    node.reverts.add((_WBTC_WETH.address.lower(), close))

    summary = _run(node, store, lines=lines)

    assert (summary.written, summary.unanswered) == (2, (FIRST_DAY + DAY,))
    # Neither pool's reading is kept for the boundary one of them could not answer.
    assert _times(store) == _times(store, _WBTC_WETH) == {FIRST_DAY, _LAST}
    assert lines[1][0] == FIRST_DAY + DAY and lines[1][1].startswith("no answer (")

    # A later run asks again, and fills the boundary once the read answers.
    node.reverts.clear()
    again = _run(node, store)
    assert (again.written, again.already_stored, again.unanswered) == (1, 2, ())
    assert _times(store) == set(_DAYS)


def test_a_slot0_revert_ends_the_run_instead_of_leaving_a_gap(store):
    node = FakeNode()
    node.slot0_reverts.add((_USDC_WETH.address.lower(), block_at(FIRST_DAY + DAY) - 1))
    with pytest.raises(MalformedResponse, match="slot0 of .* reverted"):
        _run(node, store)
    assert _times(store) == {FIRST_DAY}


def test_a_node_error_ends_the_run_and_keeps_what_was_written_before_it(store):
    node = FakeNode()
    node.errors[block_at(FIRST_DAY + DAY) - 1] = {"code": -32000, "message": "internal error"}
    with pytest.raises(RpcRejected):
        _run(node, store)
    assert _times(store) == _times(store, _WBTC_WETH) == {FIRST_DAY}
    # The boundary after the failed one was not asked for.
    assert node.calls_at(block_at(_LAST) - 1) == 0


def test_an_answer_that_cannot_be_right_ends_the_run(store):
    node = FakeNode()
    node.slot0[(_WBTC_WETH.address.lower(), block_at(FIRST_DAY + DAY) - 1)] = (1, 0)
    with pytest.raises(MalformedResponse):
        _run(node, store)
    # The first pool's reading at the failed boundary was not written alone.
    assert _times(store) == _times(store, _WBTC_WETH) == {FIRST_DAY}


def test_a_rate_limit_that_outlasts_the_attempts_ends_the_run(store):
    node = FakeNode()
    node.errors[block_at(FIRST_DAY) - 1] = {"code": 429, "message": "too many requests"}
    with pytest.raises(RpcUnavailable):
        _run(node, store)
    assert _rows(store) == 0


# --- finality --------------------------------------------------------------


def test_a_later_run_confirms_the_pending_readings_that_are_final_by_then(store):
    node = FakeNode(head=block_at(_LAST) + 10)
    _run(node, store)
    node.head = block_at(_LAST) + 64

    summary = _run(node, store)

    assert (summary.confirmed, summary.reorged, summary.written) == (2, 0, 0)
    assert store.pending_bars(ETHEREUM_MAINNET) == []
    assert store.bar(ETHEREUM_MAINNET, _WBTC_WETH.address, DAY, _LAST).finality is Finality.FINAL


def test_a_pending_reading_is_left_alone_until_its_blocks_are_final(store):
    node = FakeNode(head=block_at(_LAST) + 10)
    _run(node, store)
    node.head = block_at(_LAST) + 63
    summary = _run(node, store)
    assert (summary.confirmed, summary.reorged) == (0, 0)
    assert len(store.pending_bars(ETHEREUM_MAINNET)) == 2


def test_a_reading_from_a_dropped_block_is_marked_and_kept(store):
    node = FakeNode(head=block_at(_LAST) + 10)
    _run(node, store)
    before = store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, DAY, _LAST)
    node.head = block_at(_LAST) + 64
    node.hashes[block_at(_LAST) - 1] = "0x" + "cd" * 32

    summary = _run(node, store)

    assert (summary.confirmed, summary.reorged, summary.written) == (0, 2, 0)
    after = store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, DAY, _LAST)
    assert after == replace(before, finality=Finality.REORGED)


def test_pending_readings_are_confirmed_whatever_range_the_run_is_for(store):
    node = FakeNode(head=block_at(_LAST) + 10)
    _run(node, store)
    node.head = block_at(_LAST) + 64
    summary = _run(node, store, start=FIRST_DAY, end=FIRST_DAY)
    assert (summary.confirmed, summary.already_stored) == (2, 1)
