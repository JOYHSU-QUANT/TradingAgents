"""The ``status`` reading model: what it reads, how the document is shaped, what it must not carry.

The document's field paths are pinned here against ``SCHEMA_VERSION``; the
plan's security rules (§4) are pinned as tests: no field named like a secret,
no 32-byte hex value, nothing from the run's config, a quoted address or key
masked wherever free text is read.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp import status as status_module
from contrib.hyperliquid_perp.paper import accounting as paper_accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION as STORE_SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting
from contrib.hyperliquid_perp.status import (
    SCHEMA_VERSION,
    SOURCE,
    TEXT_LIMIT,
    Breadcrumb,
    read_status,
    status_json,
    status_lines,
)

from .conftest import (
    insert_decision_attempts,
    insert_minimal_account_snapshot,
    insert_minimal_ai_output,
    insert_minimal_fill,
)

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_NOW = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
# Synthetic credential shapes the staged config carries and the document must not.
_AGENT_KEY = "0x" + "ab" * 32
_WALLET = "0x" + "12" * 20
# An EIP-55 checksummed address and an upper-case key: mixed-case hex masks too.
_CHECKSUM_WALLET = "0x" + "aB3F" * 10
_UPPER_KEY = "0x" + "CD" * 32
_ZERO_COUNTS = {"in_progress": 0, "completed": 0, "api_failed": 0, "invalid_output": 0}


def _now() -> datetime:
    return _NOW


def _at(hours: int) -> datetime:
    return _T0 + timedelta(hours=hours)


@pytest.fixture
def db():
    with Database(":memory:") as database:
        accounting.initialize_run(
            database,
            run_id="r",
            mode="paper",
            initial_balance_usdc=D(1000),
            schema_version=1,
            # The config the run was created under names the wallet and the
            # agent key; a status must copy none of it out.
            config_json=json.dumps(
                {"wallet_address": _WALLET, "agent_key": _AGENT_KEY, "engine": {"deep": "m"}}
            ),
            created_at=_T0,
        )
        yield database


def _stage_a_busy_run(db: Database) -> None:
    """Three cycles (long, maintain, then a retried failure), a fill, two snapshots, protection, events, breadcrumbs."""
    insert_decision_attempts(
        db, ["completed", "completed", ("api_failed", "rate_limit")], start=_T0
    )
    first, second, third = (decision_attempt_id("r", _at(h)) for h in (0, 4, 8))
    paper_accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|f|0",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=D("0.01"),
        price=D(50000),
        fee_rate=D("0.0001"),
        timestamp=_at(0),
        fill_reason="entry",
    )
    with db.transaction() as conn:
        insert_minimal_ai_output(
            conn,
            run_id="r",
            attempt_id=first,
            timestamp=_at(0),
            decision_mode="set_target",
            target_side="long",
            requested_target_margin_pct=D(25),
            approved_target_margin_pct=D(20),
            risk_action="clamped",
            risk_reason="max_target_margin_pct",
            confidence=D("0.7"),
            mark_price=D(50000),
            account_equity=D(1000),
            order_created=True,
            no_order_reason=None,
            decision_reason="Momentum\tis  up.\n" + "x" * 200,
        )
        insert_minimal_ai_output(
            conn, run_id="r", attempt_id=second, timestamp=_at(4), confidence=D("0.6")
        )
        conn.execute(
            "UPDATE decision_attempts SET error_message = ?, attempt_count = 2"
            " WHERE decision_attempt_id = ?",
            ("429 from the gateway\nafter 3 tries", third),
        )
        # Distinct non-zero accumulators, so a swapped column shows.
        conn.execute(
            "UPDATE current_account_state SET realized_pnl = '7', net_funding_pnl = '-2'"
            " WHERE run_id = ?",
            ("r",),
        )
        repo.insert_ai_input(
            conn,
            input_id="r|in|1",
            timestamp=_at(0),
            mode="paper",
            run_id="r",
            symbol="BTC",
            model="deep-a",
            prompt_version="v6",
        )
        repo.insert_ai_input(
            conn,
            input_id="r|in|2",
            timestamp=_at(4),
            mode="paper",
            run_id="r",
            symbol="BTC",
            model="deep-a",
            prompt_version="v6",
        )
        insert_minimal_account_snapshot(
            conn, run_id="r", timestamp=_at(1), account_equity=D(1001), unrealized_pnl=D(1)
        )
        insert_minimal_account_snapshot(
            conn,
            run_id="r",
            timestamp=_at(2),
            wallet_balance=D("999.95"),
            account_equity=D("1004.95"),
            available_balance=D("504.95"),
            unrealized_pnl=D(5),
            total_pnl=D("4.95"),
            total_fees=D("0.05"),
            total_position_notional=D(505),
            effective_leverage=D("0.5"),
            used_initial_margin=D(500),
            total_maintenance_margin=D(25),
            margin_ratio=D(40),
        )
        repo.set_position_protection(
            conn, "r", "BTC", stop_loss_price=D(48000), take_profit_price=D(55000)
        )
        repo.set_position_liquidation_price(conn, "r", "BTC", D(30000))
        repo.upsert_scheduler_state(
            conn,
            "r",
            last_decision_at=_at(8),
            next_decision_at=_at(12),
            current_attempt_id=third,
            lock_heartbeat_at=_at(9),
            last_export_status="ok",
            last_export_at=_at(8),
            last_replay_status="failed",
            last_replay_error="books unverifiable",
            last_replay_at=_at(8),
            safe_mode_type="recoverable",
            safe_mode_reason="ws_disconnect",
            safe_mode_entered_at=_at(9),
            updated_at=_at(10),
        )
        repo.insert_kill_switch_event(
            conn,
            run_id="r",
            event_type="kill_switch_refresh_failed",
            detail="deadline=300s",
            error_message="timeout",
            timestamp=_at(9),
        )
        repo.insert_safe_mode_event(
            conn,
            run_id="r",
            event_type="safe_mode_entered",
            safe_mode_type="recoverable",
            reason="ws_disconnect",
            detail="socket closed",
            timestamp=_at(9),
        )


def _read(db: Database, **kwargs):
    with db.read_transaction() as conn:
        return read_status(conn, "r", now=_now, **kwargs)


def test_a_fresh_run_reads_as_empty_but_present(db):
    status = _read(db)
    assert (status.run_id, status.mode, status.created_at) == ("r", "paper", _T0.isoformat())
    assert status.source == SOURCE == "hyperliquid_perp"
    assert status.initial_balance_usdc == D(1000)
    assert status.store_schema_version == STORE_SCHEMA_VERSION
    assert status.generated_at == _NOW.isoformat()
    assert status.scheduler is None and status.safe_mode is None
    assert (status.cycles.total, status.cycles.recent) == (0, ())
    assert status.cycles.by_status == _ZERO_COUNTS
    assert status.models == ()
    assert status.account.ledger.wallet_balance == D(1000)
    assert status.account.snapshot is None and status.account.recent_snapshots == ()
    assert status.positions == status.recent_fills == ()
    assert status.kill_switch_events == status.safe_mode_events == ()


def test_a_busy_run_reads_every_part_from_the_store(db):
    _stage_a_busy_run(db)
    status = _read(db)
    assert status.cycles.total == 3
    assert status.cycles.by_status == {**_ZERO_COUNTS, "completed": 2, "api_failed": 1}
    newest, middle, oldest = status.cycles.recent
    assert (newest.status, newest.error_type, newest.verdict) == ("api_failed", "rate_limit", None)
    assert (newest.error_message, newest.attempt_count) == (
        "429 from the gateway\nafter 3 tries",
        2,
    )
    assert (middle.status, middle.verdict.decision_mode, middle.verdict.order_created) == (
        "completed",
        "maintain_current",
        False,
    )
    assert middle.verdict.no_order_reason == "maintain_current"
    assert oldest.verdict.target_side == "long"
    assert oldest.verdict.approved_target_margin_pct == D(20)
    assert oldest.verdict.order_created is True
    assert oldest.verdict.decision_reason.startswith("Momentum\tis  up.\n")
    assert [(m.model, m.prompt_version, m.prompts) for m in status.models] == [("deep-a", "v6", 2)]
    assert (status.models[0].first_at, status.models[0].last_at) == (
        _at(0).isoformat(),
        _at(4).isoformat(),
    )
    scheduler = status.scheduler
    assert (scheduler.last_decision_at, scheduler.next_decision_at) == (
        _at(8).isoformat(),
        _at(12).isoformat(),
    )
    assert scheduler.in_progress_attempt_id == decision_attempt_id("r", _at(8))
    assert (scheduler.lease_heartbeat_at, scheduler.updated_at) == (
        _at(9).isoformat(),
        _at(10).isoformat(),
    )
    assert scheduler.export == Breadcrumb("ok", _at(8).isoformat(), None)
    assert scheduler.replay == Breadcrumb("failed", _at(8).isoformat(), "books unverifiable")
    assert scheduler.config_drift == Breadcrumb(None, None, None)
    assert (status.safe_mode.type, status.safe_mode.reason, status.safe_mode.entered_at) == (
        "recoverable",
        "ws_disconnect",
        _at(9).isoformat(),
    )
    ledger = status.account.ledger
    assert (
        ledger.wallet_balance,
        ledger.realized_pnl,
        ledger.total_fees,
        ledger.net_funding_pnl,
    ) == (
        D("999.95"),
        D(7),
        D("0.05"),
        D(-2),
    )
    snapshot = status.account.snapshot
    assert asdict(snapshot) == {
        "timestamp": _at(2).isoformat(),
        "account_equity": D("1004.95"),
        "available_balance": D("504.95"),
        "unrealized_pnl": D(5),
        "total_pnl": D("4.95"),
        "total_position_notional": D(505),
        "effective_leverage": D("0.5"),
        "margin_ratio": D(40),
    }
    assert [s.timestamp for s in status.account.recent_snapshots] == [
        _at(2).isoformat(),
        _at(1).isoformat(),
    ]
    assert status.account.recent_snapshots[0] == snapshot
    (position,) = status.positions
    assert (position.size, position.entry_price, position.stop_loss_price) == (
        D("0.01"),
        D(50000),
        D(48000),
    )
    assert (position.take_profit_price, position.exchange_liquidation_price) == (
        D(55000),
        D(30000),
    )
    (fill,) = status.recent_fills
    assert (fill.side, fill.fill_qty, fill.fill_reason) == ("buy", D("0.01"), "entry")
    (kill,) = status.kill_switch_events
    assert (kill.event_type, kill.detail, kill.error_message) == (
        "kill_switch_refresh_failed",
        "deadline=300s",
        "timeout",
    )
    (safe,) = status.safe_mode_events
    assert (safe.event_type, safe.reason, safe.detail) == (
        "safe_mode_entered",
        "ws_disconnect",
        "socket closed",
    )


def test_a_scheduled_run_in_no_safe_mode_reads_none(db):
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", next_decision_at=_at(4), updated_at=_at(1))
    status = _read(db)
    assert status.scheduler.next_decision_at == _at(4).isoformat()
    assert status.safe_mode is None
    assert "safe_mode: none" in status_lines(status)
    assert json.loads(status_json(status))["safe_mode"] is None


def test_recent_caps_every_tail_but_not_the_counts(db):
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        for i in range(3):
            insert_minimal_fill(
                conn, run_id="r", fill_id=f"r|f|{i + 1}", timestamp=_at(10 + i), price=50000
            )
            insert_minimal_account_snapshot(conn, run_id="r", timestamp=_at(10 + i))
            repo.insert_kill_switch_event(
                conn, run_id="r", event_type="kill_switch_refreshed", timestamp=_at(10 + i)
            )
            repo.insert_safe_mode_event(
                conn,
                run_id="r",
                event_type="safe_mode_reason_added",
                safe_mode_type="recoverable",
                reason=f"reason {i}",
                timestamp=_at(10 + i),
            )
    status = _read(db, recent=2)
    assert len(status.cycles.recent) == 2 and status.cycles.total == 3
    assert status.cycles.recent[0].status == "api_failed"
    assert len(status.account.recent_snapshots) == 2
    assert len(status.recent_fills) == 2
    assert len(status.kill_switch_events) == len(status.safe_mode_events) == 2
    assert status.account.snapshot.timestamp == _at(12).isoformat()
    empty = _read(db, recent=0)
    assert empty.cycles.recent == empty.recent_fills == empty.account.recent_snapshots == ()
    assert empty.account.snapshot is not None  # the latest is not a tail


def test_an_unknown_run_is_refused(db):
    with db.read_transaction() as conn, pytest.raises(ValueError, match="'ghost' does not exist"):
        read_status(conn, "ghost", now=_now)


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        ("DELETE FROM current_account_state WHERE run_id = 'r'", "no account state"),
        ("UPDATE current_positions SET entry_price = NULL WHERE run_id = 'r'", "entry_price"),
        ("UPDATE current_positions SET size = '0' WHERE run_id = 'r'", "entry_price"),
        (
            "UPDATE decision_attempts SET output_id = NULL WHERE run_id = 'r' AND status = 'completed'",
            "no ai_outputs row",
        ),
        (
            "UPDATE decision_attempts SET output_id = (SELECT output_id FROM ai_outputs LIMIT 1)"
            " WHERE run_id = 'r' AND status = 'api_failed'",
            "has an ai_outputs row",
        ),
        ("UPDATE scheduler_state SET safe_mode_reason = NULL WHERE run_id = 'r'", "safe mode"),
        ("UPDATE scheduler_state SET safe_mode_entered_at = NULL WHERE run_id = 'r'", "safe mode"),
        ("UPDATE scheduler_state SET safe_mode_type = NULL WHERE run_id = 'r'", "safe mode"),
        ("UPDATE scheduler_state SET last_export_at = NULL WHERE run_id = 'r'", "breadcrumb"),
        ("UPDATE scheduler_state SET last_export_status = NULL WHERE run_id = 'r'", "breadcrumb"),
        (
            "UPDATE scheduler_state SET last_config_drift_error = 'x' WHERE run_id = 'r'",
            "breadcrumb",
        ),
        ("UPDATE current_account_state SET wallet_balance = 'NaN' WHERE run_id = 'r'", "finite"),
        ("UPDATE account_snapshots SET total_pnl = 'Infinity' WHERE run_id = 'r'", "finite"),
        (
            "UPDATE decision_attempts SET status = 'invalid_output', output_id = NULL"
            " WHERE run_id = 'r' AND status = 'completed'",
            "no ai_outputs row",
        ),
        (
            "UPDATE decision_attempts SET status = 'in_progress'"
            " WHERE run_id = 'r' AND status = 'completed'",
            "has an ai_outputs row",
        ),
    ],
)
def test_an_impossible_state_is_refused_not_defaulted(db, damage, said):
    # Each is a state no writer produces; a status that showed it as a quiet
    # value would hide a damaged store behind a healthy-looking page.
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(damage)
    with pytest.raises(ValueError, match=said):
        _read(db)


def test_a_cleared_breadcrumb_or_safe_mode_reads_as_none(db):
    # All three columns NULL is the "not yet" / "released" state, not a half.
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE scheduler_state SET last_export_status = NULL, last_export_at = NULL,"
            " safe_mode_type = NULL, safe_mode_reason = NULL, safe_mode_entered_at = NULL"
            " WHERE run_id = 'r'"
        )
    status = _read(db)
    assert status.scheduler.export == Breadcrumb(None, None, None)
    assert status.safe_mode is None


def test_the_verdict_split_covers_the_terminal_vocabulary():
    # Every terminal status either carries a verdict or is the API failure;
    # a new terminal word must be placed on one side before it ships.
    decided, undecided = status_module._DECIDED_STATUSES, status_module._UNDECIDED_STATUSES
    assert decided == {"completed", "invalid_output"}
    assert undecided == {"in_progress", "api_failed"}
    assert frozenset(repo.TERMINAL_ATTEMPT_STATUSES) - decided == {"api_failed"}
    assert decided | undecided == {status.value for status in repo.AttemptStatus}


def test_a_status_word_this_build_does_not_know_passes_through(db):
    # A daemon upgrade may write a word before the status tool learns it;
    # the page must show it, not refuse the whole run.
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE decision_attempts SET status = 'paused' WHERE run_id = 'r' AND status = ?",
            ("api_failed",),
        )
    status = _read(db)
    assert list(status.cycles.by_status.items()) == [
        ("in_progress", 0),
        ("completed", 2),
        ("api_failed", 0),
        ("invalid_output", 0),
        ("paused", 1),
    ]
    assert status.cycles.recent[0].status == "paused"
    paused = f"  {_at(8).isoformat()}  paused (try 2)"
    assert any(line.startswith(paused) for line in status_lines(status))
    # The same word beside a verdict passes through too: the pairing check
    # only knows the words it knows.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE decision_attempts SET status = 'paused' WHERE run_id = 'r' AND status = ?",
            ("completed",),
        )
    status = _read(db)
    assert status.cycles.by_status["paused"] == 3
    assert all(cycle.status == "paused" for cycle in status.cycles.recent)
    assert status.cycles.recent[2].verdict is not None


def test_the_clock_must_be_aware_and_is_spelled_in_utc(db):
    with db.read_transaction() as conn:
        with pytest.raises(ValueError, match="aware"):
            read_status(conn, "r", now=lambda: datetime(2026, 7, 7))
        tokyo = datetime(2026, 7, 7, 9, tzinfo=timezone(timedelta(hours=9)))
        assert read_status(conn, "r", now=lambda: tokyo).generated_at == _NOW.isoformat()


# --- the document -------------------------------------------------------------


def _nodes(value, prefix: str = ""):
    """Every ``(path, value)`` in the document, list items under ``path[]``."""
    if prefix:
        yield prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _nodes(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child, f"{prefix}[]")


def _paths(document) -> set[str]:
    return {path for path, _ in _nodes(document) if not path.endswith("[]")}


_SNAPSHOT_FIELDS = (
    "timestamp",
    "account_equity",
    "available_balance",
    "unrealized_pnl",
    "total_pnl",
    "total_position_notional",
    "effective_leverage",
    "margin_ratio",
)

# The document's shape at SCHEMA_VERSION 1. A path added, removed or moved
# changes this set; change it together with ``status.SCHEMA_VERSION``, and the
# exporter and the page that read the document with it.
_SCHEMA_1_PATHS = {
    "schema_version",
    "source",
    "generated_at",
    "run_id",
    "mode",
    "created_at",
    "initial_balance_usdc",
    "store_schema_version",
    "scheduler",
    "scheduler.last_decision_at",
    "scheduler.next_decision_at",
    "scheduler.in_progress_attempt_id",
    "scheduler.lease_heartbeat_at",
    "scheduler.updated_at",
    "scheduler.export",
    "scheduler.export.status",
    "scheduler.export.at",
    "scheduler.export.error",
    "scheduler.replay",
    "scheduler.replay.status",
    "scheduler.replay.at",
    "scheduler.replay.error",
    "scheduler.config_drift",
    "scheduler.config_drift.status",
    "scheduler.config_drift.at",
    "scheduler.config_drift.error",
    "safe_mode",
    "safe_mode.type",
    "safe_mode.reason",
    "safe_mode.entered_at",
    "cycles",
    "cycles.total",
    "cycles.by_status",
    "cycles.by_status.in_progress",
    "cycles.by_status.completed",
    "cycles.by_status.api_failed",
    "cycles.by_status.invalid_output",
    "cycles.recent",
    "cycles.recent[].scheduled_at",
    "cycles.recent[].timestamp",
    "cycles.recent[].status",
    "cycles.recent[].error_type",
    "cycles.recent[].error_message",
    "cycles.recent[].attempt_count",
    "cycles.recent[].next_decision_at",
    "cycles.recent[].verdict",
    "cycles.recent[].verdict.symbol",
    "cycles.recent[].verdict.decision_mode",
    "cycles.recent[].verdict.target_side",
    "cycles.recent[].verdict.requested_target_margin_pct",
    "cycles.recent[].verdict.approved_target_margin_pct",
    "cycles.recent[].verdict.risk_action",
    "cycles.recent[].verdict.risk_reason",
    "cycles.recent[].verdict.confidence",
    "cycles.recent[].verdict.mark_price",
    "cycles.recent[].verdict.account_equity",
    "cycles.recent[].verdict.order_created",
    "cycles.recent[].verdict.no_order_reason",
    "cycles.recent[].verdict.decision_reason",
    "cycles.recent[].verdict.guardrail_side",
    "models",
    "models[].model",
    "models[].prompt_version",
    "models[].prompts",
    "models[].first_at",
    "models[].last_at",
    "account",
    "account.ledger",
    "account.ledger.wallet_balance",
    "account.ledger.realized_pnl",
    "account.ledger.total_fees",
    "account.ledger.net_funding_pnl",
    "account.snapshot",
    *(f"account.snapshot.{field}" for field in _SNAPSHOT_FIELDS),
    "account.recent_snapshots",
    *(f"account.recent_snapshots[].{field}" for field in _SNAPSHOT_FIELDS),
    "positions",
    "positions[].symbol",
    "positions[].size",
    "positions[].entry_price",
    "positions[].realized_pnl",
    "positions[].stop_loss_price",
    "positions[].take_profit_price",
    "positions[].exchange_liquidation_price",
    "positions[].updated_at",
    "recent_fills",
    "recent_fills[].timestamp",
    "recent_fills[].symbol",
    "recent_fills[].side",
    "recent_fills[].fill_qty",
    "recent_fills[].fill_price",
    "recent_fills[].fee",
    "recent_fills[].realized_pnl_delta",
    "recent_fills[].liquidity_type",
    "recent_fills[].fill_reason",
    "kill_switch_events",
    "kill_switch_events[].timestamp",
    "kill_switch_events[].event_type",
    "kill_switch_events[].detail",
    "kill_switch_events[].error_message",
    "safe_mode_events",
    "safe_mode_events[].timestamp",
    "safe_mode_events[].event_type",
    "safe_mode_events[].safe_mode_type",
    "safe_mode_events[].reason",
    "safe_mode_events[].released_by",
    "safe_mode_events[].detail",
}


def test_the_document_has_exactly_the_schema_1_paths(db):
    _stage_a_busy_run(db)
    document = json.loads(status_json(_read(db)))
    assert SCHEMA_VERSION == 1
    assert document["schema_version"] == SCHEMA_VERSION
    assert list(document)[:3] == ["schema_version", "source", "generated_at"]
    assert _paths(document) == _SCHEMA_1_PATHS


def test_the_document_keeps_decimals_exact_and_free_text_up_to_the_limit(db):
    _stage_a_busy_run(db)
    status = _read(db)
    document = json.loads(status_json(status))
    assert document["initial_balance_usdc"] == "1000"
    assert document["account"]["snapshot"]["account_equity"] == "1004.95"
    assert document["account"]["recent_snapshots"][1]["account_equity"] == "1001"
    assert document["cycles"]["by_status"] == {**_ZERO_COUNTS, "completed": 2, "api_failed": 1}
    assert document["cycles"]["recent"][2]["verdict"]["approved_target_margin_pct"] == "20"
    assert document["cycles"]["recent"][2]["verdict"]["order_created"] is True
    assert document["cycles"]["recent"][2]["verdict"]["decision_reason"] == (
        "Momentum\tis  up.\n" + "x" * 200
    )
    assert document["cycles"]["recent"][0]["error_message"] == "429 from the gateway\nafter 3 tries"
    assert document["positions"][0]["exchange_liquidation_price"] == "30000"


def test_free_text_is_cut_at_the_limit_in_the_model_and_the_document(db):
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ai_outputs SET decision_reason = ? WHERE run_id = ?", ("y" * 1500, "r")
        )
    status = _read(db)
    reason = status.cycles.recent[2].verdict.decision_reason
    assert len(reason) == TEXT_LIMIT == 1000 and reason.endswith("yyy...")
    document = json.loads(status_json(status))
    assert document["cycles"]["recent"][2]["verdict"]["decision_reason"] == reason
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ai_outputs SET decision_reason = ? WHERE run_id = ?", ("w" * 1000, "r")
        )
    assert _read(db).cycles.recent[2].verdict.decision_reason == "w" * 1000  # whole at the limit


def test_a_secret_across_the_cut_is_masked_before_it_is_cut(db):
    # Masking runs before the cut: a key that straddles the limit must not
    # leave its first hundreds of hex characters behind as a prefix.
    _stage_a_busy_run(db)
    for secret in (_AGENT_KEY, _WALLET, _CHECKSUM_WALLET, _UPPER_KEY):
        with db.transaction() as conn:
            conn.execute(
                "UPDATE ai_outputs SET decision_reason = ? WHERE run_id = ?",
                ("a" * 980 + secret + "z" * 50, "r"),
            )
        reason = _read(db).cycles.recent[2].verdict.decision_reason
        assert len(reason) <= TEXT_LIMIT
        assert secret not in reason and f"{secret[:6]}...{secret[-4:]}" in reason
        assert re.search(r"0x[0-9a-fA-F]{8,}", reason) is None


def test_the_document_and_the_lines_are_ascii_whatever_the_model_wrote(db):
    # A console or a pipe on a narrow code page must carry both: the document
    # escapes the character, a line spells it as its escape.
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ai_outputs SET decision_reason = ? WHERE output_id = ?",
            ("Range — café", f"{decision_attempt_id('r', _at(0))}#out1"),
        )
    status = _read(db)
    text = status_json(status)
    assert text.isascii() and "Range \\u2014 caf\\u00e9" in text
    assert json.loads(text)["cycles"]["recent"][2]["verdict"]["decision_reason"] == ("Range — café")
    (line,) = [line for line in status_lines(status) if "| Range" in line]
    assert line.isascii() and line.endswith("| Range \\u2014 caf\\xe9")
    # A control character could do anything to a terminal: it prints as "?".
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ai_outputs SET decision_reason = ? WHERE output_id = ?",
            ("red \x1b[31m bell \x07 end", f"{decision_attempt_id('r', _at(0))}#out1"),
        )
    (line,) = [line for line in status_lines(_read(db)) if "| red" in line]
    assert line.endswith("| red ?[31m bell ? end")


_SECRET_FIELD = re.compile(r"key|secret|password|mnemonic|address", re.IGNORECASE)
_HEX_32_BYTES = re.compile(r"0x[0-9a-fA-F]{64}")
# Every column the readers expose that a person, a model or an exchange wrote.
_FREE_TEXT_COLUMNS = (
    ("decision_attempts", "error_message"),
    ("ai_outputs", "decision_reason"),
    ("ai_outputs", "risk_reason"),
    ("ai_outputs", "no_order_reason"),
    ("fills", "fill_reason"),
    ("kill_switch_events", "detail"),
    ("kill_switch_events", "error_message"),
    ("safe_mode_events", "reason"),
    ("safe_mode_events", "detail"),
    ("scheduler_state", "safe_mode_reason"),
    ("scheduler_state", "last_export_error"),
    ("scheduler_state", "last_replay_error"),
    ("scheduler_state", "last_config_drift_error"),
)


def test_free_text_masks_a_quoted_address_or_key_wherever_it_is_read(db):
    # An exchange refusal quotes the wallet it refused, and a traceback can
    # quote a key; the store keeps the text, the status folds the hex — in
    # every free-text column, not a hand-picked few.
    _stage_a_busy_run(db)
    quoted = (
        f"User or API Wallet {_WALLET} does not exist (key {_AGENT_KEY}); "
        f"nor {_CHECKSUM_WALLET} (key {_UPPER_KEY})"
    )
    with db.transaction() as conn:
        # The config-drift breadcrumb has no pass staged; give it one so its
        # error column is a legitimate place to quote the wallet.
        repo.upsert_scheduler_state(
            conn, "r", last_config_drift_status="drift", last_config_drift_at=_at(8)
        )
        for table, column in _FREE_TEXT_COLUMNS:
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE run_id = ?", (quoted, "r"))
    status = _read(db)
    masked = (
        "User or API Wallet 0x1212...1212 does not exist (key 0xabab...abab); "
        "nor 0xaB3F...aB3F (key 0xCDCD...CDCD)"
    )
    assert status.cycles.recent[0].error_message == masked
    assert status.cycles.recent[2].verdict.decision_reason == masked
    assert status.safe_mode.reason == masked
    assert status.scheduler.replay.error == masked
    strings = [value for _, value in _nodes(asdict(status)) if isinstance(value, str)]
    assert masked in strings
    secrets = (_WALLET, _AGENT_KEY, _CHECKSUM_WALLET, _UPPER_KEY)
    assert not [value for value in strings if any(secret in value for secret in secrets)]
    assert not [line for line in status_lines(status) if any(s in line for s in secrets)]


def test_the_document_names_no_secret_field_and_carries_no_key_or_address(db):
    # Plan §4: the field list is a whitelist. No path may read like a
    # credential, no value may be the 32-byte hex an agent key is, and the
    # staged config's wallet and key must not have been copied out.
    _stage_a_busy_run(db)
    text = status_json(_read(db))
    document = json.loads(text)
    named = [path for path in _paths(document) if _SECRET_FIELD.search(path.rsplit(".", 1)[-1])]
    assert not named
    assert not _HEX_32_BYTES.search(text)
    assert _WALLET not in text and _AGENT_KEY not in text
    assert not [path for path, value in _nodes(document) if isinstance(value, float)]


# --- the lines ----------------------------------------------------------------


def test_the_lines_say_every_part_in_one_line_each(db):
    _stage_a_busy_run(db)
    lines = status_lines(_read(db))
    assert lines[0] == "run_id: r"
    assert f"created_at: {_T0.isoformat()}" in lines
    assert "initial_balance_usdc: 1000" in lines
    assert f"store_schema_version: {STORE_SCHEMA_VERSION}" in lines
    assert f"scheduler_updated_at: {_at(10).isoformat()}" in lines
    assert f"last_decision_at: {_at(8).isoformat()}" in lines
    assert f"next_decision_at: {_at(12).isoformat()}" in lines
    assert f"in_progress_attempt: {decision_attempt_id('r', _at(8))}" in lines
    assert f"lease_heartbeat_at: {_at(9).isoformat()}" in lines
    assert "cycles: 3 (in_progress 0, completed 2, api_failed 1, invalid_output 0)" in lines
    assert (
        f"model: deep-a prompt_version=v6 prompts=2 first={_at(0).isoformat()} "
        f"last={_at(4).isoformat()}"
    ) in lines
    assert f"replay: failed at {_at(8).isoformat()} (books unverifiable)" in lines
    assert "config_drift: n/a" in lines
    assert f"safe_mode: recoverable since {_at(9).isoformat()} (ws_disconnect)" in lines
    # Amounts print as the store keeps them, exact and unrounded.
    assert "ledger: wallet 999.950000 realized 7 fees 0.050000 funding -2" in lines
    assert (
        f"snapshot: at {_at(2).isoformat()} equity 1004.95 available 504.95 unrealized 5 "
        "total_pnl 4.95 notional 505 leverage 0.5 margin_ratio 40"
    ) in lines
    (position,) = [line for line in lines if line.startswith("position:")]
    assert position.startswith(
        "position: BTC long 0.01 entry 50000 realized 0 sl 48000 tp 55000 liq 30000"
    )
    index = lines.index("recent cycles (3, newest first):")
    assert lines[index + 1].startswith(
        f"  {_at(8).isoformat()}  api_failed (try 2)  rate_limit  429 from"
    )
    assert "429 from the gateway after 3 tries" in lines[index + 1]
    assert lines[index + 2].endswith(
        "BTC maintain_current  requested n/a% approved n/a%  conf 0.6  approved  "
        "no order (maintain_current)"
    )
    assert "(try" not in lines[index + 2]  # a first try is not labelled
    oldest = lines[index + 3]
    assert (
        "BTC long  requested 25% approved 20%  conf 0.7  clamped (max_target_margin_pct)  order"
        in oldest
    )
    # The reason is one line, cut short with a marker.
    assert "| Momentum is up. xxx" in oldest and oldest.endswith("...")
    assert "\n" not in oldest and "\t" not in oldest
    index = lines.index("recent snapshots (2, newest first):")
    assert lines[index + 1].startswith(f"  {_at(2).isoformat()}  equity 1004.95 available 504.95")
    assert lines[index + 2].startswith(f"  {_at(1).isoformat()}  equity 1001 available 1000")
    assert (
        f"  {_at(0).isoformat()}  BTC buy 0.01 @ 50000  fee 0.050000  realized 0  simulated  (entry)"
        in lines
    )
    assert f"  {_at(9).isoformat()}  kill_switch_refresh_failed  deadline=300s  timeout" in lines
    assert (
        f"  {_at(9).isoformat()}  safe_mode_entered  recoverable  ws_disconnect  socket closed"
        in lines
    )


def test_the_lines_show_a_short_and_a_flat_position_as_such(db):
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn, "r", PositionState(coin="ETH", size=D("-0.02"), entry_price=D(3000))
        )
        repo.upsert_current_position(conn, "r", PositionState.flat("SOL", realized_pnl=D(3)))
    lines = [line for line in status_lines(_read(db)) if line.startswith("position:")]
    assert lines[0].startswith(
        "position: ETH short 0.02 entry 3000 realized 0 sl n/a tp n/a liq n/a"
    )
    assert lines[1].startswith("position: SOL flat realized 3 sl n/a tp n/a liq n/a")


def test_the_lines_of_a_fresh_run_say_none_rather_than_nothing(db):
    lines = status_lines(_read(db))
    for expected in (
        "scheduler: none (no cycle scheduled yet)",
        "safe_mode: none",
        "cycles: 0 (in_progress 0, completed 0, api_failed 0, invalid_output 0)",
        "model: none (no prompt built yet)",
        "ledger: wallet 1000 realized 0 fees 0 funding 0",
        "snapshot: none",
        "position: none",
        "recent cycles: none",
        "recent snapshots: none",
        "recent fills: none",
        "kill_switch_events: none",
        "safe_mode_events: none",
    ):
        assert expected in lines
