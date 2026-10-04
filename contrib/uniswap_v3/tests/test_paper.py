"""A paper run's visits: bars read off a scripted chain, decided once, and filled from its quotes."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.chain.errors import BlockNotFound, RpcRejected
from contrib.uniswap_v3.chain.gas import ChainGasOracle
from contrib.uniswap_v3.chain.quoter import ChainQuoter
from contrib.uniswap_v3.domain.bars import BarSettings, Finality
from contrib.uniswap_v3.domain.records import FillSource, Outcome, RejectionCode
from contrib.uniswap_v3.domain.types import RunMode
from contrib.uniswap_v3.engine.backtest import run_backtest
from contrib.uniswap_v3.engine.executors import ModelExecutor, QuoteExecutor
from contrib.uniswap_v3.engine.step import EngineError
from contrib.uniswap_v3.paper import run_paper
from contrib.uniswap_v3.store.repository import Store, open_store
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    USDC_WETH,
    WBTC_WETH,
    config as _config,
    ledger as _ledger,
)
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK,
    DEFAULT_TICK,
    UP_HALF_TICK,
    FakeNode,
    block_at,
    sqrt_price_at,
)
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

_CONFIG = _config()
_OPENING = _ledger()
_RUN = "paper"
_ETH_POOL = USDC_WETH.address.lower()
_BTC_POOL = WBTC_WETH.address.lower()


def _day(day: int) -> int:
    return FIRST_DAY + day * DAY


def _fill_block(day: int) -> int:
    """The first block of the day's boundary, and the config's 25 blocks after it."""
    return block_at(_day(day)) + 25


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


@pytest.fixture
def node():
    """A chain whose WBTC/WETH pool is near 15 WETH per WBTC at every block."""
    chain = FakeNode()
    chain.pool_slot0[_BTC_POOL] = (sqrt_price_at(BTC_TICK), BTC_TICK)
    chain.pool_twap_tick[_BTC_POOL] = BTC_TICK
    return chain


def _executor(node: FakeNode):
    rpc, _ = rpc_over(node.provider, attempts=1)
    return rpc, QuoteExecutor(ChainQuoter(rpc), ChainGasOracle(rpc), _CONFIG.execution)


def _visit(node: FakeNode, store: Store, *, day: int, minutes: int = 10, opening=_OPENING):
    """A visit ``minutes`` after the day's boundary, the chain's head being where the clock is."""
    now = _day(day) + minutes * 60
    node.head = block_at(now)
    rpc, executor = _executor(node)
    return run_paper(rpc, store, _CONFIG, executor, run_id=_RUN, opening=opening, now=now)


def _weth_up_on(node: FakeNode, day: int) -> None:
    """Price WETH half as high again at the day's close block and at its fill block."""
    for block in (block_at(_day(day)) - 1, _fill_block(day)):
        node.slot0[(_ETH_POOL, block)] = (sqrt_price_at(UP_HALF_TICK), UP_HALF_TICK)
    node.twap_tick[(_ETH_POOL, block_at(_day(day)) - 1)] = UP_HALF_TICK


def test_a_visit_reads_the_latest_bar_and_decides_it_with_fills_quoted_at_its_fill_block(node, store):
    summary = _visit(node, store, day=0)

    assert summary.latest == _day(0)
    assert (summary.read.written, summary.read.already_stored) == (1, 0)
    assert (summary.replayed.decided, summary.replayed.already_decided) == (1, 0)
    assert summary.decision == store.decision(_RUN, _day(0))
    assert summary.decision.outcome is Outcome.FILLED
    # Minutes old, so not final; the decision keeps that.
    assert summary.decision.seen.finality is Finality.PENDING
    run = store.run(_RUN)
    assert (run.mode, run.fills, run.ledger) == (RunMode.PAPER, FillSource.QUOTER, _OPENING)
    fills = store.fills(_RUN)
    assert [(fill.token_in, fill.token_out) for fill in fills] == [("USDC", "WETH"), ("USDC", "WBTC")]
    assert {fill.block for fill in fills} == {_fill_block(0)}
    # One quote a swap, each at the fill block and nowhere else.
    assert node.calls_at(_fill_block(0)) == 2
    # The quoter's 100,000 gas a pool crossed and the 50,000 on top, at the block's 7 gwei.
    assert [fill.gas_cost_eth for fill in fills] == [Decimal("0.00105"), Decimal("0.00175")]


def test_a_second_visit_before_the_next_boundary_finds_the_bar_decided_and_writes_no_decision(
    node, store
):
    first = _visit(node, store, day=0)
    requests = len(node.provider.requests)

    second = _visit(node, store, day=0, minutes=40, opening=None)

    assert (second.replayed.decided, second.replayed.already_decided) == (0, 1)
    assert second.decision == first.decision
    assert (second.read.written, second.read.already_stored) == (0, 1)
    assert len(store.decisions(_RUN)) == 1 and len(store.fills(_RUN)) == 2
    # No quote is asked again. The two readings, final by now, are checked and marked so.
    assert node.calls_at(_fill_block(0)) == 2
    assert len(node.provider.requests) > requests
    assert (second.read.confirmed, second.read.reorged) == (2, 0)


def test_a_late_visit_fills_at_the_bars_block_and_not_at_the_heads(node, store):
    summary = _visit(node, store, day=0, minutes=600)
    assert summary.decision.outcome is Outcome.FILLED
    assert {fill.block for fill in store.fills(_RUN)} == {_fill_block(0)}
    assert node.head > _fill_block(0) + 2_000


def test_a_visit_before_the_fill_block_decides_nothing_and_a_later_one_decides(node, store):
    with pytest.raises(BlockNotFound, match=f"fills at block {_fill_block(0)}, and the node's chain"):
        _visit(node, store, day=0, minutes=4)
    # The bar is read and kept; the run is not started.
    assert store.run(_RUN) is None
    assert store.bar(1, USDC_WETH.address, DAY, _day(0)) is not None

    summary = _visit(node, store, day=0, minutes=5)
    assert node.head == _fill_block(0)
    assert summary.decision.outcome is Outcome.FILLED
    assert (summary.read.written, summary.read.already_stored) == (0, 1)


def test_a_visit_whose_node_has_not_reached_the_boundary_decides_nothing(node, store):
    node.head = block_at(_day(0)) - 5
    rpc, executor = _executor(node)
    with pytest.raises(BlockNotFound, match=f"has not reached the bar boundary at {_day(0)}"):
        run_paper(rpc, store, _CONFIG, executor, run_id=_RUN, opening=_OPENING, now=_day(0) + 600)
    assert store.run(_RUN) is None
    assert store.bar(1, USDC_WETH.address, DAY, _day(0)) is None


def test_a_visit_after_missed_boundaries_decides_each_bar_in_order_at_its_own_fill_block(node, store):
    _visit(node, store, day=0)
    _weth_up_on(node, 1)

    summary = _visit(node, store, day=3, opening=None)

    assert (summary.read.written, summary.replayed.decided) == (3, 3)
    assert (summary.replayed.start, summary.replayed.end) == (_day(1), _day(3))
    assert [decision.outcome for decision in store.decisions(_RUN)] == [
        Outcome.FILLED,
        Outcome.FILLED,
        Outcome.FILLED,
        Outcome.HOLD,
    ]
    assert {fill.block for fill in store.fills(_RUN, _day(1))} == {_fill_block(1)}
    assert {fill.block for fill in store.fills(_RUN, _day(2))} == {_fill_block(2)}
    # The first day's two readings are final by now, and were checked on the way.
    assert summary.read.confirmed == 2


def test_a_new_run_without_opening_balances_is_refused_before_the_chain_is_read(node, store):
    with pytest.raises(EngineError, match="a new run needs opening balances"):
        _visit(node, store, day=0, opening=None)
    assert node.provider.requests == []


def test_a_boundary_the_chain_has_no_answer_at_is_not_decided(node, store):
    node.reverts.add((_ETH_POOL, block_at(_day(0)) - 1))
    summary = _visit(node, store, day=0)
    assert (summary.decision, summary.replayed) == (None, None)
    assert summary.read.unanswered == (_day(0),)
    assert store.run(_RUN) is None


def test_a_quote_short_of_the_swaps_minimum_rejects_the_rebalance_and_the_visit_ends(node, store):
    # One percent short of the pools' prices, with half a percent allowed.
    node.quote_bps = 9_900
    summary = _visit(node, store, day=0)
    decision = summary.decision
    assert (decision.outcome, decision.reason_code) == (Outcome.REJECTED, RejectionCode.EXECUTOR)
    assert "is below the swap's minimum" in decision.reason
    assert store.fills(_RUN) == [] and store.ledger(_RUN) == _OPENING
    # The bar is decided: a visit later in the day does not ask again.
    node.quote_bps = 10_000
    assert _visit(node, store, day=0, minutes=60, opening=None).decision == decision


def test_a_quote_that_reverts_rejects_the_rebalance(node, store):
    node.quote_reverts.add(_fill_block(0))
    decision = _visit(node, store, day=0).decision
    assert (decision.outcome, decision.reason_code) == (Outcome.REJECTED, RejectionCode.EXECUTOR)
    assert f"the quote at block {_fill_block(0)} has no answer" in decision.reason


def test_a_node_error_on_a_quote_leaves_the_bar_undecided_for_the_next_visit(node, store):
    node.errors[_fill_block(0)] = {"code": -32000, "message": "the node is having a moment"}
    with pytest.raises(RpcRejected):
        _visit(node, store, day=0)
    assert store.last_decided(_RUN) is None

    del node.errors[_fill_block(0)]
    assert _visit(node, store, day=0, minutes=40).decision.outcome is Outcome.FILLED


def test_a_backtest_run_is_not_carried_on_as_a_paper_run(node, store):
    _visit(node, store, day=0)
    run_backtest(store, _CONFIG, run_id="bt", start=_day(0), opening=_OPENING, created_at=0)
    rpc, executor = _executor(node)
    requests = len(node.provider.requests)
    with pytest.raises(EngineError, match="is a backtest run, and is not carried on as a paper run"):
        run_paper(rpc, store, _CONFIG, executor, run_id="bt", now=_day(3) + 600)
    # Refused before the chain is read: the days since are not backfilled for nothing.
    assert len(node.provider.requests) == requests


def test_a_paper_run_is_not_carried_on_under_another_config_and_the_chain_is_not_read(node, store):
    _visit(node, store, day=0)
    rpc, executor = _executor(node)
    requests = len(node.provider.requests)
    hourly = replace(_CONFIG, bars=BarSettings(interval_seconds=3_600))
    with pytest.raises(EngineError, match="started under another config"):
        run_paper(rpc, store, hourly, executor, run_id=_RUN, now=_day(0) + 5_400)
    assert len(node.provider.requests) == requests


def test_a_run_keeps_to_the_source_of_its_fills(node, store):
    _visit(node, store, day=0)
    _, executor = _executor(node)
    run_backtest(store, _CONFIG, run_id="model", start=_day(0), opening=_OPENING, created_at=0)
    with pytest.raises(EngineError, match="takes its fills from the model, and is not carried on"):
        run_backtest(store, _CONFIG, run_id="model", start=_day(0), created_at=0, executor=executor)
    run_backtest(
        store, _CONFIG, run_id="quoted", start=_day(0), opening=_OPENING, created_at=0, executor=executor
    )
    with pytest.raises(EngineError, match="takes its fills from the quoter, and is not carried on"):
        run_backtest(store, _CONFIG, run_id="quoted", start=_day(0), created_at=0)


def test_a_quoted_backtest_over_the_same_bars_repeats_the_paper_run(node, store):
    _weth_up_on(node, 1)
    for day in range(3):
        _visit(node, store, day=day)
    _, executor = _executor(node)

    summary = run_backtest(
        store, _CONFIG, run_id="quoted", start=_day(0), opening=_OPENING, created_at=0, executor=executor
    )

    assert summary.decided == 3
    assert store.run("quoted").fills is FillSource.QUOTER
    paper, quoted = store.decisions(_RUN), store.decisions("quoted")
    assert [decision.outcome for decision in paper] == [Outcome.FILLED] * 3
    # The same decisions, but for the finality each saw: the paper run decided on fresh readings.
    assert [(d.time, d.outcome, d.target, d.close_block) for d in quoted] == [
        (d.time, d.outcome, d.target, d.close_block) for d in paper
    ]
    assert store.fills("quoted") == store.fills(_RUN)
    assert store.valuations("quoted") == store.valuations(_RUN)


def test_the_model_and_the_quoter_decide_alike_and_fill_differently(node, store):
    _weth_up_on(node, 1)
    for day in range(3):
        _visit(node, store, day=day)
    run_backtest(store, _CONFIG, run_id="model", start=_day(0), opening=_OPENING, created_at=0)

    paper, model = store.decisions(_RUN), store.decisions("model")
    assert [(d.time, d.outcome, d.target) for d in model] == [
        (d.time, d.outcome, d.target) for d in paper
    ]
    quoted_fills, modelled_fills = store.fills(_RUN), store.fills("model")
    assert [(f.time, f.token_in, f.token_out, f.block) for f in modelled_fills] == [
        (f.time, f.token_in, f.token_out, f.block) for f in quoted_fills
    ]
    # The model takes its slippage off every fill; the scripted pools take none.
    first_quoted, first_modelled = quoted_fills[0], modelled_fills[0]
    assert first_modelled.amount_in == first_quoted.amount_in
    assert first_modelled.amount_out < first_quoted.amount_out


def test_missed_bars_are_decided_even_when_the_latest_boundary_has_no_answer(node, store):
    _visit(node, store, day=0)
    node.reverts.add((_ETH_POOL, block_at(_day(2)) - 1))

    summary = _visit(node, store, day=2, opening=None)

    assert summary.decision is None
    assert summary.read.unanswered == (_day(2),)
    assert (summary.replayed.decided, summary.replayed.missing) == (1, (_day(2),))
    assert store.last_decided(_RUN) == _day(1)


def test_a_revert_without_a_reason_of_a_pools_leaves_the_bar_for_the_next_visit(node, store):
    node.quote_reverts.add(_fill_block(0))
    node.revert_message = "execution reverted: Unexpected error"
    with pytest.raises(RpcRejected, match="gives no reason of a pool's"):
        _visit(node, store, day=0)
    assert store.last_decided(_RUN) is None

    node.quote_reverts.clear()
    assert _visit(node, store, day=0, minutes=40).decision.outcome is Outcome.FILLED


def test_a_run_started_by_a_visit_that_decided_nothing_is_still_owed_that_bar(node, store):
    node.errors[_fill_block(0)] = {"code": -32000, "message": "the node is having a moment"}
    with pytest.raises(RpcRejected):
        _visit(node, store, day=0)
    assert store.run(_RUN) is not None and store.last_decided(_RUN) is None
    del node.errors[_fill_block(0)]

    # The retry comes after the next boundary has passed.
    summary = _visit(node, store, day=1)

    assert (summary.replayed.start, summary.replayed.decided) == (_day(0), 2)
    assert [decision.time for decision in store.decisions(_RUN)] == [_day(0), _day(1)]


def test_a_visit_whose_clock_is_behind_the_run_is_refused_and_reads_no_chain(node, store):
    _visit(node, store, day=0)
    _visit(node, store, day=1, opening=None)
    rpc, executor = _executor(node)
    requests = len(node.provider.requests)
    with pytest.raises(EngineError, match="the clock is behind"):
        run_paper(rpc, store, _CONFIG, executor, run_id=_RUN, now=_day(0) + 600)
    assert len(node.provider.requests) == requests
    # At the boundary the run has come to, a visit finds the bar decided.
    assert _visit(node, store, day=1, minutes=30, opening=None).replayed.already_decided == 1


def test_a_clock_behind_the_boundary_an_undecided_run_was_started_on_is_refused(node, store):
    node.errors[_fill_block(1)] = {"code": -32000, "message": "the node is having a moment"}
    with pytest.raises(RpcRejected):
        _visit(node, store, day=1)
    del node.errors[_fill_block(1)]
    rpc, executor = _executor(node)
    requests = len(node.provider.requests)
    with pytest.raises(EngineError, match=f"before the boundary at {_day(1)} the run"):
        run_paper(rpc, store, _CONFIG, executor, run_id=_RUN, now=_day(0) + 600)
    assert len(node.provider.requests) == requests and store.last_decided(_RUN) is None


def test_a_suspect_latest_bar_is_skipped_without_waiting_for_its_fill_block(node, store):
    # Its close is some 6% from its TWAP.
    node.twap_tick[(_ETH_POOL, block_at(_day(0)) - 1)] = DEFAULT_TICK + 600
    summary = _visit(node, store, day=0, minutes=1)
    assert node.head < _fill_block(0)
    assert summary.decision.outcome is Outcome.SKIPPED_SUSPECT
    assert summary.replayed.skipped == (_day(0),)
    assert node.calls_at(_fill_block(0)) == 0


def test_a_paper_run_is_not_started_with_an_executor_that_fills_from_the_model(node, store):
    rpc, _ = _executor(node)
    modelled = ModelExecutor("USDC", _CONFIG.execution)
    with pytest.raises(EngineError, match="a paper run fills from quotes"):
        run_paper(rpc, store, _CONFIG, modelled, run_id=_RUN, opening=_OPENING, now=_day(0) + 600)
    assert node.provider.requests == [] and store.run(_RUN) is None


def test_the_summary_names_the_bars_whose_rebalance_the_executor_rejected(node, store):
    node.quote_bps = 9_900
    summary = _visit(node, store, day=0)
    assert summary.replayed.executor_rejected == (_day(0),)
    assert summary.replayed.gas_rejected == () and summary.replayed.skipped == ()


def test_a_quoted_backtest_whose_node_has_not_reached_a_fill_block_stops_there(node, store):
    _visit(node, store, day=0)
    # The head falls back to between the bar's close and its fill block.
    node.head = _fill_block(0) - 1
    _, executor = _executor(node)
    with pytest.raises(BlockNotFound):
        run_backtest(
            store, _CONFIG, run_id="quoted", start=_day(0), opening=_OPENING, created_at=0,
            executor=executor,
        )  # fmt: skip
    assert store.last_decided("quoted") is None


def test_a_visit_that_finds_the_bar_decided_does_not_wait_on_a_node_short_of_its_fill_block(
    node, store
):
    _visit(node, store, day=0)
    # Another node, or the same one fallen back: its head is short of the bar's fill block.
    node.head = _fill_block(0) - 1
    rpc, executor = _executor(node)
    summary = run_paper(rpc, store, _CONFIG, executor, run_id=_RUN, now=_day(0) + 2_400)
    assert (summary.replayed.decided, summary.replayed.already_decided) == (0, 1)
