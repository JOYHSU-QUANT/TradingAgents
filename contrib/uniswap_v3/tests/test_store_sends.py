"""A signed run's sends in the store: begun before the first swap, a leg as each fills, settled by the decision."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.records import (
    Decision,
    FillRecord,
    FillSource,
    OpenSend,
    Outcome,
    RejectionCode,
    RunRecord,
    StepRecord,
    Valuation,
)
from contrib.uniswap_v3.domain.types import Fill, RunMode, SwapIntent
from contrib.uniswap_v3.store.repository import StoreError, open_store
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
from contrib.uniswap_v3.tests.fakes.node import store_at

D = Decimal
_RUN = "fork-1"
_LEG_0 = Fill(SwapIntent(USDC, (USDC_WETH,), D("3000"), D("1.49")), D("1.5"), D("0.001"), 1_027)
_LEG_1 = Fill(SwapIntent(USDC, (USDC_WETH, WBTC_WETH), D("2000"), D("0.049")), D("0.05"), D("0.002"), 1_029)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        opened.insert_run(
            RunRecord(
                run_id=_RUN,
                mode=RunMode.FORK,
                chain_id=1,
                quote="USDC",
                strategy="fixed_weights",
                config="{}",
                ledger=_ledger(),
                created_at=FIRST_DAY,
                fills=FillSource.CHAIN,
                fork_block=26_100_000,
            )
        )
        yield opened


def _step(*fills: Fill, outcome: Outcome = Outcome.FILLED, time: int = FIRST_DAY) -> StepRecord:
    after = _ledger().apply(fills)
    explained = outcome in (Outcome.PARTIAL, Outcome.REJECTED)
    return StepRecord(
        decision=Decision(
            time=time,
            outcome=outcome,
            close_block=1_000,
            target=weights("0.5", "0.3", "0.2"),
            reason="leg 1 was refused" if explained else None,
            reason_code=RejectionCode.EXECUTOR if explained else None,
        ),
        valuation=Valuation(
            time=time,
            ledger=after,
            prices=PRICES,
            total_value=after.portfolio("USDC", PRICES).total_value,
        ),
        fills=fills,
    )


def _record(fill: Fill, leg: int, time: int = FIRST_DAY) -> FillRecord:
    return FillRecord(
        time=time,
        leg=leg,
        token_in=fill.swap.token_in.symbol,
        token_out=fill.swap.token_out.symbol,
        route=tuple(pool.address for pool in fill.swap.route),
        amount_in=fill.swap.amount_in,
        min_amount_out=fill.swap.min_amount_out,
        amount_out=fill.amount_out,
        gas_cost_eth=fill.gas_cost_eth,
        block=fill.block,
    )


def test_a_send_is_open_from_its_beginning_and_keeps_each_leg_as_it_fills(store):
    assert store.open_send(_RUN) is None
    store.begin_send(_RUN, FIRST_DAY, started_at=FIRST_DAY + 600)
    assert store.open_send(_RUN) == OpenSend(time=FIRST_DAY, started_at=FIRST_DAY + 600)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    store.record_leg(_RUN, FIRST_DAY, 1, _LEG_1)
    assert store.open_send(_RUN).legs == (_record(_LEG_0, 0), _record(_LEG_1, 1))
    # The legs are the send's; the run's fills are its decisions'.
    assert store.fills(_RUN) == []
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_decision_whose_fills_are_the_legs_settles_the_send(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    store.record(_RUN, _step(_LEG_0, outcome=Outcome.PARTIAL))
    assert store.open_send(_RUN) is None
    assert store.fills(_RUN) == [_record(_LEG_0, 0)]
    assert store.decision(_RUN, FIRST_DAY).outcome is Outcome.PARTIAL
    # The next bar's send is begun afresh.
    store.begin_send(_RUN, FIRST_DAY + DAY, started_at=0)
    assert store.open_send(_RUN).time == FIRST_DAY + DAY


def test_a_rejected_decision_settles_a_send_that_filled_nothing(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.record(_RUN, _step(outcome=Outcome.REJECTED))
    assert store.open_send(_RUN) is None


def test_a_decision_whose_fills_are_not_the_legs_is_refused(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    for step in (_step(_LEG_0, _LEG_1), _step(_LEG_1), _step(outcome=Outcome.REJECTED)):
        with pytest.raises(StoreError, match="are not the 1 leg"):
            store.record(_RUN, step)
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_failed_send_says_what_stopped_it_once_and_is_not_settled(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    store.fail_send(_RUN, FIRST_DAY, failure="SwapNotFilled: reverted", gas_eth=D("0.0005"))
    opened = store.open_send(_RUN)
    assert (opened.failure, opened.failed_gas_eth) == ("SwapNotFilled: reverted", D("0.0005"))
    with pytest.raises(StoreError, match="has already failed"):
        store.fail_send(_RUN, FIRST_DAY, failure="again", gas_eth=None)
    with pytest.raises(StoreError, match="has failed"):
        store.record_leg(_RUN, FIRST_DAY, 1, _LEG_1)
    with pytest.raises(StoreError, match="a failed send is not settled by a decision"):
        store.record(_RUN, _step(_LEG_0, outcome=Outcome.PARTIAL))


def test_a_failure_whose_gas_is_not_known_keeps_none(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.fail_send(_RUN, FIRST_DAY, failure="RpcUnavailable: down", gas_eth=None)
    assert store.open_send(_RUN).failed_gas_eth is None


@pytest.mark.parametrize(
    ("failure", "gas", "match"),
    [
        ("", None, "failure must be a non-empty string"),
        ("why", D("-1"), "gas_eth must be a non-negative Decimal"),
        ("why", 0.5, "gas_eth must be a non-negative Decimal"),
    ],
)
def test_a_failure_that_cannot_be_kept_is_refused(store, failure, gas, match):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    with pytest.raises(ValueError, match=match):
        store.fail_send(_RUN, FIRST_DAY, failure=failure, gas_eth=gas)


def test_a_send_is_begun_only_with_none_open_and_on_a_bar_after_the_last_decided(store):
    with pytest.raises(StoreError, match="there is no run 'nope'"):
        store.begin_send("nope", FIRST_DAY, started_at=0)
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    with pytest.raises(StoreError, match="has an open send at"):
        store.begin_send(_RUN, FIRST_DAY + DAY, started_at=0)
    store.record(_RUN, _step(outcome=Outcome.REJECTED))
    for time in (FIRST_DAY - DAY, FIRST_DAY):
        with pytest.raises(StoreError, match="has decided the bar at"):
            store.begin_send(_RUN, time, started_at=0)


def test_legs_are_written_in_order_and_only_to_an_open_send(store):
    with pytest.raises(StoreError, match="has no open send at"):
        store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    with pytest.raises(StoreError, match="the next is leg 0, not 1"):
        store.record_leg(_RUN, FIRST_DAY, 1, _LEG_1)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    with pytest.raises(StoreError, match="the next is leg 1, not 0"):
        store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    store.record(_RUN, _step(_LEG_0, outcome=Outcome.PARTIAL))
    # A settled send takes no more legs, nor a failure.
    with pytest.raises(StoreError, match="has no open send at"):
        store.record_leg(_RUN, FIRST_DAY, 1, _LEG_1)
    with pytest.raises(StoreError, match="has no open send at"):
        store.fail_send(_RUN, FIRST_DAY, failure="late", gas_eth=None)


def test_a_send_with_nothing_of_it_sent_is_taken_back_and_no_other_is(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.abandon_send(_RUN, FIRST_DAY)
    assert store.open_send(_RUN) is None
    # Begun again, as if for the first time.
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    store.record_leg(_RUN, FIRST_DAY, 0, _LEG_0)
    with pytest.raises(StoreError, match="has a leg or a failure, and stays"):
        store.abandon_send(_RUN, FIRST_DAY)
    store.record(_RUN, _step(_LEG_0, outcome=Outcome.PARTIAL))
    with pytest.raises(StoreError, match="has no open send at"):
        store.abandon_send(_RUN, FIRST_DAY)
    store.begin_send(_RUN, FIRST_DAY + DAY, started_at=0)
    store.fail_send(_RUN, FIRST_DAY + DAY, failure="why", gas_eth=None)
    with pytest.raises(StoreError, match="has a leg or a failure, and stays"):
        store.abandon_send(_RUN, FIRST_DAY + DAY)
    assert store.open_send(_RUN).time == FIRST_DAY + DAY


def test_a_bar_is_not_decided_while_another_bars_send_is_open(store):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    with pytest.raises(StoreError, match="has an open send at"):
        store.record(_RUN, _step(outcome=Outcome.REJECTED, time=FIRST_DAY + DAY))


def test_a_chain_runs_fills_that_were_not_written_as_legs_are_refused(store):
    with pytest.raises(StoreError, match="were not written as the legs of a send"):
        store.record(_RUN, _step(_LEG_0))
    # A step that traded nothing needs no send.
    store.record(_RUN, _step(outcome=Outcome.REJECTED))


def test_a_runs_second_open_send_is_found_out_when_read(store, tmp_path):
    store.begin_send(_RUN, FIRST_DAY, started_at=0)
    connection = sqlite3.connect(tmp_path / "store.db")
    connection.execute(
        "INSERT INTO sends (run_id, time, started_at) VALUES (?, ?, 0)", (_RUN, FIRST_DAY + DAY)
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="has 2 open sends"):
        store.open_send(_RUN)


def test_a_store_from_before_the_sends_gains_them_and_its_runs_no_fork_block(tmp_path):
    path = tmp_path / "store.db"
    connection = store_at(path, 4)
    connection.execute(
        "INSERT INTO runs VALUES ('run-1', 'backtest', 1, 'USDC', 'fixed_weights', '{}', "
        """'{"USDC": "10000", "WBTC": "0", "WETH": "0"}', '1', 0, 'model')"""
    )
    connection.close()
    with open_store(path) as store:
        assert store.run("run-1").fork_block is None
        assert store.open_send("run-1") is None
        store.begin_send("run-1", FIRST_DAY, started_at=0)
        assert store.open_send("run-1").time == FIRST_DAY
