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


def test_status_limit_widens_and_zero_prints_the_full_history(store, capsys):
    # A long manual episode (one reason_added row per distinct reason) can
    # push the episode's anchoring safe_mode_entered row out of the default
    # 10-row tail — --limit widens it, 0 prints everything (decided
    # 2026-07-17).
    path, db = store
    manager = SafeModeManager(db=db, run_id="r", gate=None, clock=ManualClock(_NOW))
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    for i in range(12):
        manager.enter("manual", f"distinct_reason_{i}")  # one reason_added each
    db.close()

    assert main(["safe-mode", "--run-id", "r", "--db", str(path)]) == 4
    default_out = capsys.readouterr().out
    assert "safe_mode_entered" not in default_out  # the anchor fell off the tail
    assert default_out.count("\n  ") == 10  # exactly ten history rows

    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--limit", "0"]) == 4
    full_out = capsys.readouterr().out
    assert "safe_mode_entered" in full_out  # the anchor is visible again
    assert full_out.count("\n  ") == 13


def test_status_rejects_a_negative_limit(store, capsys):
    path, db = store
    db.close()
    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--limit", "-1"]) == 1
    assert "--limit must be >= 0" in capsys.readouterr().err


def test_release_rejects_limit(store, capsys):
    # Same named-rejection discipline as --reason on the status path.
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
                "x",
                "--limit",
                "5",
            ]
        )
        == 1
    )
    assert "--limit applies only with --status" in capsys.readouterr().err
    with Database(path) as db2:
        assert repo.get_scheduler_state(db2.conn, "r")["safe_mode_type"] == "manual"


# -- --stamp-case: the §12.3 human-disposition path ---------------------------


def _open_case(db, *, case_type="fill_malformed", exchange_value="unparsed-deadbeef"):
    """One un-actioned case — the shape that holds the verdict unclean."""
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type=case_type,
            exchange_value=exchange_value,
            timestamp=_NOW,
        )
    return [
        row
        for row in repo.iter_exchange_reconciliation_events(db.conn, "r")
        if row["action_taken"] is None
    ][-1]["event_id"]


def test_status_lists_open_cases_with_the_ids_stamping_needs(store, capsys):
    # --status is the ONLY place a case's event_id surfaces: the safe-mode
    # detail names a count, not ids. Without this listing --stamp-case would be
    # unreachable short of hand-written SQL against the live store.
    path, db = store
    event_id = _open_case(db)
    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--status"]) == 0
    out = capsys.readouterr().out
    assert "open reconciliation cases (1;" in out
    assert f"[{event_id}]" in out
    assert "fill_malformed" in out


def test_stamping_a_case_records_the_disposition_and_unblocks_it(store, capsys):
    # A digest-keyed malformed sighting can NEVER be auto-resolved (nothing will
    # ever join it to a booked fill), and an un-actioned case holds the verdict
    # unclean — so without this path one unparseable payload wedges the run in
    # safe mode forever.
    path, db = store
    event_id = _open_case(db)
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            "reviewed evidence: payload was garbage",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"case {event_id} (fill_malformed) stamped" in out
    assert "trading does not resume yet" in out
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        assert row["action_taken"] == "reviewed evidence: payload was garbage"


def test_stamping_requires_an_action_and_refuses_to_overwrite_one(store, capsys):
    path, db = store
    event_id = _open_case(db)
    assert (
        main(["safe-mode", "--run-id", "r", "--db", str(path), "--stamp-case", str(event_id)]) == 1
    )
    assert "requires --action" in capsys.readouterr().err

    base = ["safe-mode", "--run-id", "r", "--db", str(path), "--stamp-case", str(event_id)]
    assert main([*base, "--action", "first call"]) == 0
    capsys.readouterr()
    # The first disposition is the audit record: a second must not erase it.
    assert main([*base, "--action", "second call"]) == 1
    assert "already disposed of as 'first call'" in capsys.readouterr().err


def test_a_case_disposed_of_mid_command_is_reported_not_overwritten(store, capsys, monkeypatch):
    # THE race the atomic stamp exists for. This command takes no run lease, so
    # its "is this still open?" read and its write straddle a window in which the
    # live daemon's own reconciliation pass can stamp the MACHINE disposition —
    # what the system actually DID about the sighting. Simulated here by having
    # the daemon win exactly that window (a second connection writes right after
    # the CLI's guard read returns its snapshot): the operator's UPDATE must find
    # action_taken already set, say so, and exit 1 — never erase the record with
    # no error and no audit row.
    path, db = store
    event_id = _open_case(db)
    db.close()

    real_iter = repo.iter_exchange_reconciliation_events
    fired = {"n": 0}

    def _daemon_wins_the_window(conn, run_id, *, case_type=None):
        rows = real_iter(conn, run_id, case_type=case_type)
        if fired["n"] == 0:  # only the CLI's guard read, not any later lookup
            fired["n"] = 1
            with Database(path) as daemon, daemon.transaction() as other:
                repo.set_reconciliation_action(other, event_id, "resolved_fill_booked")
        return rows

    monkeypatch.setattr(repo, "iter_exchange_reconciliation_events", _daemon_wins_the_window)
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            "human: reviewed the payload",
        ]
    )
    monkeypatch.undo()
    assert rc == 1
    assert "disposed of by another writer" in capsys.readouterr().err
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        # The daemon's disposition survived intact — not replaced by the human's.
        assert row["action_taken"] == "resolved_fill_booked"
    assert fired["n"] == 1  # the interleaving really happened (guard against a no-op test)


def test_an_unmapped_sighting_is_refused_and_labelled_not_silently_stamped(store, capsys):
    # fill_unmapped resolves by an ANTI-JOIN against fills that never reads
    # action_taken — stamping one clears no verdict and would hide the row from
    # the only listing that enumerates the backlog. That is the exact wedge this
    # command exists to prevent, so it must be refused by name, and the listing
    # must not invite it.
    path, db = store
    event_id = _open_case(db, case_type="fill_unmapped", exchange_value="tid|0xdead-77")

    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--status"]) == 0
    out = capsys.readouterr().out
    assert f"[{event_id}]" in out  # still discoverable...
    assert "not by stamping" in out  # ...but labelled with its real resolution path

    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            "looked at it",
        ]
    )
    assert rc == 1
    assert "resolves by BOOKING that fill" in capsys.readouterr().err
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        assert row["action_taken"] is None  # untouched: still visible as backlog


def test_stamping_cannot_reach_another_runs_case(store, capsys):
    # repo.set_reconciliation_action has NO run_id filter — the CLI's own
    # run-scoped lookup is the only thing preventing a typo'd --run-id from
    # stamping a different run's audit row.
    path, db = store
    other_event_id = _open_case(db)
    accounting.initialize_run(
        db,
        run_id="other",
        mode="live",
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW - timedelta(days=1),
    )
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "other",
            "--db",
            str(path),
            "--stamp-case",
            str(other_event_id),
            "--action",
            "x",
        ]
    )
    assert rc == 1
    assert f"no reconciliation case with event_id {other_event_id}" in capsys.readouterr().err
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == other_event_id
        ]
        assert row["action_taken"] is None  # run "r"'s case survived untouched


def test_status_lists_the_reopened_occurrence_of_a_disposed_of_key(store, capsys):
    # The operator half of issue #65. This listing is their ONLY enumeration of
    # the backlog, and it keys on action_taken — so while a provisionally
    # disposed-of key swallowed its next occurrence, the worse fault that
    # followed a resend appeared here not at all, under a row still reading
    # "settled". The settled row must stay off the list (it IS disposed of) and
    # the occurrence after it must be on it, with the id --stamp-case needs.
    path, db = store
    cloid = "0x" + "ab" * 16
    for action, detail in (
        ("settled_never_sent", "the send never landed"),
        (None, "rule 10: the exchange took the cloid and denies it"),
    ):
        with db.transaction() as conn:
            assert repo.insert_exchange_reconciliation_event(
                conn,
                run_id="r",
                trigger="pre_cycle",
                case_type="order_missing_on_exchange",
                exchange_value=cloid,
                action_taken=action,
                detail=detail,
                timestamp=_NOW,
            )
    open_id = repo.iter_exchange_reconciliation_events(db.conn, "r")[-1]["event_id"]
    db.close()

    assert main(["safe-mode", "--run-id", "r", "--db", str(path), "--status"]) == 0
    out = capsys.readouterr().out
    assert "open reconciliation cases (1;" in out
    assert f"[{open_id}]" in out


@pytest.mark.parametrize("word", ["settled_never_sent", "settled_canceled", "local_row_reopened"])
def test_stamping_with_one_of_the_sweeps_own_dispositions_is_refused(store, capsys, word):
    # The once-per-fact guard reads the STRING, not who wrote it: these words
    # mean "an episode that can recur", so they RE-OPEN the key. An operator
    # reaching for the vocabulary the RUNBOOK prints at them would get a stamp
    # the CLI called successful and the next pass answered with a fresh
    # unresolved row — every pass, for a fault that stays in the sweep's cursor.
    path, db = store
    event_id = _open_case(db, case_type="order_missing_on_exchange", exchange_value="0xabab")
    db.close()
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            word,
        ]
    )
    assert rc == 1
    assert "PROVISIONAL" in capsys.readouterr().err
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        assert row["action_taken"] is None  # refused BEFORE the write, not after


@pytest.mark.parametrize("word", ["local_row_backfilled", "resolved_fill_booked", "backfilled"])
def test_stamping_with_a_final_machine_disposition_is_refused_too(store, capsys, word):
    # Issue #84: the fence now covers the sweep's WHOLE vocabulary, not just
    # the provisional half. These three shut the key either way, so the dedupe
    # is not the argument — the audit row is: a human stamp spelled exactly
    # like the daemon's leaves a later reader unable to tell an operator's
    # attestation from an automatic disposal. The message must say THAT rather
    # than repeating the provisional reason at a word it does not apply to.
    path, db = store
    event_id = _open_case(db, case_type="order_missing_on_exchange", exchange_value="0xabab")
    db.close()
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            word,
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "PROVISIONAL" not in err
    assert "the sweep writes automatically" in err
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        assert row["action_taken"] is None  # refused BEFORE the write, not after


def test_stamping_with_the_operators_own_words_is_still_accepted(store):
    # Negative control for both fences: widening the refused set must not start
    # rejecting prose, which is the only thing --stamp-case exists to record.
    path, db = store
    event_id = _open_case(db, case_type="order_missing_on_exchange", exchange_value="0xabab")
    db.close()
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            str(event_id),
            "--action",
            "cancelled by hand at the venue after calling support",
        ]
    )
    assert rc == 0
    with Database(path) as db2:
        (row,) = [
            r
            for r in repo.iter_exchange_reconciliation_events(db2.conn, "r")
            if r["event_id"] == event_id
        ]
        assert row["action_taken"] == "cancelled by hand at the venue after calling support"


def test_stamping_an_unknown_case_id_is_named_not_silent(store, capsys):
    path, _ = store
    rc = main(
        [
            "safe-mode",
            "--run-id",
            "r",
            "--db",
            str(path),
            "--stamp-case",
            "999",
            "--action",
            "x",
        ]
    )
    assert rc == 1
    assert "no reconciliation case with event_id 999" in capsys.readouterr().err
