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
    fill_id,
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
        "run_seed_positions",
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


def test_transaction_begin_failure_does_not_brick(tmp_path):
    # A BEGIN that itself dies (here: a lock timeout against a concurrent
    # writer) must not leave the nesting flag stuck — every later unit of work
    # would be misdiagnosed as nested and rejected forever.
    path = tmp_path / "p.db"
    db1 = Database(path)
    db2 = Database(path)
    db1.conn.execute("PRAGMA busy_timeout = 1")
    # db2's write lock must be held while db1's BEGIN fails — keep the nesting.
    with db2.transaction():  # noqa: SIM117
        with pytest.raises(sqlite3.OperationalError), db1.transaction():
            pass  # pragma: no cover — BEGIN IMMEDIATE fails before the body
    with db1.transaction() as conn:  # the failed BEGIN left no open transaction
        conn.execute("SELECT 1")
    db1.close()
    db2.close()


def test_failed_migration_closes_connection(tmp_path, monkeypatch):
    # If a migration fails inside Database.__init__ the caller never gets an
    # instance to close, so the constructor itself must release the connection
    # (an open handle would keep the file locked on Windows).
    from contrib.hyperliquid_perp.persistence import db as db_module

    monkeypatch.setitem(db_module.MIGRATIONS, SCHEMA_VERSION + 1, ["THIS IS NOT SQL"])
    path = tmp_path / "p.db"
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    path.unlink()  # fails on Windows if the connection leaked


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
    fid = funding_event_id("r1", "BTC", _TS)
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
            mark_price=Decimal("60000"),
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
            mark_price=Decimal("60000"),
        )
    db.close()


def test_duplicate_decision_attempt_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    aid = decision_attempt_id("r1", _TS)
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
            updated_at=_TS,
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


# --------------------------------------------------------------------------
# write-boundary validation (typos and desynced derived values fail loud)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("side", "Buy", "side"),
        ("mode", "papertrade", "mode"),
        ("liquidity_type", "takerr", "liquidity_type"),
        ("fill_notional", Decimal("601"), "fill_notional"),
        ("fee", Decimal("0.28"), "fee"),
    ],
)
def test_insert_fill_rejects_bad_values(tmp_path, field, value, match):
    db = Database(tmp_path / "p.db")
    kwargs = {**_fill_kwargs(), field: value}
    with pytest.raises(ValueError, match=match), db.transaction() as conn:
        repo.insert_fill(conn, **kwargs)
    assert len(repo.iter_fills(db.conn, "r1")) == 0
    db.close()


def test_insert_fill_rejects_bad_flip_leg(tmp_path):
    db = Database(tmp_path / "p.db")
    with pytest.raises(ValueError, match="flip_leg"), db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs(), "flip_leg": "opening"})
    db.close()


def test_insert_funding_event_rejects_desynced_derived_values(tmp_path):
    db = Database(tmp_path / "p.db")
    base = {
        "funding_event_id": funding_event_id("r1", "BTC", _TS),
        "mode": "paper",
        "run_id": "r1",
        "symbol": "BTC",
        "funding_timestamp": _TS,
        "position_size": Decimal("0.05"),
        "status": "posted",
        "mark_price": Decimal("60000"),
        "funding_rate": Decimal("0.0001"),
    }
    with pytest.raises(ValueError, match="signed_position_notional"), db.transaction() as conn:
        repo.insert_funding_event(
            conn, **base, signed_position_notional=Decimal("9999"), funding_pnl=Decimal("-0.3")
        )
    with pytest.raises(ValueError, match="funding_pnl"), db.transaction() as conn:
        repo.insert_funding_event(
            conn, **base, signed_position_notional=Decimal("3000"), funding_pnl=Decimal("0.3")
        )
    with pytest.raises(ValueError, match="status"), db.transaction() as conn:
        repo.insert_funding_event(conn, **{**base, "status": "settled"})
    db.close()


def test_set_funding_status_rejects_desynced_pnl(tmp_path):
    db = Database(tmp_path / "p.db")
    with pytest.raises(ValueError, match="funding_pnl"), db.transaction() as conn:
        repo.set_funding_status(
            conn,
            "whatever",
            status="posted",
            signed_position_notional=Decimal("3000"),
            funding_rate=Decimal("0.0001"),
            funding_pnl=Decimal("0.3"),  # should be -0.3
        )
    db.close()


def test_insert_funding_event_enforces_status_field_coupling(tmp_path):
    db = Database(tmp_path / "p.db")
    base = {
        "mode": "paper",
        "run_id": "r1",
        "symbol": "BTC",
        "funding_timestamp": _TS,
        "position_size": Decimal("0.05"),
    }
    # pending must capture its settlement mark ...
    with pytest.raises(ValueError, match="mark_price"), db.transaction() as conn:
        repo.insert_funding_event(conn, funding_event_id="p1", status="pending", **base)
    # ... and cannot already carry the not-yet-learned settlement math ...
    with pytest.raises(ValueError, match="cannot already carry"), db.transaction() as conn:
        repo.insert_funding_event(
            conn,
            funding_event_id="p2",
            status="pending",
            mark_price=Decimal("60000"),
            funding_rate=Decimal("0.0001"),
            **base,
        )
    # ... while posted means the wallet moved, so the math must be complete.
    with pytest.raises(ValueError, match="missing"), db.transaction() as conn:
        repo.insert_funding_event(
            conn,
            funding_event_id="p3",
            status="posted",
            mark_price=Decimal("60000"),
            signed_position_notional=Decimal("3000"),
            funding_rate=Decimal("0.0001"),  # funding_pnl missing
            **base,
        )
    db.close()


def test_set_funding_status_is_pending_to_posted_only(tmp_path):
    db = Database(tmp_path / "p.db")
    fid = funding_event_id("r1", "BTC", _TS)
    # No mark_price supplied: posting must fall back to the stored basis mark.
    posting = {
        "status": "posted",
        "funding_rate": Decimal("0.0001"),
        "funding_pnl": Decimal("-0.3"),
        "signed_position_notional": Decimal("3000"),
    }
    with pytest.raises(ValueError, match="does not exist"), db.transaction() as conn:
        repo.set_funding_status(conn, "missing", **posting)
    with db.transaction() as conn:
        repo.insert_funding_event(
            conn,
            funding_event_id=fid,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.05"),
            status="pending",
            mark_price=Decimal("60000"),
        )
    with pytest.raises(ValueError, match="pending -> posted"), db.transaction() as conn:
        repo.set_funding_status(conn, fid, status="pending")
    # The posted notional must be the stored basis (0.05 * 60000), not the
    # caller's own idea of it.
    with pytest.raises(ValueError, match="signed_position_notional"), db.transaction() as conn:
        repo.set_funding_status(
            conn,
            fid,
            **{
                **posting,
                "signed_position_notional": Decimal("9999"),
                "funding_pnl": Decimal("-0.9999"),
            },
        )
    with db.transaction() as conn:
        repo.set_funding_status(conn, fid, **posting)
    with pytest.raises(ValueError, match="already posted"), db.transaction() as conn:
        repo.set_funding_status(conn, fid, **posting)
    db.close()


def test_insert_run_rejects_bad_mode(tmp_path):
    db = Database(tmp_path / "p.db")
    with pytest.raises(ValueError, match="mode"), db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r1",
            mode="dry-run",
            initial_balance_usdc=Decimal("1000"),
            schema_version=1,
        )
    db.close()


# --------------------------------------------------------------------------
# run seed positions (replay genesis)
# --------------------------------------------------------------------------


def test_run_seed_positions_round_trip(tmp_path):
    db = Database(tmp_path / "p.db")
    seed = PositionState(coin="BTC", size=Decimal("0.01"), entry_price=Decimal("50000"))
    with db.transaction() as conn:
        repo.insert_run_seed_position(conn, "r1", seed)
    assert repo.get_run_seed_positions(db.conn, "r1") == [seed]
    assert repo.get_run_seed_positions(db.conn, "other") == []
    db.close()


# --------------------------------------------------------------------------
# migration failure atomicity + connection posture
# --------------------------------------------------------------------------


def test_partial_migration_rolls_back_whole_version(tmp_path, monkeypatch):
    # A version whose DDL fails partway must leave neither its earlier tables
    # nor a schema_migrations row behind — the exact guarantee a future v2 needs.
    from contrib.hyperliquid_perp.persistence import db as db_mod

    broken = dict(db_mod.MIGRATIONS)
    broken[2] = ("CREATE TABLE v2_ok (x TEXT)", "CREATE BOGUS SYNTAX")
    monkeypatch.setattr(db_mod, "MIGRATIONS", broken)
    conn = connect(tmp_path / "p.db")
    with pytest.raises(sqlite3.OperationalError):
        db_mod.apply_migrations(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "v2_ok" not in tables  # the version's earlier statement rolled back
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]  # v1 applied, v2 not recorded
    conn.close()


def test_connect_enables_wal_and_busy_timeout(tmp_path):
    conn = connect(tmp_path / "p.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


# --------------------------------------------------------------------------
# id canonicalization (dedup keys must not depend on the caller's tz form)
# --------------------------------------------------------------------------


def test_ids_canonicalize_timestamp_representation():
    from datetime import timedelta

    offset_view = _TS.astimezone(timezone(timedelta(hours=5)))
    assert funding_event_id("r1", "BTC", offset_view) == funding_event_id("r1", "BTC", _TS)
    assert decision_attempt_id("r1", offset_view) == decision_attempt_id("r1", _TS)
    with pytest.raises(ValueError, match="timezone-aware"):
        decision_attempt_id("r1", datetime(2026, 7, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        funding_event_id("r1", "BTC", datetime(2026, 7, 1))


def test_naive_datetime_rejected_on_write(tmp_path):
    # Storage requires UTC-aware timestamps so the funding dedup key is canonical.
    db = Database(tmp_path / "p.db")
    naive = datetime(2026, 7, 1)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"), db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs("f1", None), "timestamp": naive})
    db.close()


# --------------------------------------------------------------------------
# fill_id: deterministic exactly-once key for slice-less fills
# --------------------------------------------------------------------------


def test_fill_id_is_deterministic_and_guarded():
    assert fill_id("r1", "o1") == "r1|o1|0"
    assert fill_id("r1", "o1") == fill_id("r1", "o1")  # a retry re-derives the same id
    assert fill_id("r1", "o1", 1) != fill_id("r1", "o1")
    with pytest.raises(ValueError, match="must not contain"):
        fill_id("r|1", "o1")
    with pytest.raises(ValueError, match="must not be empty"):
        fill_id("r1", "")


def test_duplicate_derived_fill_id_rejected_by_primary_key(tmp_path):
    # A slice-less (paper_market / SL / TP) fill has no slice_id; the PK on the
    # derived fill_id is what stops a crash-retry from double-posting it.
    db = Database(tmp_path / "p.db")
    fid = fill_id("r1", "o1")
    with db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs(fid, None), "timestamp": _TS})
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs(fid, None), "timestamp": _TS})
    count = db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert count == 1
    db.close()


# --------------------------------------------------------------------------
# scheduler_state: patch-style upsert semantics
# --------------------------------------------------------------------------


def test_scheduler_state_patch_upsert_preserves_unsupplied_fields(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r1",
            last_decision_at=_TS,
            next_decision_at=_TS,
            last_input_id="in1",
            last_output_id="out1",
            current_attempt_id="a1",
        )
    before = repo.get_scheduler_state(db.conn, "r1")
    later = datetime(2026, 7, 1, 4, tzinfo=timezone.utc)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", next_decision_at=later)
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["next_decision_at"] != before["next_decision_at"]  # advanced
    assert row["last_decision_at"] == before["last_decision_at"]  # preserved
    assert row["last_input_id"] == "in1"  # crash-recovery breadcrumbs preserved
    assert row["last_output_id"] == "out1"
    assert row["current_attempt_id"] == "a1"
    # An explicit None is a deliberate clear, distinct from "not supplied".
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", current_attempt_id=None)
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["current_attempt_id"] is None
    assert row["last_input_id"] == "in1"
    db.close()


def test_scheduler_state_fresh_partial_insert_leaves_rest_null(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r2", next_decision_at=_TS)
    row = repo.get_scheduler_state(db.conn, "r2")
    assert row["next_decision_at"] is not None
    assert row["last_decision_at"] is None
    assert row["last_input_id"] is None
    assert row["updated_at"] is not None  # always stamped
    db.close()


# --------------------------------------------------------------------------
# read_transaction: one consistent snapshot, reads only
# --------------------------------------------------------------------------


def test_read_transaction_pins_a_snapshot(tmp_path):
    path = tmp_path / "p.db"
    db = Database(path)
    with db.transaction() as conn:
        repo.insert_fill(conn, **{**_fill_kwargs("f1", None), "timestamp": _TS})
    writer = connect(path)
    with db.read_transaction() as conn:
        before = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        # A commit landing on another connection mid-read must stay invisible
        # to this snapshot (WAL lets it proceed without blocking).
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol, side,"
            " fill_qty, fill_price, fill_notional, fee, fee_rate, realized_pnl_delta,"
            " liquidity_type) VALUES ('f2', 't', 'paper', 'r1', 'o1', 'BTC', 'buy',"
            " '1', '1', '1', '0', '0', '0', 'simulated')"
        )
        writer.execute("COMMIT")
        after = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        assert before == after == 1
    # Outside the snapshot the new row is visible.
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    writer.close()
    db.close()


def test_read_transaction_rejects_writes_and_nesting(tmp_path):
    db = Database(tmp_path / "p.db")
    insert_run = (
        "INSERT INTO runs (run_id, mode, created_at, initial_balance_usdc, schema_version)"
        " VALUES ('r', 'paper', 't', '1', 1)"
    )
    with db.read_transaction() as conn:
        with pytest.raises(sqlite3.OperationalError):  # query_only: writes fail loud
            conn.execute(insert_run)
        with pytest.raises(RuntimeError, match="nested"), db.transaction():
            pass
    # query_only is restored: a normal write transaction works afterwards, and
    # nesting is rejected in the other direction too.
    with db.transaction() as conn:
        conn.execute(insert_run)
        with pytest.raises(RuntimeError, match="nested"), db.read_transaction():
            pass
    assert db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    db.close()
