"""Tests for the ``status`` subcommand (dashboard plan PR 2a)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main, offline
from contrib.hyperliquid_perp.cli.offline import _cmd_status
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.runtime import accounting
from contrib.hyperliquid_perp.status import SCHEMA_VERSION

from ..conftest import insert_decision_attempts, insert_minimal_account_snapshot
from .conftest import seed_db

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_NOW = datetime(2026, 7, 7, tzinfo=timezone.utc)


def _now() -> datetime:
    return _NOW


def test_status_prints_lines_by_default_and_a_document_with_json(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["api_failed", "in_progress"], start=_T0)
    db.close()
    assert _cmd_status(["--run-id", "r", "--db", str(path)], now=_now) == 0
    out = capsys.readouterr().out
    assert out.startswith("run_id: r\nmode: paper\n")
    assert f"generated_at: {_NOW.isoformat()}" in out
    assert "cycles: 2 (in_progress 1, completed 0, api_failed 1, invalid_output 0)" in out

    assert _cmd_status(["--run-id", "r", "--db", str(path), "--json"], now=_now) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["run_id"] == "r"
    assert document["generated_at"] == _NOW.isoformat()
    assert document["cycles"]["total"] == 2


def test_status_stamps_the_document_with_the_real_clock_in_utc(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    before = datetime.now(timezone.utc)
    assert cli_main(["status", "--run-id", "r", "--db", str(path), "--json"]) == 0
    generated = datetime.fromisoformat(json.loads(capsys.readouterr().out)["generated_at"])
    assert generated.utcoffset() == timedelta(0)
    assert before <= generated <= datetime.now(timezone.utc)


def test_status_defaults_to_the_run_whose_scheduler_row_was_touched_last(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    accounting.initialize_run(
        db, run_id="r2", mode="paper", initial_balance_usdc=Decimal(500), schema_version=1
    )
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", updated_at=_T0)
        repo.upsert_scheduler_state(conn, "r2", updated_at=_T0 + timedelta(hours=1))
    db.close()
    assert cli_main(["status", "--db", str(path)]) == 0
    assert capsys.readouterr().out.startswith("run_id: r2\n")
    assert cli_main(["status", "--db", str(path), "--run-id", "r"]) == 0
    assert capsys.readouterr().out.startswith("run_id: r\n")


def test_status_without_a_run_id_refuses_a_store_no_daemon_has_scheduled_in(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    assert cli_main(["status", "--db", str(path)]) == 1
    err = capsys.readouterr().err
    assert "no run in" in err and "has a scheduler row yet; pass --run-id, or --db" in err


def test_status_refuses_a_missing_store_or_run_by_name(tmp_path, capsys):
    missing = tmp_path / "none.db"
    assert cli_main(["status", "--db", str(missing)]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()  # never created implicitly
    path, db = seed_db(tmp_path)
    db.close()
    assert cli_main(["status", "--run-id", "ghost", "--db", str(path)]) == 1
    assert "run 'ghost' does not exist in" in capsys.readouterr().err


def test_status_recent_caps_the_tails_and_refuses_a_negative(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["api_failed"] * 3, start=_T0)
    db.close()
    assert cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", "1"]) == 0
    assert "recent cycles (1 of 3, newest first):" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", "-1"])
    assert exc.value.code == 2
    assert "--recent must be 0 or more" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", "five"])
    err = capsys.readouterr().err
    assert "--recent takes a whole number, got 'five'" in err and "_recent" not in err
    # Above SQLite's LIMIT the bind itself fails; that is a typo, not a broken store.
    with pytest.raises(SystemExit):
        cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", str(2**64)])
    assert "--recent must be at most" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", str(2**63)])
    assert "--recent must be at most" in capsys.readouterr().err
    # The two ends of the range are accepted.
    assert cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", "0"]) == 0
    assert "recent cycles: none" in capsys.readouterr().out
    largest = str(2**63 - 1)
    assert cli_main(["status", "--run-id", "r", "--db", str(path), "--recent", largest]) == 0
    assert "recent cycles (3, newest first):" in capsys.readouterr().out


def test_status_names_a_store_whose_content_does_not_hold_up_under_validates_code(
    tmp_path, capsys, monkeypatch
):
    # The store opened; what it holds could not be read. validate's exit 5,
    # not the exit-2 crash lane: PR 3's exporter keys its alert on the code.
    path, db = seed_db(tmp_path)
    db.close()

    def _torn(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(offline, "read_status", _torn)
    assert cli_main(["status", "--run-id", "r", "--db", str(path)]) == 5
    assert "store integrity failure" in capsys.readouterr().err


def test_status_names_a_file_that_is_not_a_database_under_validates_code(tmp_path, capsys):
    # The open itself fails on a file that is not SQLite: exit 5 as validate
    # gives it, not the exit-2 crash lane (the open is inside the same try).
    garbage = tmp_path / "bad.db"
    garbage.write_bytes(b"not a database" * 100)
    assert cli_main(["status", "--run-id", "r", "--db", str(garbage)]) == 5
    assert "store integrity failure" in capsys.readouterr().err


def test_status_names_a_cell_no_decimal_accepts_under_validates_code(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    with db.transaction() as conn:
        insert_minimal_account_snapshot(
            conn, run_id="r", timestamp=_T0, account_equity="not-a-number"
        )
    db.close()
    assert cli_main(["status", "--run-id", "r", "--db", str(path)]) == 5
    assert "store integrity failure" in capsys.readouterr().err


def test_status_refuses_a_run_without_its_ledger_row_under_validates_code(tmp_path, capsys):
    # initialize_run writes the run row and its ledger together; a run row
    # alone is a damaged store, not an empty account.
    path, db = seed_db(tmp_path)
    with db.transaction() as conn:
        conn.execute("DELETE FROM current_account_state WHERE run_id = ?", ("r",))
    db.close()
    assert cli_main(["status", "--run-id", "r", "--db", str(path)]) == 5
    assert "store integrity failure" in capsys.readouterr().err
