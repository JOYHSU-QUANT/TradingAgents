"""Tests for the paper daemon's post-cycle export and its breadcrumbs."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import (
    _UNVERIFIED_MARKER,
    _mark_export_verification,
    _post_cycle_export,
)
from contrib.hyperliquid_perp.persistence import repository as repo

from .conftest import seed_db

D = Decimal


def test_mark_export_verification_writes_and_clears(tmp_path):
    export_dir = tmp_path / "exp"
    export_dir.mkdir()
    marker = export_dir / _UNVERIFIED_MARKER
    # Replay did not verify -> in-band marker with the reason.
    _mark_export_verification(export_dir, "r", replay_ok=False, reason="ledger drift")
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "run_id": "r",
        "replay_verified": False,
        "reason": "ledger drift",
    }
    # A later healthy cycle reuses the dir and must clear the stale marker.
    _mark_export_verification(export_dir, "r", replay_ok=True, reason=None)
    assert not marker.exists()
    # Clearing an already-absent marker is a no-op (missing_ok).
    _mark_export_verification(export_dir, "r", replay_ok=True, reason=None)
    assert not marker.exists()


def test_post_cycle_export_marks_unverified_on_replay_mismatch(tmp_path, monkeypatch):
    path, db = seed_db(tmp_path)
    export_dir = tmp_path / "exp"
    from contrib.hyperliquid_perp.runtime import accounting as acc_mod

    # Force a replay inconsistency without corrupting the store.
    monkeypatch.setattr(
        acc_mod,
        "replay",
        lambda db, *, run_id: SimpleNamespace(is_consistent=False, mismatch_detail="boom"),
    )
    assert _post_cycle_export(db, "r", export_dir) is False
    marker = export_dir / _UNVERIFIED_MARKER
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "boom"
    # Post-mortem data is still published — all 8 CSVs written alongside the marker.
    assert len(list(export_dir.glob("*.csv"))) == 8

    # A subsequent healthy cycle re-exports and clears the marker.
    monkeypatch.setattr(
        acc_mod,
        "replay",
        lambda db, *, run_id: SimpleNamespace(is_consistent=True, mismatch_detail=None),
    )
    assert _post_cycle_export(db, "r", export_dir) is True
    assert not marker.exists()
    db.close()


def test_post_cycle_export_persists_status_breadcrumbs(tmp_path, monkeypatch):
    """Export outcomes land durably on scheduler_state (ok and failed lanes)."""
    from contrib.hyperliquid_perp.cli import _post_cycle_export
    from contrib.hyperliquid_perp.persistence import export as export_mod

    path, db = seed_db(tmp_path)
    out = tmp_path / "exports"
    assert _post_cycle_export(db, "r", out) is True
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_export_status"] == "ok"
    assert state["last_export_error"] is None
    assert state["last_export_at"] is not None

    def boom(*args, **kwargs):
        raise export_mod.ExportError("disk full")

    monkeypatch.setattr(export_mod, "export_run", boom)
    _post_cycle_export(db, "r", out)  # export failure must not raise
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_export_status"] == "failed"
    assert "disk full" in state["last_export_error"]
    db.close()


def test_post_cycle_export_persists_replay_breadcrumbs(tmp_path, monkeypatch):
    """Replay outcomes land durably on scheduler_state (ok/mismatch/failed lanes)."""
    from contrib.hyperliquid_perp.cli import _post_cycle_export
    from contrib.hyperliquid_perp.persistence.models import AccountLedger
    from contrib.hyperliquid_perp.runtime import accounting as acc_mod

    path, db = seed_db(tmp_path)
    out = tmp_path / "exports"
    assert _post_cycle_export(db, "r", out) is True
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "ok"
    assert state["last_replay_error"] is None
    assert state["last_replay_at"] is not None

    # Corrupt the materialized ledger: replay now contradicts it (mismatch lane).
    with db.transaction() as conn:
        repo.upsert_current_account_state(conn, "r", AccountLedger(wallet_balance=D(123)))
    assert _post_cycle_export(db, "r", out) is False
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "mismatch"
    assert "account_matches" in state["last_replay_error"]

    # Replay itself raising is the "failed" lane (books unverifiable).
    def boom(db_, *, run_id):
        raise RuntimeError("corrupt decimal text")

    monkeypatch.setattr(acc_mod, "replay", boom)
    assert _post_cycle_export(db, "r", out) is False
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "failed"
    assert "corrupt decimal text" in state["last_replay_error"]
    db.close()


def test_config_drift_breadcrumb_roundtrip_and_vocabulary(tmp_path):
    from contrib.hyperliquid_perp.cli import _stamp_breadcrumb

    path, db = seed_db(tmp_path)
    _stamp_breadcrumb(db, "r", "config_drift", "drift", "risk differs")
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "drift"
    assert state["last_config_drift_error"] == "risk differs"
    assert state["last_config_drift_at"] is not None

    _stamp_breadcrumb(db, "r", "config_drift", "ok", None)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "ok"
    assert state["last_config_drift_error"] is None

    # The write boundary rejects off-vocabulary statuses like its siblings.
    with pytest.raises(ValueError, match="last_config_drift_status"), db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", last_config_drift_status="weird")
    db.close()


def test_post_cycle_export_breadcrumb_write_failure_is_fail_loud(tmp_path, monkeypatch):
    # Settled 9th-loop lane: the durable breadcrumb is trading-write-grade — a
    # stamp failure must escape _post_cycle_export (killing the loop, exit 2
    # via main), not be contained like the export/replay outcomes it records.
    import sqlite3

    import contrib.hyperliquid_perp.cli as cli_mod

    path, db = seed_db(tmp_path)

    def raising_stamp(db_, run_id, kind, status, error):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli_mod.paper_export, "_stamp_breadcrumb", raising_stamp)
    with pytest.raises(sqlite3.OperationalError):
        _post_cycle_export(db, "r", tmp_path / "exp")
    db.close()
