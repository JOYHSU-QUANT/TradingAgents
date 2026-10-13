"""The repository readers behind ``status`` (dashboard plan PR 2a).

Each reader is run-scoped, ordered as its docstring says, and capped where it
says it is; the suite stages two runs so a forgotten run filter shows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.runtime import accounting

from ..conftest import (
    insert_decision_attempts,
    insert_minimal_account_snapshot,
    insert_minimal_ai_output,
    insert_minimal_fill,
)

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    with Database(":memory:") as database:
        for run_id in ("r", "other"):
            accounting.initialize_run(
                database,
                run_id=run_id,
                mode="paper",
                initial_balance_usdc=D(1000),
                schema_version=1,
            )
        yield database


def _at(hours: int) -> datetime:
    return _T0 + timedelta(hours=hours)


def test_the_latest_lease_is_the_scheduler_row_touched_last(db):
    assert repo.latest_lease_run_id(db.conn) is None
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "other", updated_at=_at(1))
        repo.upsert_scheduler_state(conn, "r", updated_at=_at(2))
    assert repo.latest_lease_run_id(db.conn) == "r"
    with db.transaction() as conn:
        # A later heartbeat on the other run moves it.
        repo.upsert_scheduler_state(conn, "other", updated_at=_at(3))
    assert repo.latest_lease_run_id(db.conn) == "other"
    with db.transaction() as conn:
        # The same instant on both: the run id breaks the tie, so the answer
        # does not depend on which row SQLite happens to visit first.
        repo.upsert_scheduler_state(conn, "r", updated_at=_at(3))
    assert repo.latest_lease_run_id(db.conn) == "other"


def test_attempt_status_counts_are_run_scoped_and_by_status_name(db):
    insert_decision_attempts(db, ["completed", "api_failed", "completed"], start=_T0)
    insert_decision_attempts(db, ["invalid_output"], run_id="other", start=_T0)
    assert repo.attempt_status_counts(db.conn, "r") == {"api_failed": 1, "completed": 2}
    assert repo.attempt_status_counts(db.conn, "ghost") == {}


def test_recent_cycles_come_newest_first_capped_with_their_verdict(db):
    insert_decision_attempts(db, ["completed", "api_failed", "completed"], start=_T0)
    insert_decision_attempts(db, ["completed"], run_id="other", start=_T0)
    with db.transaction() as conn:
        insert_minimal_ai_output(
            conn,
            run_id="r",
            attempt_id=decision_attempt_id("r", _at(0)),
            timestamp=_at(0),
            decision_mode="set_target",
            target_side="long",
            approved_target_margin_pct=D(20),
            account_equity=D(1000),
            order_created=True,
            no_order_reason=None,
            decision_reason="a reason",
        )
    rows = repo.recent_decision_cycles(db.conn, "r", limit=2)
    assert [row["status"] for row in rows] == ["completed", "api_failed"]
    assert rows[0]["decision_mode"] is None  # the newest reached no verdict
    oldest = repo.recent_decision_cycles(db.conn, "r", limit=5)[-1]
    assert (oldest["symbol"], oldest["target_side"], oldest["account_equity"]) == (
        "BTC",
        "long",
        "1000",
    )
    assert oldest["order_created"] == 1
    assert repo.recent_decision_cycles(db.conn, "r", limit=0) == []


def test_model_regimes_bucket_the_input_rows_first_seen_first(db):
    with db.transaction() as conn:
        for i, (model, version) in enumerate(
            [("deep-a", "v6"), ("deep-a", "v6"), ("deep-b", "v6"), ("deep-a", "v6")]
        ):
            repo.insert_ai_input(
                conn,
                input_id=f"r|in|{i}",
                timestamp=_at(i),
                mode="paper",
                run_id="r",
                symbol="BTC",
                model=model,
                prompt_version=version,
            )
        repo.insert_ai_input(
            conn, input_id="o|in|0", timestamp=_at(0), mode="paper", run_id="other", symbol="BTC"
        )
    buckets = repo.model_regimes(db.conn, "r")
    assert [(b["model"], b["prompt_version"], b["prompts"]) for b in buckets] == [
        ("deep-a", "v6", 3),
        ("deep-b", "v6", 1),
    ]
    assert (buckets[0]["first_at"], buckets[0]["last_at"]) == (
        _at(0).isoformat(),
        _at(3).isoformat(),
    )
    (unnamed,) = repo.model_regimes(db.conn, "other")
    assert tuple(unnamed) == (None, None, 1, _at(0).isoformat(), _at(0).isoformat())


def _snapshot(conn, run_id: str, at: datetime, equity: str) -> None:
    insert_minimal_account_snapshot(conn, run_id=run_id, timestamp=at, account_equity=D(equity))


def test_recent_account_snapshots_come_last_inserted_first_and_run_scoped(db):
    assert repo.recent_account_snapshots(db.conn, "r", limit=5) == []
    with db.transaction() as conn:
        _snapshot(conn, "r", _at(0), "1000")
        _snapshot(conn, "r", _at(1), "1010")
        _snapshot(conn, "other", _at(2), "900")
    recent = repo.recent_account_snapshots(db.conn, "r", limit=5)
    assert [row["account_equity"] for row in recent] == ["1010", "1000"]
    (newest,) = repo.recent_account_snapshots(db.conn, "r", limit=1)
    assert newest["account_equity"] == "1010"


def test_current_position_rows_carry_protection_and_the_mirrored_liquidation(db):
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn, "r", PositionState(coin="BTC", size=D("0.5"), entry_price=D(50000))
        )
        repo.set_position_protection(
            conn, "r", "BTC", stop_loss_price=D(48000), take_profit_price=None
        )
        repo.set_position_liquidation_price(conn, "r", "BTC", D(30000))
        repo.upsert_current_position(conn, "other", PositionState.flat("BTC"))
    (row,) = repo.current_position_rows(db.conn, "r")
    assert (row["size"], row["stop_loss_price"], row["take_profit_price"]) == (
        "0.5",
        "48000",
        None,
    )
    assert row["exchange_liquidation_price"] == "30000"


def _fill(conn, run_id: str, fill_id: str, at: datetime, price: int) -> None:
    insert_minimal_fill(conn, run_id=run_id, fill_id=fill_id, timestamp=at, price=price)


def test_recent_fills_and_event_tails_come_newest_first_and_capped(db):
    with db.transaction() as conn:
        # Inserted out of time order: the newest fill is the newest by its
        # timestamp, not the last row written.
        for i in (1, 2, 0):
            _fill(conn, "r", f"r|f|{i}", _at(i), 50000 + i)
        for i in range(3):
            repo.insert_kill_switch_event(
                conn, run_id="r", event_type="kill_switch_refreshed", timestamp=_at(i)
            )
            repo.insert_safe_mode_event(
                conn,
                run_id="r",
                event_type="safe_mode_entered",
                safe_mode_type="recoverable",
                reason=f"reason {i}",
                timestamp=_at(i),
            )
        _fill(conn, "other", "o|f|0", _at(9), 1)
        repo.insert_kill_switch_event(
            conn, run_id="other", event_type="kill_switch_armed", timestamp=_at(9)
        )
    assert [r["fill_price"] for r in repo.recent_fills(db.conn, "r", limit=2)] == [
        "50002",
        "50001",
    ]
    assert [r["timestamp"] for r in repo.recent_kill_switch_events(db.conn, "r", limit=2)] == [
        _at(2).isoformat(),
        _at(1).isoformat(),
    ]
    assert [r["reason"] for r in repo.recent_safe_mode_events(db.conn, "r", limit=10)] == [
        "reason 2",
        "reason 1",
        "reason 0",
    ]
    (armed,) = repo.recent_kill_switch_events(db.conn, "other", limit=5)
    assert armed["event_type"] == "kill_switch_armed"
