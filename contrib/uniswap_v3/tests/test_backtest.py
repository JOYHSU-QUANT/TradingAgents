"""A backtest: the store's bars replayed through the engine, offline and repeatable."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.uniswap_v3.config import ConfigError, StrategySpec
from contrib.uniswap_v3.domain.bars import Finality
from contrib.uniswap_v3.domain.records import BarSeen, FillSource, Outcome, SkipCode
from contrib.uniswap_v3.domain.types import Rejection, RunMode
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.engine import step as step_module
from contrib.uniswap_v3.engine.backtest import BacktestRangeError, run_backtest
from contrib.uniswap_v3.engine.executors import ModelExecutor
from contrib.uniswap_v3.engine.step import EngineError, start_run
from contrib.uniswap_v3.store.repository import Store, open_store
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    USDC_WETH,
    WBTC_WETH,
    ScriptedStrategy,
    config as _config,
    ledger as _ledger,
)
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK,
    DEFAULT_TICK,
    UP_HALF_TICK as _UP_HALF,
    block_at,
    block_hash,
    pool_bar,
    put_day as _put,
)
from contrib.uniswap_v3.tests.fakes.verdicts import (
    SOURCE,
    config as _verdict_config,
    record,
    verdict,
)

_CONFIG = _config()
_OPENING = _ledger()
_RUN = "bt"
_RUN_TABLES = ("runs", "decisions", "fills", "valuations")


def _day(day: int) -> int:
    return FIRST_DAY + day * DAY


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _put_days(store: Store, eth_ticks: list[int]) -> None:
    for day, eth_tick in enumerate(eth_ticks):
        _put(store, day, eth_tick=eth_tick)


def _backtest(store: Store, *, opening=_OPENING, run_id: str = _RUN, config=_CONFIG, **range_):
    return run_backtest(
        store,
        config,
        run_id=run_id,
        start=range_.pop("start", FIRST_DAY),
        opening=opening,
        now=FIRST_DAY,
        **range_,
    )


def _rows(path: Path, *, until: int | None = None) -> dict[str, list[tuple]]:
    """Every row a run wrote, or, with ``until``, those of the bars up to that boundary."""
    connection = sqlite3.connect(path)
    try:
        return {
            table: [
                row
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
                if until is None or table == "runs" or row[1] <= until
            ]
            for table in _RUN_TABLES
        }
    finally:
        connection.close()


def _recorder(monkeypatch, script=None) -> ScriptedStrategy:
    """Have the engine build this strategy, which keeps every view it is handed."""
    strategy = ScriptedStrategy(script or {})
    monkeypatch.setattr(step_module, "build_strategy", lambda name, params: strategy)
    return strategy


# --- the replay ------------------------------------------------------------


def test_every_stored_bar_is_decided_once_and_a_second_run_decides_nothing(store, tmp_path):
    # All in USDC to start with, then a rise by half and a fall back: three rebalances.
    _put_days(store, [DEFAULT_TICK, DEFAULT_TICK, _UP_HALF, DEFAULT_TICK])

    summary = _backtest(store)

    assert (summary.start, summary.end, summary.boundaries) == (_day(0), _day(3), 4)
    assert (summary.decided, summary.already_decided, summary.missing) == (4, 0, ())
    assert summary.outcomes == {Outcome.FILLED: 3, Outcome.HOLD: 1}
    assert [decision.outcome for decision in store.decisions(_RUN)] == [
        Outcome.FILLED,
        Outcome.HOLD,
        Outcome.FILLED,
        Outcome.FILLED,
    ]
    assert store.run(_RUN).mode is RunMode.BACKTEST
    # The fills are the model's, dated the configured delay after the boundary's first block.
    assert {fill.block for fill in store.fills(_RUN, _day(0))} == {block_at(_day(0)) + 25}
    before = _rows(tmp_path / "store.db")

    again = _backtest(store)

    assert (again.decided, again.already_decided) == (0, 4)
    assert again.outcomes == summary.outcomes
    assert _rows(tmp_path / "store.db") == before


def test_two_backtests_of_the_same_bars_write_the_same_rows(tmp_path):
    ticks = [DEFAULT_TICK, _UP_HALF, DEFAULT_TICK - 900, DEFAULT_TICK + 700, _UP_HALF]
    for name in ("one.db", "two.db"):
        with open_store(tmp_path / name) as store:
            _put_days(store, ticks)
            _backtest(store)
    one = _rows(tmp_path / "one.db")
    assert one == _rows(tmp_path / "two.db")
    assert len(one["decisions"]) == 5 and one["fills"]


def test_a_bar_changed_later_changes_no_decision_before_it(tmp_path):
    shared = [DEFAULT_TICK, _UP_HALF, DEFAULT_TICK]
    for name, later in (("one.db", [DEFAULT_TICK, DEFAULT_TICK]), ("two.db", [_UP_HALF, 190_000])):
        with open_store(tmp_path / name) as store:
            _put_days(store, shared + later)
            _backtest(store)
    one, two = tmp_path / "one.db", tmp_path / "two.db"
    assert _rows(one, until=_day(2)) == _rows(two, until=_day(2))
    assert len(_rows(one, until=_day(2))["decisions"]) == 3
    # The later bars did differ, and so did what was decided on them.
    assert _rows(one)["valuations"] != _rows(two)["valuations"]


def test_the_strategy_sees_every_stored_bar_up_to_the_one_decided_and_none_after(
    store, monkeypatch
):
    strategy = _recorder(monkeypatch)
    _put_days(store, [DEFAULT_TICK] * 5)

    summary = _backtest(store, start=_day(2), end=_day(3))

    assert summary.decided == 2
    # The bars from before the range are in the view, and the last is the one decided.
    assert [[bar.time for bar in view.bars] for view, _ in strategy.calls] == [
        [_day(0), _day(1), _day(2)],
        [_day(0), _day(1), _day(2), _day(3)],
    ]
    assert store.decision(_RUN, _day(1)) is None and store.decision(_RUN, _day(4)) is None


def test_a_boundary_without_a_bar_is_not_decided_and_is_counted(store, monkeypatch):
    strategy = _recorder(monkeypatch)
    for day in (0, 1, 3):
        _put(store, day)
    # One pool's reading does not make a bar.
    _put(store, 2, pools=(USDC_WETH,))

    summary = _backtest(store)

    assert (summary.decided, summary.missing, summary.boundaries) == (3, (_day(2),), 4)
    assert store.decision(_RUN, _day(2)) is None
    assert [bar.time for bar in strategy.calls[-1][0].bars] == [_day(0), _day(1), _day(3)]


def test_a_suspect_bar_is_skipped_with_what_the_store_says_of_it(store, monkeypatch):
    strategy = _recorder(monkeypatch)
    _put(store, 0)
    _put(store, 1, twap_tick=BTC_TICK + 600)
    _put(store, 2)

    summary = _backtest(store)

    assert summary.outcomes == {Outcome.HOLD: 2, Outcome.SKIPPED_SUSPECT: 1}
    skipped = store.decision(_RUN, _day(1))
    assert skipped.reason_code is SkipCode.TWAP_DEVIATION
    assert skipped.reason == (
        "WBTC/WETH-500: its close price is further from its TWAP than the limit"
    )
    close = block_at(_day(1)) - 1
    assert skipped.seen == BarSeen(
        close_block=close, close_block_hash=block_hash(close), finality=Finality.FINAL
    )
    # The strategy was not asked about the suspect bar, and sees it in the next view.
    assert [view.latest.time for view, _ in strategy.calls] == [_day(0), _day(2)]
    assert [bar.suspect for bar in strategy.calls[-1][0].bars] == [False, True, False]


def test_a_run_does_not_go_back_to_a_boundary_given_its_bar_later(store):
    for day in (0, 1, 3):
        _put(store, day)
    assert _backtest(store).missing == (_day(2),)
    _put(store, 2)

    with pytest.raises(EngineError, match="an earlier bar needs a new run"):
        _backtest(store)
    assert store.decision(_RUN, _day(2)) is None
    # The range is replayed as a new run, which sees the bar.
    assert _backtest(store, run_id="again").decided == 4


def test_stored_bars_that_do_not_make_a_view_stop_the_backtest(store):
    _put(store, 0)
    # The next day's readings close on an earlier block than the first day's.
    late = {"time": _day(1), "close_block": 5, "close_block_hash": block_hash(5)}
    store.insert_bars([pool_bar(USDC_WETH, **late), pool_bar(WBTC_WETH, **late)])
    with pytest.raises(EngineError, match=f"the stored bars up to {_day(1)} do not make a view"):
        _backtest(store)
    assert store.last_decided(_RUN) == _day(0)


# --- the range -------------------------------------------------------------


def test_the_range_ends_at_the_stores_latest_bar_unless_it_is_told_where(store):
    _put_days(store, [DEFAULT_TICK] * 4)
    # Cut at the last boundary at or before the end.
    first = _backtest(store, end=_day(1) + 100)
    assert (first.end, first.decided) == (_day(1), 2)
    # Past the store's latest bar, the boundaries asked for have no bar.
    past = _backtest(store, end=_day(5))
    assert (past.end, past.decided, past.already_decided) == (_day(5), 2, 2)
    assert past.missing == (_day(4), _day(5))
    latest = _backtest(store, run_id="another")
    assert (latest.end, latest.decided, latest.missing) == (_day(3), 4, ())


@pytest.mark.parametrize(
    ("start", "end", "match"),
    [
        (FIRST_DAY + 1, None, "must start on a bar boundary"),
        (FIRST_DAY, FIRST_DAY - DAY, "before it starts"),
        (FIRST_DAY + 10 * DAY, None, "the store holds no bar from .* to the store's latest bar"),
        (FIRST_DAY + 10 * DAY, FIRST_DAY + 12 * DAY, "the store holds no bar from"),
    ],
)
def test_a_range_that_cannot_be_replayed_is_refused_and_starts_no_run(store, start, end, match):
    _put_days(store, [DEFAULT_TICK] * 2)
    with pytest.raises(BacktestRangeError, match=match):
        _backtest(store, start=start, end=end)
    assert store.run(_RUN) is None


def test_a_store_without_a_pools_readings_holds_no_bar(store):
    _put(store, 0, pools=(USDC_WETH,))
    with pytest.raises(BacktestRangeError, match="the store holds no bar"):
        _backtest(store)


# --- the run ---------------------------------------------------------------


def test_a_run_is_carried_on_from_where_it_stopped(store):
    _put_days(store, [DEFAULT_TICK, _UP_HALF, DEFAULT_TICK, _UP_HALF])
    _backtest(store, end=_day(1))

    # The opening balances need not be given again, and the same ones may be.
    carried = _backtest(store, opening=None)
    assert (carried.decided, carried.already_decided) == (2, 2)
    assert _backtest(store).already_decided == 4

    with open_store(Path(":memory:")) as whole:
        _put_days(whole, [DEFAULT_TICK, _UP_HALF, DEFAULT_TICK, _UP_HALF])
        _backtest(whole)
        assert whole.ledger(_RUN) == store.ledger(_RUN)


def test_a_new_run_needs_opening_balances_and_a_stored_one_keeps_its_own(store):
    _put_days(store, [DEFAULT_TICK] * 2)
    with pytest.raises(EngineError, match="a new run needs opening balances"):
        _backtest(store, opening=None)
    assert store.run(_RUN) is None

    _backtest(store)
    with pytest.raises(EngineError, match="was started with other opening balances"):
        _backtest(store, opening=_ledger("500"))
    changed = replace(_CONFIG, strategy=StrategySpec("fixed_weights", {"band": "0.1"}))
    with pytest.raises(EngineError, match="was started under another config"):
        _backtest(store, config=changed)


def test_a_backtest_does_not_carry_on_a_run_of_another_mode(store):
    _put_days(store, [DEFAULT_TICK] * 2)
    start_run(
        store,
        _CONFIG,
        run_id="paper",
        mode=RunMode.PAPER,
        ledger=_ledger(),
        created_at=FIRST_DAY,
        fills=FillSource.QUOTER,
    )
    with pytest.raises(EngineError, match="is a paper run, and is not carried on as a backtest"):
        _backtest(store, run_id="paper")
    assert store.last_decided("paper") is None


def test_a_range_that_would_leave_stored_bars_undecided_behind_it_is_refused(store):
    _put_days(store, [DEFAULT_TICK] * 5)
    _backtest(store, end=_day(1))
    with pytest.raises(
        EngineError,
        match=f"the 2 stored bar\\(s\\) between them, the first at {_day(2)}, would be left",
    ):
        _backtest(store, opening=None, start=_day(4))
    assert store.last_decided(_RUN) == _day(1)
    # From the bar after the latest decided one, or from any earlier, the run is carried on.
    assert _backtest(store, opening=None, start=_day(2), end=_day(2)).decided == 1
    assert _backtest(store, opening=None, start=_day(1)).decided == 2


def test_the_summary_counts_readings_not_final_and_ones_that_changed_since(store):
    _put(store, 0)
    _put(store, 1, finality=Finality.PENDING)
    _put(store, 2)

    summary = _backtest(store)

    assert (summary.on_pending, summary.changed) == (1, ())
    assert store.decision(_RUN, _day(1)).seen.finality is Finality.PENDING

    store.set_finality(store.pending_bars(1), Finality.REORGED)
    again = _backtest(store)
    assert (again.decided, again.on_pending, again.changed) == (0, 0, (_day(1),))
    # The decision stands, and a new run skips the bar.
    assert store.decision(_RUN, _day(1)).outcome is Outcome.HOLD
    assert _backtest(store, run_id="again").outcomes[Outcome.SKIPPED_SUSPECT] == 1


def test_a_reading_that_became_final_as_it_was_is_not_one_that_changed(store):
    _put(store, 0, finality=Finality.PENDING)
    assert _backtest(store).on_pending == 1
    # Decided already, so a rerun counts no bar as decided on a pending reading.
    still_pending = _backtest(store)
    assert (still_pending.on_pending, still_pending.changed) == (0, ())
    store.set_finality(store.pending_bars(1), Finality.FINAL)
    assert _backtest(store).changed == ()


def test_a_bar_that_now_closes_on_another_block_is_one_that_changed(store, tmp_path):
    _put_days(store, [DEFAULT_TICK] * 2)
    _backtest(store)
    # The same block number under another hash, in both pools: not suspect, and not the same.
    connection = sqlite3.connect(tmp_path / "store.db")
    connection.execute(
        "UPDATE bars SET close_block_hash = ? WHERE time = ?", ("0x" + "ee" * 32, _day(1))
    )
    connection.commit()
    connection.close()
    again = _backtest(store)
    assert (again.already_decided, again.changed) == (2, (_day(1),))


def test_a_bar_skipped_as_suspect_that_no_longer_is_counts_as_changed(store, tmp_path):
    _put(store, 0, twap_tick=BTC_TICK + 600, finality=Finality.PENDING)
    summary = _backtest(store)
    # A suspect bar is skipped, whatever its finality, and is not one decided on a pending reading.
    assert (summary.outcomes, summary.on_pending) == ({Outcome.SKIPPED_SUSPECT: 1}, 0)
    connection = sqlite3.connect(tmp_path / "store.db")
    connection.execute("UPDATE bars SET twap_tick = tick")
    connection.commit()
    connection.close()
    assert _backtest(store).changed == (_day(0),)


def test_a_rebalance_the_executor_refused_is_not_one_rejected_for_gas(store, monkeypatch):
    monkeypatch.setattr(
        ModelExecutor, "execute", lambda self, swap, bar: Rejection(swap, "the pool is closed")
    )
    _put_days(store, [DEFAULT_TICK])
    summary = _backtest(store)
    assert summary.outcomes == {Outcome.REJECTED: 1}
    assert summary.gas_rejected == ()


def test_the_summary_names_the_rebalances_rejected_for_want_of_gas(store):
    _put_days(store, [DEFAULT_TICK, DEFAULT_TICK, _UP_HALF])
    summary = _backtest(store, opening=_ledger(gas="0.004"))
    assert summary.outcomes == {Outcome.FILLED: 1, Outcome.HOLD: 1, Outcome.REJECTED: 1}
    assert summary.gas_rejected == (_day(2),)
    assert _backtest(store, opening=None).gas_rejected == (_day(2),)


def test_a_config_whose_strategy_cannot_be_built_starts_no_run(store):
    _put_days(store, [DEFAULT_TICK] * 2)
    unbuildable = replace(_CONFIG, strategy=StrategySpec("fixed_weights", {"band": "wide"}))
    with pytest.raises(ConfigError, match="strategy: "):
        _backtest(store, config=unbuildable)
    assert store.run(_RUN) is None


def test_a_strategy_that_raises_stops_the_backtest_and_keeps_what_was_decided(store, monkeypatch):
    _put_days(store, [DEFAULT_TICK] * 4)
    _recorder(monkeypatch, {_day(1): RuntimeError("no answer")})
    with pytest.raises(RuntimeError, match="no answer"):
        _backtest(store)
    assert store.last_decided(_RUN) == _day(0)

    _recorder(monkeypatch)
    carried = _backtest(store)
    assert (carried.decided, carried.already_decided) == (3, 1)


# --- verdicts ----------------------------------------------------------------


def test_the_strategy_sees_the_verdicts_up_to_the_bar_decided_and_none_after(store, monkeypatch):
    strategy = _recorder(monkeypatch)
    _put_days(store, [DEFAULT_TICK] * 4)
    for said in (
        record("WETH", 0),
        record("WBTC", 0, Rating.HOLD),
        record("WETH", 2, Rating.OVERWEIGHT),
        record("WETH", 3, Rating.SELL),
        # Another source's, and one on a token the config does not trade: not carried.
        record("WETH", 1, source="another-judge"),
        record("LINK", 1),
    ):
        store.insert_verdict(said)

    summary = _backtest(store, config=_verdict_config(), end=_day(2))

    assert summary.decided == 3
    views = [view for view, _ in strategy.calls]
    assert [sorted(view.verdicts) for view in views] == [[_day(0)], [_day(0)], [_day(0), _day(2)]]
    assert views[0].latest_verdicts == {
        "WETH": verdict("WETH", 0),
        "WBTC": verdict("WBTC", 0, Rating.HOLD),
    }
    assert views[1].latest_verdicts == {}
    assert views[2].latest_verdicts == {"WETH": verdict("WETH", 2, Rating.OVERWEIGHT)}
    assert {view.verdict_source for view in views} == {SOURCE}
    # Each decision kept what its view carried at its bar: the digests, or that there were none.
    assert [decision.verdicts for decision in store.decisions(_RUN)] == [
        {"WETH": verdict("WETH", 0).digest, "WBTC": verdict("WBTC", 0, Rating.HOLD).digest},
        {},
        {"WETH": verdict("WETH", 2, Rating.OVERWEIGHT).digest},
    ]


def test_a_run_that_reads_no_verdicts_is_handed_none_and_keeps_none(store, monkeypatch):
    strategy = _recorder(monkeypatch)
    _put_days(store, [DEFAULT_TICK] * 2)
    store.insert_verdict(record("WETH", 0))

    _backtest(store)

    assert [dict(view.verdicts) for view, _ in strategy.calls] == [{}, {}]
    assert {view.verdict_source for view, _ in strategy.calls} == {None}
    assert [decision.verdicts for decision in store.decisions(_RUN)] == [None, None]


def test_a_verdict_recorded_after_its_bar_was_decided_is_seen_later_and_changes_no_decision(
    store, monkeypatch
):
    strategy = _recorder(monkeypatch)
    _put_days(store, [DEFAULT_TICK] * 2)
    first = _backtest(store, config=_verdict_config(), end=_day(0))
    store.insert_verdict(record("WETH", 0))

    again = _backtest(store, config=_verdict_config())

    assert (first.decided, again.decided, again.already_decided) == (1, 1, 1)
    # The late verdict is in the next bar's view, as history; the decided bar keeps that it saw none.
    assert sorted(strategy.calls[-1][0].verdicts) == [_day(0)]
    assert [decision.verdicts for decision in store.decisions(_RUN)] == [{}, {}]
