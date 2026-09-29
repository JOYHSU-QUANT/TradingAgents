"""Tests for the ``validate`` subcommand and the store-open refusals."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main
from contrib.hyperliquid_perp.paper import accounting as paper_accounting
from contrib.hyperliquid_perp.persistence.db import Database, connect
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from ..conftest import insert_decision_attempts, unreadable, unwritable
from .conftest import make_live_run, paper_argv, seed_db

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_validate_exit_codes(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    # Consistent but far short of 30 cycles -> 4 ("keep running").
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 4
    assert "phase3_ready: no" in capsys.readouterr().out

    # Integrity failure (orphan fill) -> 5 ("store is broken"), not 4.
    db = Database(path)
    paper_accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|ghost|0",
        order_id="ghost-order",
        symbol="BTC",
        side="buy",
        qty=D("0.001"),
        price=D(50000),
        fee_rate=D(0),
        timestamp=_T0,
    )
    db.close()
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 5
    assert "orphan fill" in capsys.readouterr().out


def test_validate_exit_5_when_replay_raises(tmp_path, capsys):
    # A store corrupt enough to crash the replay itself is the strongest
    # "investigate the store" signal: exit 5 with a partial report (ledger
    # metrics n/a), not the generic exit-2 crash lane.
    path, db = seed_db(tmp_path)
    paper_accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|f|0",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=D("0.001"),
        price=D(50000),
        fee_rate=D(0),
        timestamp=_T0,
    )
    with db.transaction() as conn:
        conn.execute("UPDATE fills SET fill_price = 'garbage' WHERE run_id = 'r'")
    db.close()
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 5
    out = capsys.readouterr().out
    assert "accounting replay raised" in out
    assert "realized_pnl: n/a" in out


def test_validate_exit_5_on_a_file_that_is_not_a_database(tmp_path, capsys):
    # A file that is not SQLite at all takes the same exit-5 "investigate the
    # store" verdict — not exit 2 ("tool bug") and not exit 1 (operator error).
    # A store this process merely cannot READ is the opposite verdict and is
    # pinned right below (issue #210); the two used to share both this exit code
    # and this test's old name.
    bogus = tmp_path / "bogus.db"
    bogus.write_text("this is not a database", encoding="utf-8")
    assert cli_main(["validate", "--run-id", "r", "--db", str(bogus)]) == 5
    assert "store integrity failure" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["validate", "paper"])
def test_a_db_that_cannot_be_read_is_a_named_exit_1(tmp_path, capsys, paper_seams, command):
    # Issue #210 through the CLI an operator actually types. Exit 5 means "the
    # ledger does not add up — investigate the accounting", which a file
    # permission is not; `validate` reached it because it catches sqlite3.Error
    # around the open. An owning command was worse: main()'s last resort, whose
    # `fatal: unexpected error:` line names the exception and nothing about the
    # --db, at exit 2. Both are the operator-error lane now.
    path, db = seed_db(tmp_path)
    db.close()
    argv = (
        ["validate", "--run-id", "r", "--db", str(path)]
        if command == "validate"
        else paper_argv(path, run_id="r", config=paper_seams)
    )
    with unreadable(path):
        rc = cli_main(argv)
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not be opened for reading" in err
    assert str(path) in err
    assert "store integrity failure" not in err  # the code that means something else


@pytest.mark.parametrize("command", ["validate", "paper"])
def test_a_db_that_cannot_be_written_is_a_named_exit_1(tmp_path, capsys, paper_seams, command):
    # Issue #235, the other side of #210 and measured through the CLI an
    # operator actually types. A zero-length --db that reads fine but denies
    # writes gets past every guard — there is nothing wrong with the PATH — and
    # then dies inside connect(), because opening a store WRITES: `PRAGMA
    # journal_mode = WAL` puts the mode in the database header. `validate`
    # catches sqlite3.Error around the open and called that an exit-5 `store
    # integrity failure`, whose meaning is "the ledger does not add up,
    # investigate the accounting" — a file permission borrowing the one verdict
    # the RUNBOOK says to treat as a data-integrity incident. An owning command
    # reached main()'s exit-2 last resort instead.
    path = tmp_path / "ro.db"
    path.touch()
    argv = (
        ["validate", "--run-id", "r", "--db", str(path)]
        if command == "validate"
        else paper_argv(path, run_id="r", config=paper_seams)
    )
    with unwritable(path):
        rc = cli_main(argv)
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not be opened as a store" in err
    assert str(path) in err
    assert "store integrity failure" not in err  # the ledger verdict, not this one


@pytest.mark.parametrize("command", ["validate", "paper"])
def test_a_db_whose_content_is_in_its_log_is_a_named_exit_1(
    tmp_path, capsys, paper_seams, command
):
    # Issue #236 through the CLI. A zero-length main file beside a hot -wal was
    # read as an empty store and built into in full, destroying the log on the
    # way in — the issue-#174 harm reached through the one door that guard
    # cannot see, since it asks the MAIN file what it holds. Both commands now
    # refuse by name, and the assertion that matters is on the FILE.
    path = tmp_path / "x.db"
    path.touch()
    log = tmp_path / "x.db-wal"
    body = b"\x37\x7f\x06\x82" + bytes(20000)
    log.write_bytes(body)
    argv = (
        ["validate", "--run-id", "r", "--db", str(path)]
        if command == "validate"
        else paper_argv(path, run_id="r", config=paper_seams)
    )

    rc = cli_main(argv)

    err = capsys.readouterr().err
    assert rc == 1
    assert "x.db-wal" in err
    assert "store integrity failure" not in err
    assert log.read_bytes() == body  # the whole point: still there
    assert path.stat().st_size == 0


def test_validate_operator_errors_exit_1(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    assert cli_main(["validate", "--run-id", "ghost", "--db", str(path)]) == 1
    assert cli_main(["validate", "--run-id", "r", "--db", str(tmp_path / "nope.db")]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_read_only_commands_refuse_a_store_that_needs_migrating(tmp_path, capsys):
    # The offline commands take NO run lease, so the old behaviour — open, and
    # migrate on the way in — meant "just preview the numbers" on the deploy box
    # silently upgraded the store the running daemon owns, leaving that daemon
    # writing through a schema it does not know. Both must refuse (exit 1, the
    # operator-error lane) and leave the store exactly as they found it.
    path, db = seed_db(tmp_path)
    with db.transaction() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,))
    db.close()

    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 1
    err = capsys.readouterr().err
    assert "store schema is v" in err and "will not migrate" in err

    out_dir = tmp_path / "exp"
    assert (
        cli_main(["export", "--run-id", "r", "--output-dir", str(out_dir), "--db", str(path)]) == 1
    )
    assert "store schema is v" in capsys.readouterr().err

    # Neither command wrote the missing migration back: the store is still the
    # version the daemon is running against.
    probe = connect(path)
    recorded = probe.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    probe.close()
    assert recorded == SCHEMA_VERSION - 1

    # Negative control: with the bookkeeping restored the very same command runs —
    # the refusal is the version check, not a broken store path. (Written raw,
    # not via a migrating open: this store's TABLES are already current, only its
    # schema_migrations row was removed to stage the "needs upgrading" read.)
    restore = connect(path)
    restore.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, "2026-07-30T00:00:00+00:00"),
    )
    restore.close()
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 4


def test_validate_exit_0_when_phase3_ready(tmp_path, capsys):
    # A consistent store with >= 30 completed cycles is the exit-0 path.
    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"] * 30, start=_T0)
    db.close()
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 0
    assert "phase3_ready: yes" in capsys.readouterr().out


def test_validate_dispatches_live_run(tmp_path, capsys):
    dbp = make_live_run(tmp_path)
    rc = cli_main(["validate", "--run-id", "live-BTC", "--db", str(dbp)])
    out = capsys.readouterr().out
    assert "execution_mode: testnet_live" in out
    # A freshly-initialized live run is internally consistent (clean replay) but
    # short of the gate (0 cycles, no smoke) → exit 4 "keep running", not 5.
    assert rc == 4
    assert "shortfall:" in out


def test_validate_live_missing_run_exits_1(tmp_path, capsys):
    dbp = make_live_run(tmp_path)
    rc = cli_main(["validate", "--run-id", "nope", "--db", str(dbp)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err
