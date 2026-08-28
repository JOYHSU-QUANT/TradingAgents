"""Tests for the schema v6 live additions and their repository writers.

Covers the PR 2 persistence contract (phase3-spec §16): the v5→v6 upgrade
path, the fills dedupe UNIQUE, the cloid registry's idempotent/conflict
semantics, the live_order_attempts evidence trail, and the §18.5 kill switch
event log — plus the v9 exchange-liquidation mirror and its one writer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import (
    Database,
    SchemaVersionError,
    apply_migrations,
    connect,
    stored_schema_version,
)
from contrib.hyperliquid_perp.persistence.ids import live_order_attempt_id
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import MIGRATIONS, SCHEMA_VERSION

_NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
_HEX = "0x" + "ab" * 16
_HEX2 = "0x" + "cd" * 16


@pytest.fixture
def db():
    with Database(":memory:") as database:
        yield database


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# migration v6
# ---------------------------------------------------------------------------


def test_v5_store_upgrades_to_latest_in_place(monkeypatch):
    # Build a store that stops at v5 (an existing paper DB), then re-open with
    # the full migration list: the later migrations (v6, v7's additive ADD
    # COLUMN, and v8's new live_smoke_tests table) apply, and the paper rows
    # survive.
    conn = connect(":memory:")
    v5_only = {version: MIGRATIONS[version] for version in sorted(MIGRATIONS) if version <= 5}
    import contrib.hyperliquid_perp.persistence.db as db_module

    monkeypatch.setattr(db_module, "MIGRATIONS", v5_only)
    assert apply_migrations(conn) == 5
    # Every table v6 adds a column to gets a pre-migration row, not just
    # `orders`: seeding one table would let a mis-targeted ALTER on any of the
    # other four pass unnoticed. The paper run this migrates is live on a
    # server, so "the existing rows survive" is the whole point.
    stamp = "2026-07-12T00:00:00+00:00"
    conn.execute(
        "INSERT INTO orders (order_id, timestamp, mode, run_id, symbol, order_role,"
        " side, type, qty, status, updated_at)"
        f" VALUES ('o1', '{stamp}', 'paper', 'r', 'BTC', 'entry',"
        f" 'buy', 'paper_market', '1', 'filled', '{stamp}')"
    )
    conn.execute(
        "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol, side,"
        " fill_qty, fill_price, fill_notional, fee, fee_rate, realized_pnl_delta,"
        " liquidity_type)"
        f" VALUES ('f1', '{stamp}', 'paper', 'r', 'o1', 'BTC', 'buy',"
        " '1', '100', '100', '0.1', '0.001', '0', 'taker')"
    )
    # The two snapshot ids are INTEGER PRIMARY KEY AUTOINCREMENT — let SQLite
    # assign them.
    conn.execute(
        "INSERT INTO account_snapshots (timestamp, mode, run_id, wallet_balance,"
        " account_equity, available_balance, realized_pnl, unrealized_pnl, total_pnl,"
        " total_fees, net_funding_pnl, total_position_notional, used_initial_margin,"
        " total_maintenance_margin)"
        f" VALUES ('{stamp}', 'paper', 'r', '1000', '1000', '900', '0', '0', '0',"
        " '0', '0', '100', '100', '5')"
    )
    conn.execute(
        "INSERT INTO position_snapshots (timestamp, mode, run_id, symbol,"
        " position_size, side, mark_price, position_notional, unrealized_pnl, realized_pnl,"
        " maintenance_margin)"
        f" VALUES ('{stamp}', 'paper', 'r', 'BTC', '1', 'buy', '100', '100', '0', '0', '5')"
    )
    conn.execute(f"INSERT INTO scheduler_state (run_id, updated_at) VALUES ('r', '{stamp}')")

    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    assert apply_migrations(conn) == SCHEMA_VERSION

    row = conn.execute("SELECT * FROM orders").fetchone()
    assert row["order_id"] == "o1"
    assert row["cloid_hex"] is None  # new columns are nullable for old rows
    # The other four survive their ALTERs too, rows and all.
    for table, key, value in (
        ("fills", "fill_id", "f1"),
        ("account_snapshots", "run_id", "r"),
        ("position_snapshots", "symbol", "BTC"),
        ("scheduler_state", "run_id", "r"),
    ):
        surviving = conn.execute(f"SELECT * FROM {table}").fetchall()
        assert [r[key] for r in surviving] == [value], table
    # v8's new table actually landed on the upgraded (populated) connection —
    # the real Lightsail scenario — with the columns the repository writes, and
    # a result row round-trips through the repo layer.
    assert {
        "result_id",
        "run_id",
        "test_key",
        "test_number",
        "test_name",
        "status",
        "network",
        "dry_run",
        "detail",
        "error_message",
        "executed_at",
    } <= _columns(conn, "live_smoke_tests")
    repo.insert_smoke_test_result(
        conn,
        run_id="r",
        test_number=1,
        test_key="signed_client_init",
        test_name="signed client initialization",
        status="passed",
        network="testnet",
        executed_at=_NOW,
    )
    latest = repo.latest_smoke_test_results(conn, "r")
    assert latest["signed_client_init"]["status"] == "passed"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v6_creates_the_seven_live_tables(db):
    tables = {
        row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "exchange_reconciliation_events",
        "kill_switch_events",
        "live_order_attempts",
        "protection_order_events",
        "accounting_adjustment_events",
        "safe_mode_events",
        "cloid_registry",
    } <= tables


def test_v6_adds_the_spec_columns(db):
    assert {
        "cloid_logical",
        "cloid_hex",
        "exchange_status",
        "exchange_raw_status",
        "submitted_at",
        "acknowledged_at",
        "canceled_at",
        "cancel_reason",
        "is_bot_owned",
        "raw_exchange_payload_path",
    } <= _columns(db.conn, "orders")
    assert {
        "exchange_fill_key",
        "cloid_logical",
        "cloid_hex",
        "liquidity_role",
        "exchange_fee",
        "exchange_closed_pnl",
        "exchange_fill_time",
        "raw_exchange_payload_path",
    } <= _columns(db.conn, "fills")
    for table in ("account_snapshots", "position_snapshots"):
        assert {
            "exchange_raw_payload_path",
            "reconciliation_status",
            "reconciliation_diff",
        } <= _columns(db.conn, table)
    assert {
        "safe_mode_type",
        "safe_mode_reason",
        "safe_mode_entered_at",
        "day_start_equity",
        "day_start_date",
        "consecutive_loss_count",
    } <= _columns(db.conn, "scheduler_state")


def test_exchange_fill_key_is_unique_but_null_stays_distinct(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol, side,"
            " fill_qty, fill_price, fill_notional, fee, fee_rate, realized_pnl_delta,"
            " liquidity_type, exchange_fill_key)"
            " VALUES ('f1', 't', 'live', 'r', 'o1', 'BTC', 'buy', '1', '10', '10', '0',"
            " '0', '0', 'taker', 'tid-1')"
        )
        # Paper fills carry no key: two NULLs must not collide (§14.2 is
        # live-only; the guard must not break Phase 2 rows).
        for fid in ("f2", "f3"):
            conn.execute(
                "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol,"
                " side, fill_qty, fill_price, fill_notional, fee, fee_rate,"
                " realized_pnl_delta, liquidity_type)"
                " VALUES (?, 't', 'paper', 'r', 'o1', 'BTC', 'buy', '1', '10', '10',"
                " '0', '0', '0', 'simulated')",
                (fid,),
            )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol,"
            " side, fill_qty, fill_price, fill_notional, fee, fee_rate,"
            " realized_pnl_delta, liquidity_type, exchange_fill_key)"
            " VALUES ('f4', 't', 'live', 'r', 'o1', 'BTC', 'buy', '1', '10', '10',"
            " '0', '0', '0', 'taker', 'tid-1')"
        )


# ---------------------------------------------------------------------------
# cloid_registry
# ---------------------------------------------------------------------------


def _register(conn, *, logical="log-1", hex_id=_HEX):
    repo.insert_cloid_mapping(
        conn,
        cloid_logical=logical,
        cloid_hex=hex_id,
        run_id="r",
        symbol="BTC",
        order_role="entry",
        created_at=_NOW,
    )


def test_cloid_mapping_is_idempotent_for_the_same_pair(db):
    with db.transaction() as conn:
        _register(conn)
        _register(conn)  # §8.3 rule 1: a retry re-registers the same pair
    row = repo.get_cloid_by_hex(db.conn, _HEX)
    assert row["cloid_logical"] == "log-1"
    assert repo.get_cloid_by_logical(db.conn, "log-1")["cloid_hex"] == _HEX
    count = db.conn.execute("SELECT COUNT(*) FROM cloid_registry").fetchone()[0]
    assert count == 1


def test_cloid_mapping_conflict_fails_loud(db):
    with db.transaction() as conn:
        _register(conn)
    with pytest.raises(ValueError, match="conflict"), db.transaction() as conn:
        _register(conn, logical="other-logical")  # same hex, new logical
    with pytest.raises(ValueError, match="conflict"), db.transaction() as conn:
        _register(conn, hex_id=_HEX2)  # same logical, new hex


def test_cloid_mapping_rejects_unknown_role(db):
    with pytest.raises(ValueError, match="order_role"), db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical="l",
            cloid_hex=_HEX,
            run_id="r",
            symbol="BTC",
            order_role="yolo",
        )


def test_unknown_hex_lookup_returns_none(db):
    assert repo.get_cloid_by_hex(db.conn, _HEX2) is None


# ---------------------------------------------------------------------------
# live_order_attempts
# ---------------------------------------------------------------------------


def _insert_attempt(conn, *, index=0, status="submitted", action="place"):
    repo.insert_live_order_attempt(
        conn,
        attempt_id=live_order_attempt_id("r", action, _HEX, index),
        run_id="r",
        action=action,
        symbol="BTC",
        attempt_index=index,
        status=status,
        order_id="o1",
        cloid_logical="log-1",
        cloid_hex=_HEX,
        side="buy",
        qty=Decimal("0.01"),
        price=Decimal("100"),
        reduce_only=False,
        order_role="entry",
        requested_at=_NOW,
    )


def test_attempt_roundtrip_and_outcome_patch(db):
    attempt_id = live_order_attempt_id("r", "place", _HEX, 0)
    with db.transaction() as conn:
        _insert_attempt(conn)
    row = repo.get_live_order_attempt(db.conn, attempt_id)
    assert row["status"] == "submitted"
    assert row["acknowledged_at"] is None
    with db.transaction() as conn:
        repo.update_live_order_attempt(
            conn,
            attempt_id,
            status="acknowledged",
            exchange_order_id="123",
            exchange_status="filled",
            raw_exchange_payload_path="/tmp/x.json",
            acknowledged_at=_NOW,
        )
    row = repo.get_live_order_attempt(db.conn, attempt_id)
    assert row["status"] == "acknowledged"
    assert row["exchange_order_id"] == "123"
    assert row["acknowledged_at"] == _NOW.isoformat()


def test_attempt_patch_requires_an_existing_row(db):
    with pytest.raises(ValueError, match="does not exist"), db.transaction() as conn:
        repo.update_live_order_attempt(conn, "nope", status="failed")


def test_settled_attempt_is_immutable(db):
    attempt_id = live_order_attempt_id("r", "place", _HEX, 0)
    with db.transaction() as conn:
        _insert_attempt(conn)
        repo.update_live_order_attempt(conn, attempt_id, status="acknowledged")
    # The §8.3 pre-send check trusts settled statuses; rewriting one would
    # falsify the evidence trail — a retry is a NEW attempt row.
    with pytest.raises(ValueError, match="immutable"), db.transaction() as conn:
        repo.update_live_order_attempt(conn, attempt_id, status="failed")


def test_attempt_retry_is_a_new_row_never_an_overwrite(db):
    with db.transaction() as conn:
        _insert_attempt(conn, index=0)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        _insert_attempt(conn, index=0)  # same (cloid, action, index)
    with db.transaction() as conn:
        _insert_attempt(conn, index=1)  # the retry
    rows = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [r["attempt_index"] for r in rows] == [0, 1]


def test_next_attempt_index_resumes_above_persisted_max(db):
    assert repo.next_live_attempt_index(db.conn, action="place", cloid_hex=_HEX) == 0
    with db.transaction() as conn:
        _insert_attempt(conn, index=0)
        _insert_attempt(conn, index=1)
    assert repo.next_live_attempt_index(db.conn, action="place", cloid_hex=_HEX) == 2
    # Another action namespace counts separately.
    assert repo.next_live_attempt_index(db.conn, action="cancel_by_cloid", cloid_hex=_HEX) == 0


def test_next_attempt_index_spans_runs(db):
    # The UNIQUE (cloid_hex, action, attempt_index) namespace has no run
    # column, so the allocator must not filter by run either: a later run's
    # shutdown sweep cancels an earlier run's surviving order, and a
    # run-scoped MAX() would re-derive an index the constraint already holds.
    with db.transaction() as conn:
        repo.insert_live_order_attempt(
            conn,
            attempt_id=live_order_attempt_id("r1", "cancel_by_cloid", _HEX, 0),
            run_id="r1",
            action="cancel_by_cloid",
            symbol="BTC",
            attempt_index=0,
            cloid_logical="log-1",
            cloid_hex=_HEX,
            requested_at=_NOW,
        )
    next_index = repo.next_live_attempt_index(db.conn, action="cancel_by_cloid", cloid_hex=_HEX)
    assert next_index == 1
    # r2's insert under the allocated index lands — no IntegrityError.
    with db.transaction() as conn:
        repo.insert_live_order_attempt(
            conn,
            attempt_id=live_order_attempt_id("r2", "cancel_by_cloid", _HEX, next_index),
            run_id="r2",
            action="cancel_by_cloid",
            symbol="BTC",
            attempt_index=next_index,
            cloid_logical="log-1",
            cloid_hex=_HEX,
            requested_at=_NOW,
        )
    rows = db.conn.execute(
        "SELECT run_id, attempt_index FROM live_order_attempts ORDER BY rowid"
    ).fetchall()
    assert [(r["run_id"], r["attempt_index"]) for r in rows] == [("r1", 0), ("r2", 1)]


def test_attempt_field_presence_rules(db):
    with db.transaction() as conn:
        with pytest.raises(ValueError, match="cloid pair"):
            repo.insert_live_order_attempt(
                conn,
                attempt_id="a1",
                run_id="r",
                action="place",
                symbol="BTC",
                attempt_index=0,
            )
        with pytest.raises(ValueError, match="exchange_order_id"):
            repo.insert_live_order_attempt(
                conn,
                attempt_id="a2",
                run_id="r",
                action="cancel",
                symbol="BTC",
                attempt_index=0,
            )
        with pytest.raises(ValueError, match="together"):
            repo.insert_live_order_attempt(
                conn,
                attempt_id="a3",
                run_id="r",
                action="place",
                symbol="BTC",
                attempt_index=0,
                cloid_hex=_HEX,  # hex without logical
            )


# ---------------------------------------------------------------------------
# kill_switch_events
# ---------------------------------------------------------------------------


def test_kill_switch_events_roundtrip_in_order(db):
    with db.transaction() as conn:
        repo.insert_kill_switch_event(
            conn, run_id="r", event_type="kill_switch_armed", detail="deadline=120s"
        )
        repo.insert_kill_switch_event(
            conn, run_id="r", event_type="kill_switch_refresh_failed", error_message="timeout"
        )
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert [e["event_type"] for e in events] == [
        "kill_switch_armed",
        "kill_switch_refresh_failed",
    ]
    assert events[1]["error_message"] == "timeout"


def test_kill_switch_event_vocabulary_is_enforced(db):
    with pytest.raises(ValueError, match="event_type"), db.transaction() as conn:
        repo.insert_kill_switch_event(conn, run_id="r", event_type="not_an_event")


# ---------------------------------------------------------------------------
# orders live columns
# ---------------------------------------------------------------------------


def _insert_live_order(conn, **overrides):
    fields = {
        "order_id": "o1",
        "mode": "live",
        "run_id": "r",
        "symbol": "BTC",
        "order_role": "entry",
        "side": "buy",
        "order_type": "ioc_limit",
        "qty": Decimal("0.01"),
        "status": "submitted",
        "price": Decimal("100"),
        "cloid_logical": "log-1",
        "cloid_hex": _HEX,
        "submitted_at": _NOW,
        "is_bot_owned": True,
        "timestamp": _NOW,
    }
    fields.update(overrides)
    repo.insert_order(conn, **fields)


def test_live_order_roundtrip_with_ack_backfill(db):
    with db.transaction() as conn:
        _insert_live_order(conn)
    with db.transaction() as conn:
        repo.update_order(
            conn,
            "o1",
            status="filled",
            exchange_order_id="777",
            exchange_status="filled",
            acknowledged_at=_NOW,
            updated_at=_NOW,
        )
    row = repo.get_order(db.conn, "o1")
    assert row["exchange_order_id"] == "777"
    assert row["exchange_status"] == "filled"
    assert row["acknowledged_at"] == _NOW.isoformat()
    assert row["is_bot_owned"] == 1
    assert row["cloid_hex"] == _HEX


def test_order_cloid_pair_travels_together(db):
    with pytest.raises(ValueError, match="together"), db.transaction() as conn:
        _insert_live_order(conn, cloid_logical=None)


def test_orders_cloid_hex_is_unique_but_null_stays_distinct(db):
    # One orders row per cloid (schema v6 idx_orders_cloid_hex): the registry
    # pins the pair, this pins the row count. Paper rows carry no cloid, so
    # NULLs must not collide — same posture as the fills dedupe key.
    with db.transaction() as conn:
        _insert_live_order(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        _insert_live_order(conn, order_id="o2")  # same cloid_hex
    with db.transaction() as conn:
        for oid in ("p1", "p2"):
            _insert_live_order(conn, order_id=oid, cloid_logical=None, cloid_hex=None)


def test_live_order_roles_accepted(db):
    for i, role in enumerate(("close", "emergency_close", "cleanup_cancel")):
        with db.transaction() as conn:
            _insert_live_order(
                conn,
                order_id=f"role-{i}",
                order_role=role,
                cloid_logical=f"log-{role}",
                cloid_hex=f"0x{i:032x}",
            )
    assert repo.get_order(db.conn, "role-0")["order_role"] == "close"


# ---------------------------------------------------------------------------
# review round 7: write-boundary vocabulary + evidence completeness
# ---------------------------------------------------------------------------


def _seed_order(db, *, order_id="o1", status="open", cloid_hex=_HEX):
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id=order_id,
            mode="live",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("0.01"),
            status=status,
            price=Decimal("100"),
            cloid_logical=f"log-{order_id}",
            cloid_hex=cloid_hex,
            is_bot_owned=True,
            timestamp=_NOW,
        )


def test_exchange_status_must_be_a_normalized_family_word(db):
    # §16.1: exchange_status carries the NORMALIZED family (the orders.status
    # words); exchange_raw_status carries the verbatim wire word. The ack path
    # holds BOTH at one call site, and the same-named parameter on
    # update_live_order_attempt takes the OPPOSITE vocabulary — so writing
    # "resting"/"error" into the normalized column is one keystroke away, and it
    # produces a plausible row that PR 4's reconciliation would never match.
    _seed_order(db)
    for raw_word in ("resting", "error"):
        with pytest.raises(ValueError, match="exchange_status"), db.transaction() as conn:
            repo.update_order(conn, "o1", exchange_status=raw_word)
    # The four families are accepted; the raw column stays free-form.
    with db.transaction() as conn:
        repo.update_order(conn, "o1", exchange_status="open", exchange_raw_status="resting")
    row = repo.get_order(db.conn, "o1")
    assert (row["exchange_status"], row["exchange_raw_status"]) == ("open", "resting")


def test_a_place_attempt_must_carry_the_parameters_it_sent(db):
    # The attempt row is written BEFORE the network call, so it is the only
    # record of the intent if the process dies inside the send window. §8.3
    # recovery and PR 4's reconciliation compare that intent against the
    # exchange — an attempt with NULL side/qty/price cannot support either.
    with pytest.raises(ValueError, match="order parameters"), db.transaction() as conn:
        repo.insert_live_order_attempt(
            conn,
            attempt_id="a1",
            run_id="r",
            action="place",
            symbol="BTC",
            attempt_index=0,
            cloid_logical="log-1",
            cloid_hex=_HEX,
            requested_at=_NOW,
        )


def test_the_place_predicates_are_not_scoped_to_one_run(db):
    # PR 4 reconciles orders it found on the EXCHANGE, which may belong to an
    # EARLIER run. A run-scoped predicate would answer False there — "not this
    # run" reading as "never sent" — licensing exactly the resend §8.3 rule 10
    # exists to forbid. One evidence trail, one scope.
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical="log-old",
            cloid_hex=_HEX,
            run_id="r-old",
            symbol="BTC",
            order_role="entry",
        )
        repo.insert_live_order_attempt(
            conn,
            attempt_id="a-old",
            run_id="r-old",
            action="place",
            symbol="BTC",
            attempt_index=0,
            status="acknowledged",
            cloid_logical="log-old",
            cloid_hex=_HEX,
            side="buy",
            qty=Decimal("0.01"),
            price=Decimal("100"),
            order_role="entry",
            requested_at=_NOW,
        )
    # Asked from a DIFFERENT run, the evidence still stands.
    assert repo.has_place_attempt(db.conn, cloid_hex=_HEX) is True
    assert repo.has_exchange_known_cloid(db.conn, cloid_hex=_HEX) is True


def test_iter_open_live_orders_sees_only_non_terminal_live_rows(db):
    # The disarm cross-check's input: what SQLite still believes may be resting.
    _seed_order(db, order_id="live1", status="open", cloid_hex=_HEX)
    _seed_order(db, order_id="done1", status="filled", cloid_hex=_HEX2)
    assert [r["order_id"] for r in repo.iter_open_live_orders(db.conn)] == ["live1"]


# ---------------------------------------------------------------------------
# migration v9 + the exchange liquidation mirror
# ---------------------------------------------------------------------------


def test_v8_store_upgrades_to_v9_in_place(monkeypatch):
    # Same shape as the v5 upgrade above, one version narrower: the paper run
    # live on the server is already at v8, so v9's additive ADD COLUMN is what
    # its next restart actually runs. Its existing position row must survive
    # and read NULL — a paper run has no exchange estimate to mirror, and a
    # NOT NULL column would have made the upgrade unrunnable.
    conn = connect(":memory:")
    v8_only = {version: MIGRATIONS[version] for version in sorted(MIGRATIONS) if version <= 8}
    import contrib.hyperliquid_perp.persistence.db as db_module

    monkeypatch.setattr(db_module, "MIGRATIONS", v8_only)
    assert apply_migrations(conn) == 8
    assert "exchange_liquidation_price" not in _columns(conn, "current_positions")
    stamp = "2026-07-12T00:00:00+00:00"
    conn.execute(
        "INSERT INTO current_positions (run_id, symbol, size, entry_price, realized_pnl,"
        f" updated_at) VALUES ('r', 'BTC', '0.01', '50000', '3', '{stamp}')"
    )

    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    assert apply_migrations(conn) == SCHEMA_VERSION

    assert "exchange_liquidation_price" in _columns(conn, "current_positions")
    row = conn.execute("SELECT * FROM current_positions").fetchone()
    assert (row["symbol"], row["size"], row["realized_pnl"]) == ("BTC", "0.01", "3")
    assert row["exchange_liquidation_price"] is None
    # And the reader hands that NULL back as None rather than tripping on it.
    upgraded = repo.get_current_position(conn, "r", "BTC")
    assert upgraded is not None and upgraded.liquidation_price is None
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_freshly_created_store_has_the_liquidation_column(db):
    # The ADD COLUMN path and the CREATE path must agree: a brand-new live run
    # never replays v9's ALTER against a v8 table, it runs the whole list.
    assert "exchange_liquidation_price" in _columns(db.conn, "current_positions")


def _seed_position(db, *, size="0.01", entry="50000"):
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal(size), entry_price=Decimal(entry)),
            updated_at=_NOW,
        )


def test_liquidation_mirror_round_trips_and_a_none_clears_it(db):
    _seed_position(db)
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    stored = repo.get_current_position(db.conn, "r", "BTC")
    assert stored is not None and stored.liquidation_price == Decimal("43210.5")
    # The reconciler writes an explicit None when the exchange reports flat (or
    # no liquidationPx at all); a stale estimate surviving that would band the
    # next position's SL off a dead number.
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", None)
    cleared = repo.get_current_position(db.conn, "r", "BTC")
    assert cleared is not None and cleared.liquidation_price is None


def test_liquidation_mirror_on_a_missing_row_is_a_silent_no_op(db):
    # Unlike set_position_protection, a missing row is NOT an error here: the
    # exchange can report a position the local books have not booked yet, and
    # that mismatch is the reconciler's own §12.3 lane, not this writer's. It
    # must also not FABRICATE the row — a current_positions row nobody's fills
    # created would read as a position the run opened.
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    assert repo.get_current_position(db.conn, "r", "BTC") is None
    assert db.conn.execute("SELECT COUNT(*) FROM current_positions").fetchone()[0] == 0


def test_upsert_current_position_preserves_the_mirrored_liquidation_price(db):
    # The invariant upsert_current_position's docstring promises, same posture
    # as the SL/TP pair: a position-changing fill lands between two reconcile
    # passes and must not wipe the mirror, or the live SL band silently falls
    # back to the entry-based one until the next heartbeat.
    _seed_position(db)
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.02"), entry_price=Decimal("51000")),
            updated_at=_NOW,
        )
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None
    assert pos.size == Decimal("0.02")  # the fill landed
    assert pos.liquidation_price == Decimal("43210.5")  # and the mirror survived it


def test_a_flip_clears_the_mirrored_liquidation_price(db):
    # The other half of that invariant, and the reason it is not "always
    # preserve": the estimate describes a DIRECTION. Carried across a flip it
    # sits on the wrong side of the new entry, and stops.stop_loss_decision
    # reads a wrong-side liq as `liquidation_too_close` → CLOSE_NOW → a §17.2
    # emergency close of a position that was never in danger, plus the §13.5
    # manual latch a human has to clear.
    _seed_position(db)
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("-0.01"), entry_price=Decimal("51000")),
            updated_at=_NOW,
        )
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None
    assert pos.size == Decimal("-0.01")  # the flip landed
    assert pos.liquidation_price is None  # and took the long's estimate with it


def test_a_flatten_clears_the_mirrored_liquidation_price(db):
    # Close-and-reopen is the same hazard as a flip: an estimate from before the
    # flat describes a position that no longer exists. The reconciler also
    # clears on a flat exchange, but only on its next pass — this closes the
    # ticks in between, where the closing fill is booked and no pass has run.
    _seed_position(db)
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_NOW)
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.size == 0
    assert pos.liquidation_price is None


# ---------------------------------------------------------------------------
# schema-version safety (2026-07-30 migration review)
# ---------------------------------------------------------------------------


def test_stored_schema_version_reads_the_store_without_applying_anything():
    # The whole point of this reader: a caller must be able to ask "what version
    # is this store?" BEFORE deciding whether opening it is safe. If it migrated
    # as a side effect (or needed the schema to already exist) the read-only
    # commands could not use it as their gate.
    conn = connect(":memory:")
    assert stored_schema_version(conn) == 0  # brand-new store: nothing applied
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"schema_migrations"}  # its own bookkeeping table and NOTHING else
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert stored_schema_version(conn) == SCHEMA_VERSION  # now the max recorded version
    conn.close()


def test_a_store_migrated_by_a_newer_build_is_refused_not_written_through():
    # The rollback hazard this exists for: an OLDER binary opening a store a
    # NEWER one already migrated used to migrate-nothing and carry on writing
    # through it, because nothing but apply_migrations reads schema_migrations.
    # Its SQL does not know the newer columns (a pre-v9 upsert_current_position
    # has no exchange_liquidation_price clause), so a stale mirrored liquidation
    # survives a flip/flatten the current build clears — §17 then bands off a
    # liquidation belonging to a position that no longer exists.
    conn = connect(":memory:")
    assert apply_migrations(conn) == SCHEMA_VERSION
    future = max(MIGRATIONS) + 1
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (future, "2026-07-30T00:00:00+00:00"),
    )
    with pytest.raises(SchemaVersionError, match=f"v{future}"):
        apply_migrations(conn)
    # Negative control: with that row gone the very same call is a clean no-op —
    # the refusal is keyed on the recorded version, not on re-opening at all.
    conn.execute("DELETE FROM schema_migrations WHERE version = ?", (future,))
    assert apply_migrations(conn) == SCHEMA_VERSION
    conn.close()


def test_a_read_only_open_of_an_up_to_date_store_applies_nothing(tmp_path):
    # migrate=False must still be a perfectly ordinary open for the normal case
    # (validate / export / live-smoke --gate-status against a current store):
    # it opens, and it writes NO migration bookkeeping of its own.
    path = tmp_path / "live.db"
    with Database(path) as built:
        before = [
            tuple(row)
            for row in built.conn.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            )
        ]
    with Database(path, migrate=False) as reopened:
        assert stored_schema_version(reopened.conn) == SCHEMA_VERSION
        after = [
            tuple(row)
            for row in reopened.conn.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            )
        ]
    assert after == before  # same versions AND same applied_at stamps: nothing re-ran


def test_a_read_only_open_refuses_a_behind_store_instead_of_upgrading_it(tmp_path, monkeypatch):
    # The other direction, and the one that bites on the deploy box: a read-style
    # command takes NO run lease, so migrating here would silently upgrade a
    # store a RUNNING daemon owns and leave that daemon writing through a schema
    # it does not know. Refuse and tell the operator instead.
    import contrib.hyperliquid_perp.persistence.db as db_module

    path = tmp_path / "behind.db"
    behind = sorted(MIGRATIONS)[-2]
    older = {version: MIGRATIONS[version] for version in sorted(MIGRATIONS) if version <= behind}
    monkeypatch.setattr(db_module, "MIGRATIONS", older)
    with Database(path) as built:
        assert stored_schema_version(built.conn) == behind
        # The LATEST migration's column (v10: ai_inputs.context_shape) is what
        # a one-behind store must lack — re-point this when a new version lands.
        assert "context_shape" not in _columns(built.conn, "ai_inputs")

    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)
    with pytest.raises(SchemaVersionError, match="will not migrate"):
        Database(path, migrate=False)

    # The refusal is not just an exception AFTER the fact: the store on disk is
    # byte-for-byte the version it was, so the daemon that owns it is unharmed.
    conn = connect(path)
    assert stored_schema_version(conn) == behind
    assert "context_shape" not in _columns(conn, "ai_inputs")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version > ?", (behind,)
        ).fetchone()[0]
        == 0
    )
    conn.close()

    # Negative control: the migrating open — the lane an operator reaches through
    # a command that DOES own the run — upgrades it exactly as before.
    with Database(path) as upgraded:
        assert stored_schema_version(upgraded.conn) == SCHEMA_VERSION
        assert "context_shape" in _columns(upgraded.conn, "ai_inputs")


def test_a_deferred_open_owes_an_upgrade_only_when_the_store_is_not_current(tmp_path, monkeypatch):
    # Issue #129: the handle itself says whether the deferred upgrade is still
    # owed, so an owning command's "before I migrate" guards (the sibling
    # lease check) fire only when a migration would actually run. Behind →
    # owed until paid; current → nothing owed; ahead → owed, and paying it
    # refuses by name and leaves the debt standing.
    import contrib.hyperliquid_perp.persistence.db as db_module

    path = tmp_path / "deferred.db"
    older = {v: MIGRATIONS[v] for v in sorted(MIGRATIONS)[:-1]}
    monkeypatch.setattr(db_module, "MIGRATIONS", older)
    Database(path).close()
    monkeypatch.setattr(db_module, "MIGRATIONS", MIGRATIONS)

    with Database(path, migrate=False, defer_migration=True) as behind:
        assert behind.migration_pending is True
        behind.apply_deferred_migration()
        assert behind.migration_pending is False
        assert stored_schema_version(behind.conn) == SCHEMA_VERSION
    with Database(path, migrate=False, defer_migration=True) as current:
        assert current.migration_pending is False
        current.apply_deferred_migration()  # nothing owed: a no-op, no raise

    with Database(path) as db, db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2099-01-01T00:00:00+00:00"),
        )
    with Database(path, migrate=False, defer_migration=True) as ahead:
        assert ahead.migration_pending is True
        with pytest.raises(SchemaVersionError, match="NEWER build"):
            ahead.apply_deferred_migration()
        assert ahead.migration_pending is True


# ---------------------------------------------------------------------------
# atomic §12.3 case stamping (2026-07-30 concurrency review)
# ---------------------------------------------------------------------------


def _open_case(db, *, exchange_value="unparsed-deadbeef") -> int:
    """One un-actioned §12.3 case — the shape `safe-mode --stamp-case` targets."""
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_malformed",
            exchange_value=exchange_value,
            timestamp=_NOW,
        )
    return db.conn.execute("SELECT MAX(event_id) FROM exchange_reconciliation_events").fetchone()[0]


def _action_of(db, event_id: int):
    return db.conn.execute(
        "SELECT action_taken FROM exchange_reconciliation_events WHERE event_id = ?", (event_id,)
    ).fetchone()["action_taken"]


def test_stamping_an_open_case_stamps_it_and_says_so(db):
    event_id = _open_case(db)
    with db.transaction() as conn:
        assert repo.stamp_reconciliation_action_if_unset(
            conn, event_id, "human: payload was garbage"
        )
    assert _action_of(db, event_id) == "human: payload was garbage"


def test_stamping_a_case_another_writer_already_disposed_of_preserves_the_original(db):
    # THE race: the operator's `--stamp-case` holds no run lease, so between its
    # "is this still open?" read and its write the daemon's own reconciliation
    # pass can stamp the MACHINE disposition — what the system actually DID about
    # the sighting. A plain UPDATE would erase that with no error and no audit
    # row. Putting the NULL test in the UPDATE's WHERE makes the loser of the
    # race get told instead of silently winning.
    event_id = _open_case(db)
    with db.transaction() as conn:
        repo.set_reconciliation_action(conn, event_id, "resolved_fill_booked")
    with db.transaction() as conn:
        assert (
            repo.stamp_reconciliation_action_if_unset(conn, event_id, "human: looked at it")
            is False
        )
    assert _action_of(db, event_id) == "resolved_fill_booked"  # the daemon's record survived

    # Negative control — the two writers differ on purpose: the daemon keeps the
    # overwriting writer, because revising its own disposition is legitimate
    # there and it already does read/test/write inside one transaction.
    with db.transaction() as conn:
        repo.set_reconciliation_action(conn, event_id, "resolved_manual")
    assert _action_of(db, event_id) == "resolved_manual"


def test_stamping_a_nonexistent_case_raises_rather_than_reporting_a_lost_race(db):
    # False means "someone else disposed of it"; a bad event_id is a different
    # fact entirely (a typo'd id, a wrong store) and must not be dressed as a
    # lost race — the CLI prints a completely different remedy for each.
    with db.transaction() as conn, pytest.raises(ValueError, match="does not exist"):
        repo.stamp_reconciliation_action_if_unset(conn, 4242, "human: looked at it")


def test_stamping_with_a_blank_action_is_refused(db):
    # The audit row's whole value is what the human attested; a blank (or
    # whitespace) disposition would clear the verdict block while recording
    # nothing — the same guard set_reconciliation_action carries.
    event_id = _open_case(db)
    for blank in ("", "   "):
        with db.transaction() as conn, pytest.raises(ValueError, match="non-empty"):
            repo.stamp_reconciliation_action_if_unset(conn, event_id, blank)
    assert _action_of(db, event_id) is None  # and the case stays open, not half-stamped


# ---------------------------------------------------------------------------
# the once-per-fact guard vs a fact that recurs (issue #65)
# ---------------------------------------------------------------------------

_ORDER_KEY = "0x" + "ab" * 16


def _sight_order_case(db, *, action_taken=None, detail=None) -> bool:
    """One sweep sighting under a fixed order fact key; True iff a row was written."""
    with db.transaction() as conn:
        return repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="pre_cycle",
            case_type="order_missing_on_exchange",
            exchange_value=_ORDER_KEY,
            action_taken=action_taken,
            detail=detail,
            timestamp=_NOW,
        )


def _order_cases(db):
    return repo.iter_exchange_reconciliation_events(
        db.conn, "r", case_type="order_missing_on_exchange"
    )


def test_a_provisionally_disposed_key_records_its_next_occurrence(db):
    # A disposition in PROVISIONAL_DISPOSITIONS ends an episode the sweep can
    # find itself facing again — how it comes back differs per member, and the
    # one below is the plainest: settled as never-sent, then re-sent under §8.3
    # rule 5, then answered with the rule-10 fault, which is far worse than the
    # first sighting. While a stamped key stayed shut, that occurrence was
    # written NOWHERE: neither `safe-mode --status` nor §21.4's unresolved count
    # would show it.
    assert _sight_order_case(db, action_taken="settled_never_sent", detail="first episode")
    assert not repo.has_exchange_reconciliation_case(
        db.conn, "r", case_type="order_missing_on_exchange", exchange_value=_ORDER_KEY
    )
    assert _sight_order_case(db, detail="rule 10: the exchange took the cloid and denies it")
    rows = _order_cases(db)
    assert len(rows) == 2
    assert rows[-1]["action_taken"] is None  # and the new one is OPEN, which is the point
    assert "rule 10" in rows[-1]["detail"]

    # Reopening the key does not disarm the guard: the new occurrence is
    # unresolved, so the fact is back to one row however often it is re-observed.
    # This is why the guard weighs the LATEST row — weighing the settled first
    # one would answer every later pass with another row.
    for _ in range(3):
        assert not _sight_order_case(db, detail="still unresolved")
    assert len(_order_cases(db)) == 2


def test_an_unresolved_key_still_swallows_its_re_sighting(db):
    # The guard's original job, untouched: an unhealed fact re-observed pass
    # after pass is one row, not one per pass.
    assert _sight_order_case(db, detail="first")
    for _ in range(3):
        assert not _sight_order_case(db, detail="again")
    assert len(_order_cases(db)) == 1


def test_a_human_disposition_shuts_the_key_for_good(db):
    # THE reason the line is drawn at the disposition rather than at the
    # case_type: a §8.3 rule-10 order stays in the sweep's cursor indefinitely,
    # so it is re-observed every pass, and `--stamp-case` is the operator's only
    # way to clear it. Were their answer treated as provisional, every pass
    # thereafter would answer it with a fresh row — the re-sighting flood the
    # guard exists to stop.
    assert _sight_order_case(db, detail="rule 10, unresolvable here")
    event_id = _order_cases(db)[0]["event_id"]
    with db.transaction() as conn:
        repo.stamp_reconciliation_action_if_unset(conn, event_id, "human: venue ticket 4471")
    for _ in range(3):
        assert not _sight_order_case(db, detail="still the same fault")
    assert len(_order_cases(db)) == 1


@pytest.mark.parametrize("disposition", ["resolved_fill_booked", "local_row_backfilled"])
def test_a_machine_disposition_for_a_fact_that_cannot_return_shuts_the_key(db, disposition):
    # Not every machine stamp is provisional, and these two are the negative
    # control: nothing DELETEs a fills row or an orders row, so the facts they
    # dispose of ("the ledger lacks this fill", "there is no local row") cannot
    # come back. A re-sighting of one is the SAME fact, and minting a row for it
    # on every backfill pass is exactly the flood.
    assert _sight_order_case(db, action_taken=disposition)
    assert not _sight_order_case(db, detail="re-observed")
    assert len(_order_cases(db)) == 1


def test_the_case_lookup_answers_with_the_live_occurrence_not_the_settled_one(db):
    # Every disposition stamp goes through this lookup, and once a key can hold
    # more than one row, aiming it at the FIRST would re-close a finished
    # episode and leave the live one open — silently, both rows being genuine.
    assert _sight_order_case(db, action_taken="settled_never_sent")
    assert _sight_order_case(db, detail="the occurrence after the resend")
    latest = repo.get_exchange_reconciliation_case(
        db.conn, "r", case_type="order_missing_on_exchange", exchange_value=_ORDER_KEY
    )
    assert latest["action_taken"] is None
    assert latest["event_id"] == max(row["event_id"] for row in _order_cases(db))
