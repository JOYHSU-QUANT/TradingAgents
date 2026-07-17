"""Tests for the ``safe-mode`` CLI subcommand (§13.6 status / manual release)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.cli import main
from contrib.hyperliquid_perp.live.safe_mode import (
    REASON_NON_BOT_OWNED_ORDER,
    REASON_WS_DISCONNECT,
    SafeModeManager,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    """A real on-disk store with one live run (the CLI opens by path)."""
    path = tmp_path / "live_trading.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW - timedelta(days=1),
    )
    yield path, db
    db.close()


def _enter(db, safe_mode_type, reason):
    SafeModeManager(db=db, run_id="r", gate=None, clock=ManualClock(_NOW)).enter(
        safe_mode_type, reason
    )


def test_status_reports_none_when_not_in_safe_mode(store, capsys):
    path, db = store
    db.close()
    assert main(["safe-mode", "--run-id", "r", "--db", str(path)]) == 0
    assert "safe_mode: none" in capsys.readouterr().out


def test_status_reports_the_current_state_and_history(store, capsys):
    path, db = store
    _enter(db, "manual", REASON_NON_BOT_OWNED_ORDER)
    db.close()
    # Exit 4 while a safe mode is latched (0 = none): the scriptable-probe
    # contract (decided 2026-07-17).
    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--status"]) == 4
    out = capsys.readouterr().out
    assert "safe_mode: manual" in out
    assert REASON_NON_BOT_OWNED_ORDER in out
    assert "safe_mode_entered" in out


def test_status_refuses_release_only_flags(store, capsys):
    # --reason/--released-by without --release: named rejection, not silence —
    # an operator who typed them almost certainly meant --release, and
    # dropping them without a word would read as "release recorded".
    path, db = store
    _enter(db, "manual", REASON_NON_BOT_OWNED_ORDER)
    db.close()
    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--reason", "x"]) == 1
    assert "apply only with --release" in capsys.readouterr().err
    with Database(path) as db2:
        assert repo.get_scheduler_state(db2.conn, "r")["safe_mode_type"] == "manual"


def test_wrong_mode_run_is_a_named_error(store, capsys, tmp_path):
    # Safe mode is live-run state: a paper run id (a typo'd --run-id/--db)
    # must be named as such, not answered with a misleading "safe_mode: none".
    path = tmp_path / "paper_trading.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="p",
        mode="paper",
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW,
    )
    db.close()
    assert main(["safe-mode", "--run-id", "p", "--db", str(path)]) == 1
    assert "is a paper run" in capsys.readouterr().err


def test_release_clears_the_state_and_leaves_the_audit_trail(store, capsys):
    path, db = store
    _enter(db, "manual", REASON_NON_BOT_OWNED_ORDER)
    db.close()
    assert (
        main(
            [
                "safe-mode",
                "--run-id",
                "r",
                "--db",
                str(path),
                "--release",
                "--reason",
                "手動確認外部單已撤",
                "--released-by",
                "joy",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "released by joy" in out
    assert "§13.6 rule 3" in out  # the does-not-resume-trading reminder
    with Database(path) as db2:
        row = repo.get_scheduler_state(db2.conn, "r")
        assert row["safe_mode_type"] is None
        events = repo.iter_safe_mode_events(db2.conn, "r")
        assert events[-1]["event_type"] == "safe_mode_released"
        assert events[-1]["released_by"] == "joy"
        assert events[-1]["detail"] == "手動確認外部單已撤"


def test_release_without_a_reason_is_refused(store, capsys):
    path, db = store
    _enter(db, "manual", REASON_NON_BOT_OWNED_ORDER)
    db.close()
    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--release"]) == 1
    assert "--reason" in capsys.readouterr().err
    with Database(path) as db2:
        assert repo.get_scheduler_state(db2.conn, "r")["safe_mode_type"] == "manual"


def test_release_refuses_a_recoverable_safe_mode(store, capsys):
    path, db = store
    _enter(db, "recoverable", REASON_WS_DISCONNECT)
    db.close()
    assert (
        main(["safe-mode", "--run-id", "r", "--db", str(path), "--release", "--reason", "x"]) == 1
    )
    assert "RECOVERABLE" in capsys.readouterr().err


def test_release_refuses_a_run_not_in_safe_mode(store, capsys):
    path, db = store
    db.close()
    assert (
        main(["safe-mode", "--run-id", "r", "--db", str(path), "--release", "--reason", "x"]) == 1
    )
    assert "not in safe mode" in capsys.readouterr().err


def test_unknown_run_and_missing_store_are_named_errors(store, capsys, tmp_path):
    path, db = store
    db.close()
    assert main(["safe-mode", "--run-id", "nope", "--db", str(path)]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main(["safe-mode", "--run-id", "r", "--db", str(tmp_path / "absent.db")]) == 1
    assert "does not exist" in capsys.readouterr().err
