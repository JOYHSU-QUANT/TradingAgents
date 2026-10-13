"""The ``status`` reading model: what it reads, how the document is shaped, what it must not carry.

Dashboard plan PR 2a. The document's field paths are pinned here against
``SCHEMA_VERSION``; the plan's security rules (§4) are pinned as tests: no
field named like a secret, no 32-byte hex value, nothing from the run's
config.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.paper import accounting as paper_accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION as STORE_SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting
from contrib.hyperliquid_perp.status import (
    SCHEMA_VERSION,
    Breadcrumb,
    read_status,
    status_json,
    status_lines,
)

from .conftest import insert_decision_attempts, insert_minimal_account_snapshot

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_NOW = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
# Synthetic credential shapes the staged config carries and the document must not.
_AGENT_KEY = "0x" + "ab" * 32
_WALLET = "0x" + "12" * 20


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
    """Three cycles (the newest failed), one fill, a snapshot, protection, events, breadcrumbs."""
    insert_decision_attempts(
        db, ["completed", "completed", ("api_failed", "rate_limit")], start=_T0
    )
    first = decision_attempt_id("r", _at(0))
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
        conn.execute(
            "UPDATE decision_attempts SET output_id = ? WHERE decision_attempt_id = ?",
            ("r|out|1", first),
        )
        conn.execute(
            "UPDATE decision_attempts SET error_message = ? WHERE decision_attempt_id = ?",
            ("429 from the gateway\nafter 3 tries", decision_attempt_id("r", _at(8))),
        )
        repo.insert_ai_output(
            conn,
            output_id="r|out|1",
            timestamp=_at(0),
            mode="paper",
            run_id="r",
            decision_attempt_id=first,
            symbol="BTC",
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
            decision_reason="Momentum\tis  up.\n" + "x" * 200,
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
        insert_minimal_account_snapshot(
            conn,
            run_id="r",
            timestamp=_at(1),
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
        repo.upsert_scheduler_state(
            conn,
            "r",
            last_decision_at=_at(8),
            next_decision_at=_at(12),
            current_attempt_id=None,
            lock_heartbeat_at=_at(9),
            last_export_status="ok",
            last_export_at=_at(8),
            last_replay_status="failed",
            last_replay_error="books unverifiable",
            last_replay_at=_at(8),
            safe_mode_type="recoverable",
            safe_mode_reason="ws_disconnect",
            safe_mode_entered_at=_at(9),
            updated_at=_at(9),
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
    assert status.initial_balance_usdc == D(1000)
    assert status.store_schema_version == STORE_SCHEMA_VERSION
    assert status.generated_at == _NOW.isoformat()
    assert status.scheduler is None and status.safe_mode is None
    assert (status.cycles.total, status.cycles.by_status, status.cycles.recent) == (0, {}, ())
    assert status.models == ()
    assert status.account.ledger.wallet_balance == D(1000)
    assert status.account.snapshot is None
    assert status.positions == status.recent_fills == ()
    assert status.kill_switch_events == status.safe_mode_events == ()


def test_a_busy_run_reads_every_part_from_the_store(db):
    _stage_a_busy_run(db)
    status = _read(db)
    assert status.cycles.total == 3
    assert status.cycles.by_status == {"api_failed": 1, "completed": 2}
    newest, _, oldest = status.cycles.recent
    assert (newest.status, newest.error_type, newest.verdict) == ("api_failed", "rate_limit", None)
    assert newest.error_message == "429 from the gateway\nafter 3 tries"
    assert oldest.verdict.target_side == "long"
    assert oldest.verdict.approved_target_margin_pct == D(20)
    assert oldest.verdict.order_created is True
    assert oldest.verdict.decision_reason.startswith("Momentum\tis  up.\n")
    assert [(m.model, m.prompt_version, m.prompts) for m in status.models] == [("deep-a", "v6", 1)]
    assert status.scheduler.lease_heartbeat_at == _at(9).isoformat()
    assert status.scheduler.export == Breadcrumb("ok", _at(8).isoformat(), None)
    assert status.scheduler.replay == Breadcrumb("failed", _at(8).isoformat(), "books unverifiable")
    assert status.scheduler.config_drift == Breadcrumb(None, None, None)
    assert (status.safe_mode.type, status.safe_mode.reason) == ("recoverable", "ws_disconnect")
    assert status.account.ledger.total_fees == D("0.05")
    assert status.account.snapshot.account_equity == D("1004.95")
    assert status.account.snapshot.margin_ratio == D(40)
    (position,) = status.positions
    assert (position.size, position.entry_price, position.stop_loss_price) == (
        D("0.01"),
        D(50000),
        D(48000),
    )
    assert position.exchange_liquidation_price is None
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


def test_recent_caps_the_cycles_fills_and_events_but_not_the_counts(db):
    _stage_a_busy_run(db)
    status = _read(db, recent=1)
    assert len(status.cycles.recent) == 1 and status.cycles.total == 3
    assert status.cycles.recent[0].status == "api_failed"
    assert _read(db, recent=0).cycles.recent == ()


def test_an_unknown_run_is_refused(db):
    with db.read_transaction() as conn, pytest.raises(ValueError, match="'ghost' does not exist"):
        read_status(conn, "ghost", now=_now)


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


# The document's shape at SCHEMA_VERSION 1. A path added, removed or moved
# changes this set; change it together with ``status.SCHEMA_VERSION``, and the
# exporter and the page that read the document with it.
_SCHEMA_1_PATHS = {
    "schema_version",
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
    "cycles.by_status.api_failed",
    "cycles.by_status.completed",
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
    "account.snapshot.timestamp",
    "account.snapshot.account_equity",
    "account.snapshot.available_balance",
    "account.snapshot.unrealized_pnl",
    "account.snapshot.total_pnl",
    "account.snapshot.total_position_notional",
    "account.snapshot.effective_leverage",
    "account.snapshot.margin_ratio",
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
    assert list(document)[:2] == ["schema_version", "generated_at"]
    assert _paths(document) == _SCHEMA_1_PATHS


def test_the_document_keeps_decimals_exact_and_free_text_whole(db):
    _stage_a_busy_run(db)
    status = _read(db)
    document = json.loads(status_json(status))
    assert document["initial_balance_usdc"] == "1000"
    assert document["account"]["snapshot"]["account_equity"] == "1004.95"
    assert document["cycles"]["recent"][2]["verdict"]["approved_target_margin_pct"] == "20"
    assert document["cycles"]["recent"][2]["verdict"]["order_created"] is True
    assert document["cycles"]["recent"][2]["verdict"]["decision_reason"] == (
        "Momentum\tis  up.\n" + "x" * 200
    )
    assert document["cycles"]["recent"][0]["error_message"] == "429 from the gateway\nafter 3 tries"
    assert document["positions"][0]["exchange_liquidation_price"] is None


def test_the_document_and_the_lines_are_ascii_whatever_the_model_wrote(db):
    # A console or a pipe on a narrow code page must carry both: the document
    # escapes the character, a line spells it as its escape.
    _stage_a_busy_run(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ai_outputs SET decision_reason = ? WHERE run_id = ?",
            ("Range \u2014 caf\u00e9", "r"),
        )
    status = _read(db)
    text = status_json(status)
    assert text.isascii() and "Range \\u2014 caf\\u00e9" in text
    assert json.loads(text)["cycles"]["recent"][2]["verdict"]["decision_reason"] == (
        "Range \u2014 caf\u00e9"
    )
    (line,) = [line for line in status_lines(status) if "| Range" in line]
    assert line.isascii() and line.endswith("| Range \\u2014 caf\\xe9")


_SECRET_FIELD = re.compile(r"key|secret|password|mnemonic|address", re.IGNORECASE)
_HEX_32_BYTES = re.compile(r"0x[0-9a-fA-F]{64}")


def test_free_text_masks_a_quoted_address_or_key_everywhere_it_is_read(db):
    # An exchange refusal quotes the wallet it refused, and a traceback can
    # quote a key; the store keeps the text, the status folds the hex.
    _stage_a_busy_run(db)
    quoted = f"User or API Wallet {_WALLET} does not exist (key {_AGENT_KEY})"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE decision_attempts SET error_message = ? WHERE run_id = ? AND status = ?",
            (quoted, "r", "api_failed"),
        )
        conn.execute("UPDATE ai_outputs SET decision_reason = ? WHERE run_id = ?", (quoted, "r"))
        conn.execute(
            "UPDATE kill_switch_events SET error_message = ? WHERE run_id = ?", (quoted, "r")
        )
        conn.execute("UPDATE safe_mode_events SET detail = ? WHERE run_id = ?", (quoted, "r"))
        repo.upsert_scheduler_state(
            conn,
            "r",
            safe_mode_type="recoverable",
            safe_mode_reason=quoted,
            safe_mode_entered_at=_at(9),
            last_replay_error=quoted,
        )
    status = _read(db)
    masked = "User or API Wallet 0x1212...1212 does not exist (key 0xabab...abab)"
    assert status.cycles.recent[0].error_message == masked
    assert status.cycles.recent[2].verdict.decision_reason == masked
    assert status.kill_switch_events[0].error_message == masked
    assert status.safe_mode_events[0].detail == masked
    assert status.safe_mode.reason == masked
    assert status.scheduler.replay.error == masked
    assert not [line for line in status_lines(status) if _WALLET in line or _AGENT_KEY in line]


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
    text = "\n".join(lines)
    assert lines[0] == "run_id: r"
    assert "cycles: 3 (api_failed 1, completed 2)" in lines
    assert f"model: deep-a prompt_version=v6 prompts=1 first={_at(0).isoformat()}" in text
    assert f"replay: failed at {_at(8).isoformat()} (books unverifiable)" in lines
    assert "config_drift: n/a" in lines
    assert f"safe_mode: recoverable since {_at(9).isoformat()} (ws_disconnect)" in lines
    # Amounts print as the store keeps them, exact and unrounded.
    assert "ledger: wallet 999.950000 realized 0 fees 0.050000 funding 0" in lines
    (position,) = [line for line in lines if line.startswith("position:")]
    assert position.startswith(
        "position: BTC long 0.01 entry 50000 realized 0 sl 48000 tp 55000 liq n/a"
    )
    index = lines.index("recent cycles (3, newest first):")
    assert lines[index + 1].startswith(f"  {_at(8).isoformat()}  api_failed  rate_limit  429 from")
    assert "429 from the gateway after 3 tries" in lines[index + 1]
    oldest = lines[index + 3]
    assert (
        "BTC long  requested 25% approved 20%  conf 0.7  clamped (max_target_margin_pct)  order"
        in oldest
    )
    # The reason is one line, cut short with a marker.
    assert "| Momentum is up. xxx" in oldest and oldest.endswith("...")
    assert "\n" not in oldest and "\t" not in oldest
    assert (
        f"  {_at(0).isoformat()}  BTC buy 0.01 @ 50000  fee 0.050000  realized 0  simulated  (entry)"
        in lines
    )
    assert f"  {_at(9).isoformat()}  kill_switch_refresh_failed  deadline=300s  timeout" in lines
    assert (
        f"  {_at(9).isoformat()}  safe_mode_entered  recoverable  ws_disconnect  socket closed"
        in lines
    )


def test_the_lines_of_a_fresh_run_say_none_rather_than_nothing(db):
    lines = status_lines(_read(db))
    for expected in (
        "scheduler: none (no cycle scheduled yet)",
        "safe_mode: none",
        "cycles: 0 (none)",
        "model: none (no prompt built yet)",
        "snapshot: none",
        "position: none",
        "recent cycles: none",
        "recent fills: none",
        "kill_switch_events: none",
        "safe_mode_events: none",
    ):
        assert expected in lines

