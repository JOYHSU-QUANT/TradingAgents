"""The store's run tables: runs, and a decision per bar with its fills and valuation."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.bars import Finality
from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import (
    BarSeen,
    Decision,
    FillRecord,
    Outcome,
    RejectionCode,
    RunRecord,
    StepRecord,
    Valuation,
)
from contrib.uniswap_v3.domain.types import Fill, RunMode, SwapIntent
from contrib.uniswap_v3.ports import Journal
from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.store.schema import _MIGRATIONS, APPLICATION_ID
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    PRICES,
    USDC,
    USDC_WETH,
    WBTC_WETH,
    ledger as _ledger,
    weights,
)
from contrib.uniswap_v3.tests.fakes.node import pool_bar

D = Decimal
_HASH = "0x" + "ab" * 32


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _run(run_id: str = "run-1", **changes) -> RunRecord:
    fields = {
        "run_id": run_id,
        "mode": RunMode.BACKTEST,
        "chain_id": 1,
        "quote": "USDC",
        "strategy": "fixed_weights",
        "config": '{"chain_id":1}',
        "ledger": _ledger(),
        "created_at": FIRST_DAY,
    }
    return RunRecord(**{**fields, **changes})


def _tamper(path, *statements: str) -> None:
    """Change the database behind the store's back."""
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _held(time: int = FIRST_DAY, ledger: Ledger | None = None) -> StepRecord:
    ledger = ledger or _ledger()
    return StepRecord(
        decision=Decision(time=time, outcome=Outcome.HOLD, close_block=999),
        valuation=Valuation(
            time=time,
            ledger=ledger,
            prices=PRICES,
            total_value=ledger.portfolio("USDC", PRICES).total_value,
        ),
    )


def _filled(time: int = FIRST_DAY) -> StepRecord:
    after = _ledger("5000", "1.49775075", "0.04990006", gas="0.997")
    return StepRecord(
        decision=Decision(
            time=time,
            outcome=Outcome.FILLED,
            close_block=999,
            target=weights("0.5", "0.3", "0.2"),
            seen=BarSeen(close_block=999, close_block_hash=_HASH, finality=Finality.PENDING),
        ),
        valuation=Valuation(time=time, ledger=after, prices=PRICES, total_value=D("9991.5039")),
        fills=(
            Fill(
                SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49175375")),
                D("1.49775075"),
                D("0.001"),
                1_026,
            ),
            Fill(
                SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("2000"), D("0.04970026")),
                D("0.04990006"),
                D("0.002"),
                1_026,
            ),
        ),
    )


def test_the_store_is_the_engines_journal(store):
    assert isinstance(store, Journal)


def test_a_run_comes_back_as_it_was_stored(store):
    run = _run(mode=RunMode.PAPER, ledger=_ledger("1234.567891", weth="0.000000000000000001"))
    store.insert_run(run)
    assert store.run("run-1") == run
    assert store.run("run-2") is None


def test_a_run_id_is_stored_once(store):
    store.insert_run(_run())
    with pytest.raises(StoreError, match="the run 'run-1' is already stored"):
        store.insert_run(_run(mode=RunMode.PAPER))
    assert store.run("run-1").mode is RunMode.BACKTEST


def test_before_any_decision_a_runs_ledger_is_its_opening_balances(store):
    store.insert_run(_run())
    assert store.ledger("run-1") == _ledger()
    assert store.last_decided("run-1") is None
    assert store.decision("run-1", FIRST_DAY) is None
    assert store.valuation("run-1", FIRST_DAY) is None
    assert store.fills("run-1") == []


def test_a_step_comes_back_as_it_was_recorded(store):
    store.insert_run(_run())
    step = _filled()
    store.record("run-1", step)
    assert store.decision("run-1", FIRST_DAY) == step.decision
    assert store.valuation("run-1", FIRST_DAY) == step.valuation
    assert store.ledger("run-1") == step.valuation.ledger
    assert store.last_decided("run-1") == FIRST_DAY
    assert store.fills("run-1", FIRST_DAY) == [
        FillRecord(
            time=FIRST_DAY,
            leg=0,
            token_in="USDC",
            token_out="WETH",
            route=(USDC_WETH.address,),
            amount_in=D("3000"),
            min_amount_out=D("1.49175375"),
            amount_out=D("1.49775075"),
            gas_cost_eth=D("0.001"),
            block=1_026,
        ),
        FillRecord(
            time=FIRST_DAY,
            leg=1,
            token_in="USDC",
            token_out="WBTC",
            route=(USDC_WETH.address, WBTC_WETH.address),
            amount_in=D("2000"),
            min_amount_out=D("0.04970026"),
            amount_out=D("0.04990006"),
            gas_cost_eth=D("0.002"),
            block=1_026,
        ),
    ]
    assert store.fills("run-1", FIRST_DAY + DAY) == []


def test_a_rejected_decision_keeps_its_reason_and_a_skipped_one_its_flag(store):
    store.insert_run(_run())
    ledger = _ledger()
    value = Valuation(time=FIRST_DAY, ledger=ledger, prices=PRICES, total_value=D("10000"))
    rejected = Decision(
        time=FIRST_DAY,
        outcome=Outcome.REJECTED,
        close_block=999,
        target=weights("0.5", "0.3", "0.2"),
        reason="leg 0 (USDC to WETH) was refused: closed",
        reason_code=RejectionCode.EXECUTOR,
    )
    skipped = Decision(time=FIRST_DAY + DAY, outcome=Outcome.SKIPPED_SUSPECT, close_block=8_199)
    store.record("run-1", StepRecord(decision=rejected, valuation=value))
    store.record(
        "run-1",
        StepRecord(
            decision=skipped,
            valuation=Valuation(
                time=FIRST_DAY + DAY, ledger=ledger, prices=PRICES, total_value=D("10000")
            ),
        ),
    )
    assert store.decision("run-1", FIRST_DAY) == rejected
    assert store.decision("run-1", FIRST_DAY + DAY) == skipped


def test_a_bar_is_decided_once_and_a_second_record_writes_nothing(store):
    store.insert_run(_run())
    store.record("run-1", _held())
    with pytest.raises(StoreError, match="has already decided the bar at"):
        store.record("run-1", _filled())
    # Not the decision, not its fills, not its valuation.
    assert store.decision("run-1", FIRST_DAY).outcome is Outcome.HOLD
    assert store.fills("run-1") == []
    assert store.ledger("run-1") == _ledger()


def test_runs_do_not_see_each_others_decisions(store):
    store.insert_run(_run("run-1"))
    store.insert_run(_run("run-2"))
    store.record("run-1", _filled())
    store.record("run-2", _held())
    assert store.decision("run-2", FIRST_DAY).outcome is Outcome.HOLD
    assert store.fills("run-2") == []
    assert store.ledger("run-2") == _ledger()
    assert len(store.fills("run-1")) == 2


def test_a_run_that_is_not_stored_has_no_ledger_and_takes_no_record(store):
    with pytest.raises(StoreError, match="there is no run 'run-9'"):
        store.ledger("run-9")
    with pytest.raises(StoreError, match="there is no run 'run-9'"):
        store.record("run-9", _held())
    assert store.decision("run-9", FIRST_DAY) is None


def test_a_step_on_an_earlier_bar_than_the_latest_decided_is_refused(store):
    store.insert_run(_run())
    store.record("run-1", _held(FIRST_DAY + DAY))
    with pytest.raises(StoreError, match=f"has already decided the later bar at {FIRST_DAY + DAY}"):
        store.record("run-1", _held(FIRST_DAY))
    assert store.decision("run-1", FIRST_DAY) is None
    assert store.last_decided("run-1") == FIRST_DAY + DAY


def test_a_step_whose_ledger_does_not_follow_from_the_runs_is_refused(store):
    # What a second writer on the run would send: balances worked from a ledger that has moved on.
    store.insert_run(_run())
    store.record("run-1", _filled())
    with pytest.raises(StoreError, match="is not its ledger before with the step's fills applied"):
        store.record("run-1", _held(FIRST_DAY + DAY, _ledger()))
    stale = _filled(FIRST_DAY + DAY)
    with pytest.raises(StoreError, match="is not its ledger before"):
        store.record("run-1", stale)
    assert store.last_decided("run-1") == FIRST_DAY
    assert len(store.fills("run-1")) == 2


def test_fills_the_runs_ledger_does_not_cover_are_refused(store):
    store.insert_run(_run(ledger=_ledger("100")))
    with pytest.raises(StoreError, match="do not apply to its ledger .*holds 100 USDC"):
        store.record("run-1", _filled())
    assert store.decision("run-1", FIRST_DAY) is None


@pytest.mark.parametrize("mode", list(RunMode))
def test_the_schema_takes_every_run_mode(store, mode):
    store.insert_run(_run(mode=mode))
    assert store.run("run-1").mode is mode


@pytest.mark.parametrize("finality", list(Finality))
def test_the_schema_takes_every_finality_a_decision_can_keep(store, finality):
    store.insert_run(_run())
    held = _held()
    seen = BarSeen(close_block=999, close_block_hash=_HASH, finality=finality)
    store.record("run-1", replace(held, decision=replace(held.decision, seen=seen)))
    assert store.decision("run-1", FIRST_DAY).seen == seen


def test_an_outcome_or_a_reason_code_this_code_does_not_know_is_refused_when_read(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
        store.record("run-1", _held())
    _tamper(path, "UPDATE decisions SET outcome = 'partial'")
    with open_store(path) as store, pytest.raises(StoreError, match="'partial' is not a valid"):
        store.decision("run-1", FIRST_DAY)
    _tamper(
        path,
        "UPDATE decisions SET outcome = 'rejected', target = '{\"USDC\": \"1\"}', "
        "reason = 'why', reason_code = 'weather'",
    )
    with open_store(path) as store, pytest.raises(StoreError, match="'weather' is not a valid"):
        store.decision("run-1", FIRST_DAY)


def test_a_step_that_fails_part_way_leaves_nothing_behind(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
    # The decision and the fills go in; the valuation, last, has nowhere to go.
    _tamper(path, "DROP TABLE valuations")
    with open_store(path) as store:
        with pytest.raises(StoreError, match="the store failed while recording a decision"):
            store.record("run-1", _filled())
        assert store.decision("run-1", FIRST_DAY) is None
        assert store.fills("run-1") == []


def test_the_schema_refuses_a_hash_without_a_finality(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
        store.record("run-1", _held())
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _tamper(path, f"UPDATE decisions SET close_block_hash = '{_HASH}'")


def test_a_stored_row_that_no_longer_reads_is_a_store_error(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
        store.record("run-1", _held())
    connection = sqlite3.connect(path)
    connection.execute("UPDATE valuations SET total_value = 'much'")
    connection.execute("UPDATE valuations SET prices = '{\"WETH\": 2000, \"WBTC\": 40000}'")
    connection.execute("UPDATE runs SET balances = '{\"USDC\": \"-1\"}'")
    connection.commit()
    connection.close()
    with open_store(path) as store:
        with pytest.raises(StoreError, match="the stored valuation of run 'run-1'"):
            store.valuation("run-1", FIRST_DAY)
        with pytest.raises(StoreError, match="the stored run 'run-1' is not valid"):
            store.run("run-1")


def test_a_decision_whose_valuation_is_gone_leaves_the_run_without_a_ledger(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
        store.record("run-1", _held())
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM valuations")
    connection.commit()
    connection.close()
    # Not the opening balances: the run has moved on from them.
    with (
        open_store(path) as store,
        pytest.raises(StoreError, match=f"at {FIRST_DAY} has no valuation"),
    ):
        store.ledger("run-1")


def test_a_store_from_before_the_run_tables_gains_them_and_keeps_its_bars(tmp_path):
    path = tmp_path / "store.db"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    for statement in _MIGRATIONS[0]:
        connection.execute(statement)
    connection.execute("INSERT INTO schema_migrations VALUES (1, 0)")
    connection.close()
    with open_store(path) as store:
        store.insert_bars([pool_bar()])
        store.insert_run(_run())
        store.record("run-1", _filled())
        assert store.bar(1, USDC_WETH.address, DAY, FIRST_DAY) == pool_bar()
        assert len(store.fills("run-1")) == 2
