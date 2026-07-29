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
from contrib.hyperliquid_perp.persistence.db import Database, apply_migrations, connect
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
