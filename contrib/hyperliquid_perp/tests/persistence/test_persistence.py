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
from contrib.hyperliquid_perp.persistence.schema import LEASE_READABLE_SINCE, SCHEMA_VERSION

from ..conftest import build_store_at

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
    rows = db.conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    # Every version up to the latest is recorded, and the latest IS SCHEMA_VERSION.
    assert [r[0] for r in rows] == list(range(1, SCHEMA_VERSION + 1))
    db.close()


def test_v10_adds_a_nullable_context_shape_to_ai_inputs(tmp_path):
    # Issue #97: the prompt's structure beside its version stamp. Nullable
    # on purpose — every pre-v10 row stays valid and reads as "written before
    # the column existed", never as a shape.
    db = Database(tmp_path / "p.db")
    cols = {row["name"]: row for row in db.conn.execute("PRAGMA table_info(ai_inputs)")}
    assert cols["context_shape"]["notnull"] == 0
    assert cols["context_shape"]["type"] == "TEXT"
    db.close()


def test_v11_adds_a_nullable_format_fingerprint_and_indexes_fills_by_run_and_time(tmp_path):
    # Issue #129: the third segmentation key, nullable for the same reason as
    # v10's (a pre-v11 row reads "written before the column", never as a
    # digest). Issue #134: the per-cycle ``MAX(timestamp) WHERE run_id`` read
    # was a full-table scan; the plan must now walk the index.
    db = Database(tmp_path / "p.db")
    cols = {row["name"]: row for row in db.conn.execute("PRAGMA table_info(ai_inputs)")}
    assert cols["format_fingerprint"]["notnull"] == 0
    assert cols["format_fingerprint"]["type"] == "TEXT"
    indexes = {row["name"]: row for row in db.conn.execute("PRAGMA index_list(fills)")}
    assert indexes["idx_fills_run_timestamp"]["unique"] == 0
    indexed = [row["name"] for row in db.conn.execute("PRAGMA index_info(idx_fills_run_timestamp)")]
    assert indexed == ["run_id", "timestamp"]
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT MAX(timestamp) FROM fills WHERE run_id = ?", ("r",)
    ).fetchall()
    assert any("idx_fills_run_timestamp" in row[3] for row in plan), [row[3] for row in plan]
    db.close()


_LEASE_READABLE_VERSIONS = list(range(LEASE_READABLE_SINCE, SCHEMA_VERSION + 1))


@pytest.mark.parametrize("version", _LEASE_READABLE_VERSIONS)
def test_every_pre_lease_reader_works_on_a_store_at_or_above_the_lease_floor(tmp_path, version):
    # Issue #147: the executable half of what ``schema.LEASE_READABLE_SINCE``
    # promises — see its comment for the floor and the reader list. Built at
    # EVERY version from the floor up so a later migration, or a new
    # pre-lease reader, fails HERE and not as an OperationalError inside an
    # owning command's refusal. Every pre-v6 version has no ``live`` tables at
    # all, which is the point: the readers must not need them.
    import json

    from contrib.hyperliquid_perp.cli.live_shared import _conflicting_run_lease
    from contrib.hyperliquid_perp.paper.run_lock import (
        acquire_run_lock,
        peek_run_lock,
        release_run_lock,
    )
    from contrib.hyperliquid_perp.persistence.db import stored_schema_version

    genesis = json.dumps({"live": {"network": "testnet"}})
    now = datetime.now(timezone.utc)

    def populate(db):
        with db.transaction() as conn:
            for run_id in ("own", "sibling"):
                repo.insert_run(
                    conn,
                    run_id=run_id,
                    mode="live",
                    initial_balance_usdc=Decimal("100"),
                    schema_version=version,
                    config_json=genesis,
                )
        acquire_run_lock(db, "sibling", pid=4321, now=now)

    path = tmp_path / f"v{version}.db"
    build_store_at(path, version, populate)

    with Database(path, migrate=False, defer_migration=True) as db:
        assert db.migration_pending is (version < SCHEMA_VERSION)
        # The readers, in the order the CLI runs them, against the old schema.
        assert repo.get_run(db.conn, "own")["mode"] == "live"
        leases = repo.iter_other_run_leases(db.conn, "own")
        assert [(r["run_id"], r["lock_pid"]) for r in leases] == [("sibling", 4321)]
        assert _conflicting_run_lease(db, "own") == ("sibling", 4321)
        peek_run_lock(db, "own", now=now)  # free: no raise
        # ...and the one WRITE paper / smoke make before migrating: the lease.
        acquire_run_lock(db, "own", pid=111, now=now)
        release_run_lock(db, "own", pid=111, now=now)
        assert stored_schema_version(db.conn) == version  # nothing upgraded yet
        db.apply_deferred_migration()
        assert stored_schema_version(db.conn) == SCHEMA_VERSION
        assert db.migration_pending is False


def test_a_deferred_open_refuses_a_populated_store_older_than_the_lease_floor(tmp_path):
    # Issue #147 (item 5): below the floor the lease columns do not exist, so
    # the deferring caller's first read would raise sqlite3.OperationalError —
    # main()'s exit-2 traceback, not a named exit 1. Refused at open instead,
    # with the store untouched, and with ONE remedy across both non-migrating
    # policies (the reporting refusal's "run paper/live" would only bounce
    # back here); the migrating open the message names still upgrades it.
    from contrib.hyperliquid_perp.persistence.db import SchemaVersionError, stored_schema_version

    below = LEASE_READABLE_SINCE - 1

    def populate(db):
        with db.transaction() as conn:
            repo.insert_run(
                conn,
                run_id="r",
                mode="paper",
                initial_balance_usdc=Decimal("1"),
                schema_version=below,
            )

    path = tmp_path / "pre-lease.db"
    build_store_at(path, below, populate)
    probe = connect(path)
    try:
        assert "lock_pid" not in {
            row["name"] for row in probe.execute("PRAGMA table_info(scheduler_state)")
        }
        floor_refusal = rf"v{below}.*v{LEASE_READABLE_SINCE}.*safe-mode --status"
        with pytest.raises(SchemaVersionError, match=floor_refusal):
            Database(path, migrate=False, defer_migration=True)
        with pytest.raises(SchemaVersionError, match=floor_refusal):
            Database(path, migrate=False)  # the read-only policy: same words
        assert stored_schema_version(probe) == below  # refused, not upgraded
    finally:
        probe.close()

    with Database(path) as upgraded:  # the named remedy: a migrating open
        assert stored_schema_version(upgraded.conn) == SCHEMA_VERSION
    with Database(path, migrate=False, defer_migration=True) as current:
        assert current.migration_pending is False


class _RacedConnection:
    """A real connection with one seam: the first time it goes for the write
    lock, ``winner_lands`` runs first — deterministic single-thread staging of
    "another process committed between my pre-read and my BEGIN IMMEDIATE".
    ``apply_migrations`` touches nothing but ``execute``."""

    def __init__(self, real, winner_lands):
        self._real = real
        self._winner_lands = winner_lands
        self.raced = False

    def execute(self, sql, *params):
        if sql == "BEGIN IMMEDIATE" and not self.raced:
            self.raced = True
            self._winner_lands()
        return self._real.execute(sql, *params)


def test_two_processes_migrating_the_same_behind_store_leave_the_loser_a_no_op(tmp_path):
    # Issue #147 (item 2): two owning commands started against the same behind
    # store in the same moment (two ``live`` on the shared live_trading.db)
    # both read "not yet applied" OUTSIDE the write lock. ``BEGIN IMMEDIATE``
    # serialises them correctly, but the loser then re-ran the winner's
    # ``ALTER TABLE ... ADD COLUMN`` and died on ``duplicate column name``.
    # The loser must skip the version, return the current one, and leave no
    # transaction open.
    from contrib.hyperliquid_perp.persistence.db import apply_migrations
    from contrib.hyperliquid_perp.persistence.schema import MIGRATIONS

    path = tmp_path / "shared.db"
    build_store_at(path, sorted(MIGRATIONS)[-2])
    winner = connect(path)
    loser_conn = connect(path)

    def winner_lands():
        assert apply_migrations(winner) == SCHEMA_VERSION

    loser = _RacedConnection(loser_conn, winner_lands)
    try:
        assert apply_migrations(loser) == SCHEMA_VERSION  # no duplicate-column raise
        assert loser.raced
        assert loser_conn.in_transaction is False  # the skipped version released its lock
        recorded = [
            row[0]
            for row in loser_conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert recorded == list(range(1, SCHEMA_VERSION + 1))  # one row per version, once
        # And the loser is a working connection afterwards: a write goes through.
        loser_conn.execute("BEGIN IMMEDIATE")
        loser_conn.execute("ROLLBACK")
    finally:
        winner.close()
        loser_conn.close()


def test_a_loser_whose_winner_ran_a_newer_build_is_refused_not_skipped(tmp_path):
    # The other half of the race fix: if the winner was a NEWER build, the
    # versions it landed carry the store past what this build knows. Skipping
    # them silently would return "current" and let this build write through a
    # schema it does not know — the very corruption the NEWER refusal exists
    # for, and something the pre-fix ``duplicate column name`` crash at least
    # prevented. The re-check under the write lock must raise that refusal.
    from contrib.hyperliquid_perp.persistence import db as db_module
    from contrib.hyperliquid_perp.persistence.db import SchemaVersionError, apply_migrations
    from contrib.hyperliquid_perp.persistence.schema import MIGRATIONS

    path = tmp_path / "shared.db"
    build_store_at(path, sorted(MIGRATIONS)[-2])
    winner = connect(path)
    loser_conn = connect(path)
    ahead = SCHEMA_VERSION + 1
    newer_build = {**MIGRATIONS, ahead: ("ALTER TABLE runs ADD COLUMN operator_note TEXT",)}

    def newer_winner_lands():
        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(db_module, "MIGRATIONS", newer_build)
            assert apply_migrations(winner) == ahead

    loser = _RacedConnection(loser_conn, newer_winner_lands)
    try:
        with pytest.raises(SchemaVersionError, match=rf"v{ahead}.*NEWER build"):
            apply_migrations(loser)
        assert loser.raced
        assert loser_conn.in_transaction is False  # the refusal rolled back its lock
    finally:
        winner.close()
        loser_conn.close()


def test_defer_migration_with_migrate_true_is_rejected_by_name(tmp_path):
    # Meaningless combination: migrate=True would already have upgraded the
    # store on open, so there is nothing left to defer. Fail loudly rather than
    # let a caller believe it deferred when it did not.
    with pytest.raises(ValueError, match="requires migrate=False"):
        Database(tmp_path / "x.db", migrate=True, defer_migration=True)


def test_migrations_idempotent_on_reopen(tmp_path):
    path = tmp_path / "p.db"
    Database(path).close()
    db = Database(path)  # reopen: must not re-run any migration
    count = db.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == SCHEMA_VERSION
    db.close()


def test_v3_v4_add_scheduler_state_operational_columns(tmp_path):
    # v3: the single-instance lease + CSV-export breadcrumbs; v4: the replay
    # breadcrumbs. A fresh DB must carry them all and record every version.
    db = Database(tmp_path / "p.db")
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(scheduler_state)")}
    for expected in (
        "lock_pid",
        "lock_heartbeat_at",
        "last_export_status",
        "last_export_error",
        "last_export_at",
        "last_replay_status",
        "last_replay_error",
        "last_replay_at",
    ):
        assert expected in cols
    versions = [
        r[0] for r in db.conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert versions == list(range(1, SCHEMA_VERSION + 1))
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
# audit-family mode vocabulary (issue #62)
# --------------------------------------------------------------------------


def _ai_input_kwargs(mode):
    return {
        "input_id": f"i-{mode}",
        "timestamp": _TS,
        "mode": mode,
        "run_id": "r1",
        "symbol": "BTC",
    }


def _ai_output_kwargs(mode):
    return {
        "output_id": f"o-{mode}",
        "timestamp": _TS,
        "mode": mode,
        "run_id": "r1",
        "symbol": "BTC",
        "decision_mode": "maintain_current",
        "risk_action": "approved",
        "order_created": False,
    }


def _decision_attempt_kwargs(mode):
    # scheduled_at varies with mode so the two legal inserts don't trip the
    # UNIQUE (run_id, scheduled_at) constraint.
    return {
        "decision_attempt_id": f"a-{mode}",
        "timestamp": _TS,
        "mode": mode,
        "run_id": "r1",
        "scheduled_at": _TS.replace(hour=1 if mode == "paper" else 2),
        "status": "in_progress",
    }


def _account_snapshot_kwargs(mode):
    return {
        "timestamp": _TS,
        "mode": mode,
        "run_id": "r1",
        "wallet_balance": Decimal("1000"),
        "account_equity": Decimal("1000"),
        "available_balance": Decimal("1000"),
        "realized_pnl": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "total_pnl": Decimal("0"),
        "total_fees": Decimal("0"),
        "net_funding_pnl": Decimal("0"),
        "total_position_notional": Decimal("0"),
        "used_initial_margin": Decimal("0"),
        "total_maintenance_margin": Decimal("0"),
    }


def _position_snapshot_kwargs(mode):
    return {
        "timestamp": _TS,
        "mode": mode,
        "run_id": "r1",
        "symbol": "BTC",
        "position_size": Decimal("0.01"),
        "side": "long",
        "mark_price": Decimal("60000"),
        "position_notional": Decimal("600"),
        "unrealized_pnl": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "maintenance_margin": Decimal("6"),
    }


_AUDIT_MODE_INSERTS = [
    ("ai_inputs", repo.insert_ai_input, _ai_input_kwargs),
    ("ai_outputs", repo.insert_ai_output, _ai_output_kwargs),
    ("decision_attempts", repo.insert_decision_attempt, _decision_attempt_kwargs),
    ("account_snapshots", repo.insert_account_snapshot, _account_snapshot_kwargs),
    ("position_snapshots", repo.insert_position_snapshot, _position_snapshot_kwargs),
]


@pytest.mark.parametrize(("table", "insert", "kwargs_for"), _AUDIT_MODE_INSERTS)
def test_audit_inserts_reject_an_out_of_vocabulary_mode(tmp_path, table, insert, kwargs_for):
    # Same write-boundary rule as insert_fill / insert_order / insert_run /
    # insert_funding_event: a future third assembly caller passing e.g.
    # mode="replay" must raise here, not be silently written where a reader
    # that splits these tables by mode would drop or misfile it.
    db = Database(tmp_path / "p.db")
    with pytest.raises(ValueError, match="mode must be one of"), db.transaction() as conn:
        insert(conn, **kwargs_for("replay"))
    assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize(("table", "insert", "kwargs_for"), _AUDIT_MODE_INSERTS)
def test_audit_inserts_accept_both_legal_modes(tmp_path, table, insert, kwargs_for):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        insert(conn, **kwargs_for("paper"))
        insert(conn, **kwargs_for("live"))
    modes = {r[0] for r in db.conn.execute(f"SELECT mode FROM {table}")}
    assert modes == {"paper", "live"}
    db.close()


def test_audit_insert_missing_mode_keeps_its_not_null_failure_shape(tmp_path):
    # The guard validates mode only when present — an omitted mode still fails
    # on the column's NOT NULL constraint exactly as it did before the guard
    # existed, not with a KeyError from the guard itself.
    db = Database(tmp_path / "p.db")
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        repo.insert_ai_input(conn, input_id="i1", timestamp=_TS, run_id="r1", symbol="BTC")
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
        repo.insert_ai_input(conn, **_ai_input_kwargs("paper"))
        repo.insert_ai_output(conn, **_ai_output_kwargs("paper"))
        repo.insert_order(
            conn,
            order_id="ord1",
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="paper_market",
            qty=Decimal("0.01"),
            status="filled",
            updated_at=_TS,
        )
        # effective_leverage is nullable and not part of the minimal row — pass
        # it here explicitly so its column name stays locked against the DDL.
        repo.insert_account_snapshot(
            conn, **_account_snapshot_kwargs("paper"), effective_leverage=Decimal("0")
        )
        repo.insert_position_snapshot(conn, **_position_snapshot_kwargs("paper"))
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


def test_set_funding_status_rejects_overriding_stored_mark(tmp_path):
    # The settlement mark is fixed when the pending row is stored; a backfill
    # posting may re-supply mark_price only if it MATCHES — never override it, or a
    # price move between pending and backfill would silently rewrite the basis.
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
            position_size=Decimal("0.05"),
            status="pending",
            mark_price=Decimal("60000"),
        )
    # A divergent mark is rejected at the boundary (its own internally-consistent
    # settlement math — 0.05 * 61000 — does not save it).
    with (
        pytest.raises(ValueError, match="immutable stored settlement mark"),
        db.transaction() as conn,
    ):
        repo.set_funding_status(
            conn,
            fid,
            status="posted",
            mark_price=Decimal("61000"),
            signed_position_notional=Decimal("3050"),
            funding_rate=Decimal("0.0001"),
            funding_pnl=Decimal("-0.305"),
        )
    # Re-supplying the SAME stored mark is allowed (it overrides nothing).
    with db.transaction() as conn:
        repo.set_funding_status(
            conn,
            fid,
            status="posted",
            mark_price=Decimal("60000"),
            signed_position_notional=Decimal("3000"),
            funding_rate=Decimal("0.0001"),
            funding_pnl=Decimal("-0.3"),
        )
    row = repo.get_funding_event(db.conn, fid)
    assert row["status"] == "posted"
    assert Decimal(row["mark_price"]) == Decimal("60000")
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


def test_scheduler_state_patch_semantics_for_v3_columns(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r1",
            lock_pid=1234,
            lock_heartbeat_at=_TS,
            last_export_status="ok",
            last_export_error=None,
            last_export_at=_TS,
        )
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["lock_pid"] == 1234
    assert row["lock_heartbeat_at"] == _TS.isoformat()
    assert row["last_export_status"] == "ok"
    assert row["last_export_error"] is None
    assert row["last_export_at"] == _TS.isoformat()
    # Omitted keywords preserve the stored value; supplied ones change it.
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn, "r1", last_export_status="failed", last_export_error="disk full"
        )
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["lock_pid"] == 1234  # lease untouched by an export update
    assert row["lock_heartbeat_at"] == _TS.isoformat()
    assert row["last_export_status"] == "failed"
    assert row["last_export_error"] == "disk full"
    # An explicit None is a deliberate clear, distinct from "not supplied".
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", lock_pid=None, lock_heartbeat_at=None)
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["lock_pid"] is None
    assert row["lock_heartbeat_at"] is None
    assert row["last_export_status"] == "failed"  # breadcrumbs untouched by release
    db.close()


def test_iter_other_run_leases_excludes_self_and_holderless_rows(tmp_path):
    # The run lease is keyed on run_id; the kill switch, updateLeverage and the
    # §19.3 sweep it protects are per-WALLET, and two runs in one store share a
    # wallet. This is the read that lets a caller see the SIBLING it would
    # trample. Three ways it could lie, all pinned here: reporting the caller's
    # own lease (every ordinary invocation would then refuse itself), reporting
    # a released lease (lock_pid NULL — the row survives release_run_lock, it is
    # only blanked), or reporting a row that records a pid with no heartbeat to
    # judge freshness by (the caller's staleness maths would crash on NULL).
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "self", lock_pid=1111, lock_heartbeat_at=_TS)
        repo.upsert_scheduler_state(conn, "sibling", lock_pid=2222, lock_heartbeat_at=_TS)
        repo.upsert_scheduler_state(conn, "released", lock_pid=None, lock_heartbeat_at=None)
        repo.upsert_scheduler_state(conn, "pid-no-beat", lock_pid=3333, next_decision_at=_TS)
    rows = repo.iter_other_run_leases(db.conn, "self")
    assert [(r["run_id"], r["lock_pid"]) for r in rows] == [("sibling", 2222)]
    assert rows[0]["lock_heartbeat_at"] == _TS.isoformat()  # freshness is the caller's call
    # Negative control on the exclusion itself: asked from the sibling's side,
    # "self" IS the other run — the filter is the run_id, not the row.
    assert [r["run_id"] for r in repo.iter_other_run_leases(db.conn, "sibling")] == ["self"]
    # And a store whose only lease is the caller's own reports nothing.
    solo = Database(tmp_path / "solo.db")
    with solo.transaction() as conn:
        repo.upsert_scheduler_state(conn, "only", lock_pid=1111, lock_heartbeat_at=_TS)
    assert repo.iter_other_run_leases(solo.conn, "only") == []
    solo.close()
    db.close()


def test_upsert_scheduler_state_rejects_bad_export_status(tmp_path):
    db = Database(tmp_path / "p.db")
    with pytest.raises(ValueError, match="last_export_status"), db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", last_export_status="bogus")
    assert repo.get_scheduler_state(db.conn, "r1") is None  # nothing written
    db.close()


def test_scheduler_state_replay_breadcrumbs_patch_and_enum(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r1",
            last_replay_status="mismatch",
            last_replay_error="pos BTC",
            last_replay_at=_TS,
        )
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["last_replay_status"] == "mismatch"
    assert row["last_replay_error"] == "pos BTC"
    assert row["last_replay_at"] == _TS.isoformat()
    # Patch semantics: an unrelated update leaves the replay breadcrumbs alone.
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", last_export_status="ok")
    row = repo.get_scheduler_state(db.conn, "r1")
    assert row["last_replay_status"] == "mismatch"
    # Vocabulary enforced at the write boundary, like its export sibling.
    with pytest.raises(ValueError, match="last_replay_status"), db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r1", last_replay_status="bogus")
    assert repo.get_scheduler_state(db.conn, "r1")["last_replay_status"] == "mismatch"
    db.close()


def test_decision_attempt_error_type_validated_at_write_boundary(tmp_path):
    db = Database(tmp_path / "p.db")
    aid = decision_attempt_id("r1", _TS)
    with db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id=aid,
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            scheduled_at=_TS,
            status="in_progress",
        )
    # The §6.2 vocabulary is exact — "Timeout" is a typo, not a variant.
    with pytest.raises(ValueError, match="error_type"), db.transaction() as conn:
        repo.update_decision_attempt(conn, aid, error_type="Timeout")
    with db.transaction() as conn:
        repo.update_decision_attempt(conn, aid, error_type="timeout")  # accepted
    assert repo.get_decision_attempt(db.conn, aid)["error_type"] == "timeout"
    with db.transaction() as conn:
        repo.update_decision_attempt(conn, aid, error_type=None)  # explicit clear
    assert repo.get_decision_attempt(db.conn, aid)["error_type"] is None
    # The insert path guards the same vocabulary.
    with pytest.raises(ValueError, match="error_type"), db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="other",
            timestamp=_TS,
            mode="paper",
            run_id="r1",
            scheduled_at=datetime(2026, 7, 1, 4, tzinfo=timezone.utc),
            status="in_progress",
            error_type="Timeout",
        )
    db.close()


def test_insert_funding_event_validates_source(tmp_path):
    db = Database(tmp_path / "p.db")
    base = {
        "mode": "paper",
        "run_id": "r1",
        "symbol": "BTC",
        "funding_timestamp": _TS,
        "position_size": Decimal("0.05"),
        "status": "pending",
        "mark_price": Decimal("60000"),
    }
    with pytest.raises(ValueError, match="source"), db.transaction() as conn:
        repo.insert_funding_event(conn, funding_event_id="f-bad", source="bogus", **base)
    with db.transaction() as conn:
        repo.insert_funding_event(conn, funding_event_id="f-ok", source="live_public_data", **base)
    assert repo.get_funding_event(db.conn, "f-ok")["source"] == "live_public_data"
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


# --------------------------------------------------------------------------
# order / plan patch-writers: fail-loud + patch semantics (PR3 engine seam)
# --------------------------------------------------------------------------


def _order_kwargs(order_id="ord1", **overrides):
    base = {
        "order_id": order_id,
        "timestamp": _TS,
        "mode": "paper",
        "run_id": "r1",
        "symbol": "BTC",
        "order_role": "entry",
        "side": "buy",
        "order_type": "paper_market",
        "qty": Decimal("0.01"),
        "status": "open",
        "remaining_qty": Decimal("0.01"),
    }
    base.update(overrides)
    return base


def test_update_order_rejects_missing_row(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn, pytest.raises(ValueError, match="does not exist"):
        repo.update_order(conn, "nope", status="filled")
    db.close()


def test_update_execution_plan_rejects_missing_row(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn, pytest.raises(ValueError, match="does not exist"):
        repo.update_execution_plan(conn, "nope", status="expired")
    db.close()


def test_set_position_protection_rejects_missing_row(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn, pytest.raises(ValueError, match="no current_positions"):
        repo.set_position_protection(
            conn, "r1", "BTC", stop_loss_price=None, take_profit_price=None
        )
    db.close()


def test_update_order_patch_preserves_unsupplied_columns(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_order(conn, **_order_kwargs(filled_qty=Decimal("0.004")))
        repo.update_order(conn, "ord1", status="canceled", status_reason="deadline")
    row = db.conn.execute("SELECT * FROM orders WHERE order_id = 'ord1'").fetchone()
    assert (row["status"], row["status_reason"]) == ("canceled", "deadline")
    # Omitted keywords are untouched — not nulled, not zeroed.
    assert Decimal(row["filled_qty"]) == Decimal("0.004")
    assert Decimal(row["remaining_qty"]) == Decimal("0.01")
    db.close()


def test_update_order_rejects_bad_status(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_order(conn, **_order_kwargs())
        with pytest.raises(ValueError, match="status"):
            repo.update_order(conn, "ord1", status="not_a_status")
    db.close()


# --------------------------------------------------------------------------
# restart-guard helpers: max_engine_seq + get_position_protection
# --------------------------------------------------------------------------


def test_max_engine_seq_scans_orders_plans_and_flip_ids(tmp_path):
    db = Database(tmp_path / "p.db")
    with db.transaction() as conn:
        repo.insert_order(conn, **_order_kwargs(order_id="r1:ord:3"))
        repo.insert_execution_plan(
            conn,
            plan_id="r1:plan:7",
            run_id="r1",
            symbol="BTC",
            status="completed",
            created_at=_TS,
            flip_plan_id="r1:flip:9",
        )
        # Another run's ids and non-engine ids are ignored.
        repo.insert_order(conn, **_order_kwargs(order_id="other:ord:99", run_id="r2"))
        repo.insert_order(conn, **_order_kwargs(order_id="manual-id"))
    assert repo.max_engine_seq(db.conn, "r1") == 9
    assert repo.max_engine_seq(db.conn, "fresh") == 0
    db.close()


def test_get_position_protection_distinguishes_missing_row_from_cleared(tmp_path):
    db = Database(tmp_path / "p.db")
    assert repo.get_position_protection(db.conn, "r1", "BTC") is None
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn, "r1", PositionState(coin="BTC", size=Decimal("0.01"), entry_price=Decimal(50000))
        )
    assert repo.get_position_protection(db.conn, "r1", "BTC") == (None, None)
    with db.transaction() as conn:
        repo.set_position_protection(
            conn,
            "r1",
            "BTC",
            stop_loss_price=Decimal("46250"),
            take_profit_price=Decimal("60000"),
        )
    assert repo.get_position_protection(db.conn, "r1", "BTC") == (
        Decimal("46250"),
        Decimal("60000"),
    )
    db.close()
