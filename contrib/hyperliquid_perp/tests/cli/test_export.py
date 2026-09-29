"""Tests for the ``export`` subcommand."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main
from contrib.hyperliquid_perp.common import store_layout

from ..conftest import insert_decision_attempts, stamp_prompt_regimes, write_payload
from .conftest import seed_db

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_export_subcommand_writes_eight_csvs(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    out = tmp_path / "exp"
    assert cli_main(["export", "--run-id", "r", "--output-dir", str(out), "--db", str(path)]) == 0
    files = sorted(p.name for p in out.glob("*.csv"))
    assert len(files) == 8
    with (out / "account_snapshots.csv").open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header[0] == "timestamp"


def test_export_unknown_run_exits_1(tmp_path, capsys):
    path, db = seed_db(tmp_path)
    db.close()
    rc = cli_main(
        ["export", "--run-id", "ghost", "--output-dir", str(tmp_path / "e"), "--db", str(path)]
    )
    assert rc == 1
    assert "export_failed" in capsys.readouterr().err


def test_export_backfill_flag_stamps_null_fingerprints_before_writing_the_csvs(tmp_path, capsys):
    # Issue #163: the one write ``export`` can make. A pre-v11 row (NULL
    # fingerprint, payload on disk) is stamped from the payload's own format
    # text BEFORE the CSVs are written, so the exported set carries the value;
    # the pass reports its counts on stderr. The trust rules and counters are
    # pinned in tests/persistence/test_backfill.py — this is the wiring.
    from contrib.hyperliquid_perp.domains.perp.target_decision import format_fingerprint

    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"], start=_T0)
    payload, digest = write_payload(
        tmp_path / "payload.json", {"format_instructions": "the block as the model saw it"}
    )
    stamp_prompt_regimes(db, [("phase2-target-v4", "price|market", None, payload, digest)])
    db.close()

    out = tmp_path / "exp"
    rc = cli_main(
        [
            "export",
            "--run-id",
            "r",
            "--output-dir",
            str(out),
            "--db",
            str(path),
            "--backfill-format-fingerprint",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert (
        "format_fingerprint backfill for 'r': stamped=1 pre_v10=0 missing_payload=0"
        " unreadable=0 unverified=0" in err
    )
    with (out / "ai_inputs.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["format_fingerprint"] for row in rows] == [
        format_fingerprint("the block as the model saw it")
    ]

    # An unknown run with the flag: the standard refusal, and no backfill
    # line claiming zeros about a run that does not exist.
    rc = cli_main(
        [
            "export",
            "--run-id",
            "ghost",
            "--output-dir",
            str(out),
            "--db",
            str(path),
            "--backfill-format-fingerprint",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "export_failed" in err  # the same refusal as without the flag
    assert "format_fingerprint backfill" not in err


def test_export_backfill_names_a_locked_store_instead_of_a_traceback(tmp_path, capsys, monkeypatch):
    # The pass takes the store's write lock and RUNBOOK §6 says a running
    # daemon is fine — so a lock held past busy_timeout must be the named
    # exit 1 every other refusal here is, not a traceback exit 2; and the
    # export does not run over a pass that did not finish.
    import contrib.hyperliquid_perp.persistence.backfill as backfill_mod

    path, db = seed_db(tmp_path)
    db.close()

    def locked(db, *, run_id, payload_root=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(backfill_mod, "backfill_format_fingerprints", locked)
    out = tmp_path / "exp"
    rc = cli_main(
        [
            "export",
            "--run-id",
            "r",
            "--output-dir",
            str(out),
            "--db",
            str(path),
            "--backfill-format-fingerprint",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: format_fingerprint backfill failed — database is locked" in err
    assert not out.exists()


def test_export_payload_root_reads_a_copied_stores_payloads_by_name(tmp_path, capsys):
    # Issue #197: a store copied off the host that wrote it records absolute
    # paths that exist nowhere here, so the pass could only count every row
    # as missing_payload. ``--payload-root DIR`` reads each row's payload by
    # its recorded FILE NAME under DIR; the remap rules and the hash rule
    # under a root are pinned in tests/persistence/test_backfill.py — this is
    # the wiring, plus the two ways the option itself can be misused.
    from contrib.hyperliquid_perp.domains.perp.target_decision import format_fingerprint

    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"], start=_T0)
    # The daemon's own path shape on the Linux host, and the copied file here.
    copied, digest = write_payload(
        tmp_path / "copied" / "BTC-20260706T120000_000000Z.json",
        {"format_instructions": "the block as the model saw it"},
    )
    recorded = "/srv/hl/payloads/BTC-20260706T120000_000000Z.json"
    stamp_prompt_regimes(db, [("phase2-target-v4", "price|market", None, recorded, digest)])
    db.close()
    out = tmp_path / "exp"
    base = ["export", "--run-id", "r", "--output-dir", str(out), "--db", str(path)]

    # The option needs the pass it modifies: argparse's usage error (exit 2),
    # not a silent no-op that read as "nothing to backfill". An empty root is
    # the same usage error: ``Path("")`` is cwd and would pass the directory
    # check — an unset shell variable must not quietly become the root.
    for argv, wording in [
        ([*base, "--payload-root", str(tmp_path / "copied")], "only applies with"),
        ([*base, "--backfill-format-fingerprint", "--payload-root", ""], "needs a directory, got ''"),
    ]:
        with pytest.raises(SystemExit) as excinfo:
            cli_main(argv)
        assert excinfo.value.code == 2
        assert f"--payload-root {wording}" in capsys.readouterr().err

    # A root that is not a directory is named (exit 1) rather than reported as
    # N x missing_payload — a typo would otherwise read like a store whose
    # payloads really are gone. Nothing was exported over it.
    rc = cli_main(
        [*base, "--backfill-format-fingerprint", "--payload-root", str(tmp_path / "typo")]
    )
    assert rc == 1
    assert "error: --payload-root" in capsys.readouterr().err
    assert not out.exists()

    # Without the option on this copied store: the recorded path is what is
    # read, so nothing is provable — and since EVERY payload is missing and
    # nothing sits beside this store in the daemons' layout (the copy went
    # elsewhere), the count line is followed by a hint naming the option and
    # that directory, so the operator knows both where a copy is found
    # without the flag and that this one was not. The no-flag read of a copy
    # that DID keep the layout is the sibling test below.
    candidate = str(store_layout.payload_dir(path, "r"))
    assert cli_main([*base, "--backfill-format-fingerprint"]) == 0
    err = capsys.readouterr().err
    assert "stamped=0 pre_v10=0 missing_payload=1" in err
    assert "note:" not in err
    assert "hint: every payload is missing at its recorded path" in err
    assert f"there is no payload directory beside this store at {candidate}; a store copied" in err
    assert "--payload-root pointing at that run's payload directory" in err

    # A root at the wrong LEVEL (the copied ``payloads/`` parent rather than
    # the run's own directory under it) is a directory, so it cannot be
    # refused — but nothing under it matches a recorded name, and that is
    # far likelier the operator's level than a tree that lost its files, so
    # the count line is followed by the root-side hint with the same candidate.
    rc = cli_main([*base, "--backfill-format-fingerprint", "--payload-root", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "stamped=0 pre_v10=0 missing_payload=1" in err
    assert "hint: no payload under --payload-root" in err
    assert f"<db dir>/payloads/r/ (here: {candidate})" in err

    rc = cli_main(
        [*base, "--backfill-format-fingerprint", "--payload-root", str(tmp_path / "copied")]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "stamped=1 pre_v10=0 missing_payload=0 unreadable=0 unverified=0" in err
    assert "hint:" not in err
    with (out / "ai_inputs.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["format_fingerprint"] for row in rows] == [
        format_fingerprint("the block as the model saw it")
    ]
    assert [row["input_payload_path"] for row in rows] == [recorded]  # the row itself is untouched


def test_export_backfill_reads_the_payloads_beside_a_store_moved_with_them(tmp_path, capsys):
    """Issue #221: a copy that kept the daemons' layout needs no ``--payload-root``.

    The daemons write ``<db dir>/payloads/<run_id>/`` (one recipe, in
    ``common.store_layout``), so a backup that moved the db and that directory
    together has its payloads exactly where the reader can derive from
    ``--db`` and ``--run-id``. The first pass still reads the recorded paths
    (a store on its own host must not be second-guessed); only when EVERY one
    is missing and that directory exists does the pass run again under it —
    said on stderr so the two count lines read as one story. The sibling test
    above pins the case where nothing sits beside the store.
    """
    from contrib.hyperliquid_perp.domains.perp.target_decision import format_fingerprint

    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"], start=_T0)
    beside = store_layout.payload_dir(path, "r")
    _copied, digest = write_payload(
        beside / "BTC-20260706T120000_000000Z.json",
        {"format_instructions": "the block as the model saw it"},
    )
    recorded = "/srv/hl/payloads/BTC-20260706T120000_000000Z.json"
    stamp_prompt_regimes(db, [("phase2-target-v4", "price|market", None, recorded, digest)])
    db.close()
    out = tmp_path / "exp"
    base = ["export", "--run-id", "r", "--output-dir", str(out), "--db", str(path)]

    assert cli_main([*base, "--backfill-format-fingerprint"]) == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith(("format_fingerprint", "note:"))]
    # The recorded-path pass first, then the note, then the pass beside the
    # store — in that order, so an operator reading top-down sees why the
    # second count line exists.
    assert len(lines) == 3, err
    assert "stamped=0 pre_v10=0 missing_payload=1" in lines[0]
    assert lines[1] == (
        "note: every payload is missing at its recorded path; reading them under "
        f"the directory beside this store instead ({beside})"
    )
    assert "stamped=1 pre_v10=0 missing_payload=0 unreadable=0 unverified=0" in lines[2]
    assert "hint:" not in err
    with (out / "ai_inputs.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["format_fingerprint"] for row in rows] == [
        format_fingerprint("the block as the model saw it")
    ]
    assert [row["input_payload_path"] for row in rows] == [recorded]  # the row itself is untouched

    # A second run finds the cell already stamped: the recorded-path pass has
    # nothing left to look for, so it neither retries nor hints — the retry is
    # gated on missing_payload > 0, not on stamped == 0 alone.
    assert cli_main([*base, "--backfill-format-fingerprint"]) == 0
    err = capsys.readouterr().err
    assert "stamped=0 pre_v10=0 missing_payload=0" in err
    assert "note:" not in err and "hint:" not in err


def test_export_backfill_hints_when_the_directory_beside_the_store_matches_nothing(
    tmp_path, capsys
):
    # The directory exists but holds no file of a recorded name (a different
    # run's payloads, say): the retry happens, finds nothing, and the hint
    # says so — naming the flag for a copy that did not keep the layout —
    # rather than the "no payload directory beside this store" sentence,
    # which would be false here.
    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"], start=_T0)
    _path, digest = write_payload(tmp_path / "elsewhere.json", {"format_instructions": "the block"})
    recorded = "/srv/hl/payloads/BTC-20260706T120000_000000Z.json"
    stamp_prompt_regimes(db, [("phase2-target-v4", "price|market", None, recorded, digest)])
    db.close()
    beside = store_layout.payload_dir(path, "r")
    write_payload(beside / "ETH-20260706T120000_000000Z.json", {"format_instructions": "x"})

    rc = cli_main(
        ["export", "--run-id", "r", "--output-dir", str(tmp_path / "exp"), "--db", str(path)]
        + ["--backfill-format-fingerprint"]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert err.count("stamped=0 pre_v10=0 missing_payload=1") == 2
    assert "note: every payload is missing at its recorded path" in err
    assert f"hint: no payload beside this store ({beside}) matched a recorded file name" in err
    assert "needs --payload-root pointing at that run's payload directory" in err

    # An explicit --payload-root is the operator saying where the files are:
    # even with that same directory beside the store, a root that matches
    # nothing is reported as such (one count line, the root-side hint) and
    # never silently overridden by a read from somewhere they did not point.
    rc = cli_main(
        ["export", "--run-id", "r", "--output-dir", str(tmp_path / "exp2"), "--db", str(path)]
        + ["--backfill-format-fingerprint", "--payload-root", str(tmp_path)]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert err.count("stamped=0 pre_v10=0 missing_payload=1") == 1
    assert "note:" not in err
    assert "hint: no payload under --payload-root" in err


def test_export_backfill_does_not_retry_beside_the_store_over_a_partly_readable_pass(
    tmp_path, capsys
):
    # One row's payload is missing, another's is where it was recorded but
    # does not hash to its row (unverified): the files ARE where the pass
    # looked, and the counts already say what is wrong with them. No retry
    # under the directory beside the store even though it exists and holds
    # the missing row's file, and no hint — either would send the operator
    # after a moved store when the problem is an edited file.
    path, db = seed_db(tmp_path)
    insert_decision_attempts(db, ["completed", "completed"], start=_T0)
    beside = store_layout.payload_dir(path, "r")
    _moved, digest_a = write_payload(
        beside / "BTC-20260706T120000_000000Z.json", {"format_instructions": "block a"}
    )
    present, _digest_b = write_payload(tmp_path / "present.json", {"format_instructions": "block b"})
    stamp_prompt_regimes(
        db,
        [
            ("phase2-target-v4", "price|market", None, "/srv/hl/BTC-20260706T120000_000000Z.json", digest_a),
            ("phase2-target-v4", "price|market", None, present, "sha256:not-what-is-on-disk"),
        ],
    )
    db.close()

    rc = cli_main(
        ["export", "--run-id", "r", "--output-dir", str(tmp_path / "exp"), "--db", str(path)]
        + ["--backfill-format-fingerprint"]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert err.count("format_fingerprint backfill for") == 1
    assert "stamped=0 pre_v10=0 missing_payload=1 unreadable=0 unverified=1" in err
    assert "note:" not in err and "hint:" not in err


def test_export_backfill_names_a_lock_hit_by_the_retry_beside_the_store(tmp_path, capsys, monkeypatch):
    # The second pass takes the write lock like the first; a lock held past
    # busy_timeout on THAT pass is the same named exit 1 — the retry lives
    # inside the one sqlite3.Error lane, not after it — and no export runs
    # over a pass that did not finish.
    import contrib.hyperliquid_perp.persistence.backfill as backfill_mod

    path, db = seed_db(tmp_path)
    db.close()
    store_layout.payload_dir(path, "r").mkdir(parents=True)
    roots = []

    def first_finds_nothing_then_locked(db, *, run_id, payload_root=None):
        roots.append(payload_root)
        if len(roots) == 1:
            return backfill_mod.FingerprintBackfill(
                stamped=0, pre_v10=0, missing_payload=1, unreadable=0, unverified=0
            )
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(backfill_mod, "backfill_format_fingerprints", first_finds_nothing_then_locked)
    out = tmp_path / "exp"
    rc = cli_main(
        ["export", "--run-id", "r", "--output-dir", str(out), "--db", str(path)]
        + ["--backfill-format-fingerprint"]
    )
    assert rc == 1
    assert roots == [None, store_layout.payload_dir(path, "r")]
    err = capsys.readouterr().err
    assert "note: every payload is missing at its recorded path" in err
    assert "error: format_fingerprint backfill failed — database is locked" in err
    assert not out.exists()
