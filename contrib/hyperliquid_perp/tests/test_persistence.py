"""Tests for the SQLite persistence layer: migrations, transactions, constraints."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database, connect
from contrib.hyperliquid_perp.persistence.ids import (
    decision_attempt_id,
    funding_event_id,
    slice_id,
)
from contrib.hyperliquid_perp.persistence.models import AccountLedger, PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _fill_kwargs(fill_id="f1", slice_id_=None):
    return {
        "fill_id": fill_id,
        "mode": "paper",
        "run_id": "r1",
        "order_id": "o1",
        "symbol": "BTC",
        "side": "buy",
        "fill_qty": Decimal("0.01"),
        "fill_price": Decimal("60000"),
        "fill_notional": Decimal("600"),
        "fee": Decimal("0.27"),
        "fee_rate": Decimal("0.00045"),
        "realized_pnl_delta": Decimal("0"),
        "slice_id": slice_id_,
    }


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def test_migrations_record_version(tmp_path):
    db = Database(tmp_path / "p.db")
    rows = db.conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [r[0] for r in rows] == [SCHEMA_VERSION]
    db.close()


def test_migrations_idempotent_on_reopen(tmp_path):
    path = tmp_path / "p.db"
    Database(path).close()
    db = Database(path)  # reopen: must not re-run migration 1
    count = db.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 1
    db.close()


def test_all_tables_exist(tmp_path):
    db = Database(tmp_path / "p.db")
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in (
        "ai_inputs",
        "decision_attempts",
        "ai_outputs",
        "orders",
        "fills",
        "funding_events",
        "account_snapshots",
        "position_snapshots",
        "runs",
        "scheduler_state",
        "execution_plans",
        "current_positions",
        "current_account_state",
        "schema_migrations",
    ):
        assert expected in names
    db.close()


# --------------------------------------------------------------------------
# transaction semantics
# --------------------------------------------------------------------------


def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "p.db")
    with pytest.raises(RuntimeError, match="boom"), db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r1",
            mode="paper",
            initial_balance_usdc=Decimal("1000"),
            schema_version=1,
        )
        raise RuntimeError("boom")  # crash before COMMIT
    assert repo.get_run(db.conn, "r1") is None
    db.close()


def test_transaction_commits(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r1",
            mode="paper",
            initial_balance_usdc=Decimal("1000"),
            schema_version=1,
        )
    assert repo.get_run(db.conn, "r1") is not None
    db.close()


def test_committed_state_survives_reopen(tmp_path):
    path = tmp_path / "p.db"
    db = Database(path)
    with db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r1",
            mode="paper",
            initial_balance_usdc=Decimal("1000"),
            schema_version=1,
        )
    db.close()
    reopened = Database(path)
    assert repo.get_run(reopened.conn, "r1") is not None
    reopened.close()


def test_transaction_cannot_nest(tmp_path):
    db = Database(tmp_path / "p.db")
    # The outer transaction must stay open — the nested BEGIN is the thing under test.
    with db.transaction():  # noqa: SIM117
        with pytest.raises(RuntimeError, match="cannot be nested"), db.transaction():
            pass
    db.close()


# --------------------------------------------------------------------------
# unique constraints (dedup / exactly-once keys)
# --------------------------------------------------------------------------


def test_duplicate_slice_id_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    sid = slice_id("r1", "plan1", None, 0)
    with db.transaction() as conn:
        repo.insert_fill(conn, **_fill_kwargs("f1", sid))
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        repo.insert_fill(conn, **_fill_kwargs("f2", sid))
    # the second fill was rolled back — only one row exists
    assert len(repo.iter_fills(db.conn, "r1")) == 1
    db.close()


def test_null_slice_ids_are_distinct(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_fill(conn, **_fill_kwargs("f1", None))
        repo.insert_fill(conn, **_fill_kwargs("f2", None))
    assert len(repo.iter_fills(db.conn, "r1")) == 2  # paper_market/SL/TP fills not blocked
    db.close()


def test_duplicate_funding_key_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    fid = funding_event_id("r1", "BTC", _TS.isoformat())
    with db.transaction() as conn:
        repo.insert_funding_event(
            conn,
            funding_event_id=fid,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.01"),
            status="pending",
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        # same (run_id, symbol, funding_timestamp) but a different id string
        repo.insert_funding_event(
            conn,
            funding_event_id="other",
            mode="paper",
            run_id="r1",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.01"),
            status="pending",
        )
    db.close()


def test_duplicate_decision_attempt_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    aid = decision_attempt_id("r1", _TS.isoformat())
    row = {
        "decision_attempt_id": aid,
        "timestamp": _TS,
        "mode": "paper",
        "run_id": "r1",
        "scheduled_at": _TS,
        "status": "in_progress",
    }
    with db.transaction() as conn:
        repo.insert_decision_attempt(conn, **row)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        repo.insert_decision_attempt(conn, **{**row, "decision_attempt_id": "other"})
    db.close()


# --------------------------------------------------------------------------
# current_* round-trips
# --------------------------------------------------------------------------


def test_current_account_state_round_trip(tmp_path):
    db = Database(tmp_path / "p.db")
    ledger = AccountLedger(
        wallet_balance=Decimal("1009.4555"),
        realized_pnl=Decimal("10"),
        total_fees=Decimal("0.5445"),
        net_funding_pnl=Decimal("0"),
    )
    with db.transaction() as conn:
        repo.upsert_current_account_state(conn, "r1", ledger)
    assert repo.get_current_account_state(db.conn, "r1") == ledger
    db.close()


def test_current_position_round_trip_and_upsert(tmp_path):
    db = Database(tmp_path / "p.db")
    pos = PositionState(coin="BTC", size=Decimal("0.05"), entry_price=Decimal("60000"))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r1", pos)
    assert repo.get_current_position(db.conn, "r1", "BTC") == pos
    # upsert overwrites the same (run_id, symbol)
    flat = PositionState.flat("BTC", realized_pnl=Decimal("123"))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r1", flat)
    got = repo.get_current_position(db.conn, "r1", "BTC")
    assert got == flat and got.is_flat
    db.close()


def test_connect_memory_db_applies_no_persistence():
    # A bare connect() has no schema until migrations run; Database wires them.
    conn = connect(":memory:")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fills" not in tables
    conn.close()


def test_thin_inserts_align_with_schema_columns(tmp_path):
    # The **fields passthrough inserts build column lists from their kwargs; this
    # locks the column names against the DDL so a typo raises here, not in PR3/PR4.
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_ai_input(
            conn, input_id="i1", timestamp=_TS, mode="paper", run_id="r1", symbol="BTC"
        )
        repo.insert_ai_output(
            conn,
            output_id="o1",
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            decision_mode="maintain_current",
            risk_action="approved",
            order_created=False,
        )
        repo.insert_order(
            conn,
            order_id="ord1",
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            order_role="entry",
            side="buy",
            type="paper_market",
            qty=Decimal("0.01"),
            status="filled",
        )
        repo.insert_account_snapshot(
            conn,
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            wallet_balance=Decimal("1000"),
            account_equity=Decimal("1000"),
            available_balance=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_pnl=Decimal("0"),
            total_fees=Decimal("0"),
            net_funding_pnl=Decimal("0"),
            total_position_notional=Decimal("0"),
            effective_leverage=Decimal("0"),
            used_initial_margin=Decimal("0"),
            total_maintenance_margin=Decimal("0"),
        )
        repo.insert_position_snapshot(
            conn,
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            position_size=Decimal("0.01"),
            side="long",
            mark_price=Decimal("60000"),
            position_notional=Decimal("600"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            maintenance_margin=Decimal("6"),
        )
        repo.insert_execution_plan(
            conn,
            plan_id="p1",
            run_id="r1",
            symbol="BTC",
            status="active",
            created_at=_TS,
            updated_at=_TS,
        )
    counts = {
        t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in (
            "ai_inputs",
            "ai_outputs",
            "orders",
            "account_snapshots",
            "position_snapshots",
            "execution_plans",
        )
    }
    assert all(c == 1 for c in counts.values())
    db.close()


def test_scheduler_state_round_trip(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", next_decision_at=_TS, last_output_id="o1")
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["last_output_id"] == "o1"
    assert row["next_decision_at"] == _TS.isoformat()
    db.close()


def test_naive_datetime_rejected_on_write(tmp_path):
    # Storage requires UTC-aware timestamps so the funding dedup key is canonical.
    db = Database(tmp_path / "p.db")
    naive = datetime(2026, 7, 1)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"), db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs("f1", None), "timestamp": naive})
    db.close()
