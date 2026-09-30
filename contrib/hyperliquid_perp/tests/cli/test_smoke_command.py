"""Tests for the ``live-smoke`` subcommand with the suite's session stubbed or dry."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from contrib.hyperliquid_perp.cli import main as cli_main, smoke as cli_smoke_mod
from contrib.hyperliquid_perp.persistence.db import Database

from .conftest import live_yaml, make_live_run, seed_live_run_with_genesis_subset


def test_live_smoke_gate_status_reports_not_passed(tmp_path, capsys):
    dbp = make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    out = capsys.readouterr().out
    assert "smoke_gate_passed: no" in out
    assert rc == 4


def test_live_smoke_gate_status_nonexistent_run_exits_1(tmp_path, capsys):
    dbp = make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "nope", "--db", str(dbp), "--gate-status"])
    assert rc == 1


def test_live_smoke_bad_only_key_exits_1(tmp_path, capsys):
    dbp = make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--only", "bogus"])
    assert rc == 1
    assert "unknown smoke test key" in capsys.readouterr().err


def test_live_smoke_dry_run_records_skipped(tmp_path, capsys):
    # Dry-run needs a valid live config + the run to exist; it touches no network.
    # Genesis matches the config (the drift identity check runs before the
    # dry-run fork, 2026-07-28).
    cfg = live_yaml(tmp_path)
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="live-BTC")
    rc = cli_main(
        ["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp), "--dry-run"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run complete" in out
    assert "smoke_gate_passed: no" in out  # dry-run rows never satisfy the gate


def test_live_smoke_refuses_mainnet_mode(tmp_path, capsys):
    # The §20.2 smoke suite is a TESTNET pre-flight; a mainnet_tiny config must be
    # refused before any real order can reach mainnet (mainnet relies on the
    # separately-run testnet smoke, §21.3).
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    dbp = make_live_run(tmp_path, mode="mainnet_tiny")
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "only against a testnet_live run" in err
    assert "mainnet_tiny" in err


def test_live_smoke_missing_risk_block_exits_1(tmp_path, capsys):
    cfg = live_yaml(tmp_path, risk_lines=None)
    dbp = make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp)])
    assert rc == 1
    assert "no risk: block" in capsys.readouterr().err


def test_every_gate_stage_has_its_live_smoke_wording():
    from contrib.hyperliquid_perp.cli.smoke import _gate_refusal_wording
    from contrib.hyperliquid_perp.live.config import (
        ExecutionMode,
        LiveGateRefusal,
        LiveGateStage,
    )

    for stage in LiveGateStage:
        assert _gate_refusal_wording(LiveGateRefusal(stage, mode=ExecutionMode.MAINNET_TINY))


def test_live_smoke_refuses_mainnet_even_with_dry_run(tmp_path, capsys):
    # The guard sits before the dry-run branch: a mainnet config is refused even
    # for the offline wiring check, so a green dry-run can never lull an operator.
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    dbp = make_live_run(tmp_path, mode="mainnet_tiny")
    rc = cli_main(
        ["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp), "--dry-run"]
    )
    assert rc == 1
    assert "only against a testnet_live run" in capsys.readouterr().err


def test_live_smoke_refuses_run_created_for_another_network(tmp_path, capsys):
    # Q1 2026-07-28: run-identity discipline. A valid testnet config with a
    # typo'd --run-id pointing at the mainnet acceptance run (same default db)
    # must be refused BEFORE the pre-flight recovery can reconcile the testnet
    # exchange against that ledger and file integrity cases the §5 cumulative
    # policy makes permanent. The genesis live.network mismatch is the trip.
    cfg = live_yaml(tmp_path)  # testnet_live / testnet
    dbp = make_live_run(tmp_path, mode="mainnet_tiny")  # genesis network=mainnet
    rc = cli_main(
        ["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp), "--dry-run"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "live.network" in err
    assert "new --run-id" in err


def test_live_smoke_refuses_run_created_for_another_coin(tmp_path, capsys):
    cfg = live_yaml(tmp_path)  # allowed_symbols → BTC
    dbp = make_live_run(tmp_path, coin="ETH")
    rc = cli_main(
        ["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp), "--dry-run"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "created for coin 'ETH'" in err
    assert "'BTC'" in err


def test_live_smoke_gate_status_refuses_non_testnet_run(tmp_path, capsys):
    # Q2 2026-07-28: a mainnet_tiny run's live_smoke_tests is empty BY DESIGN
    # (§21.3) — raw buckets would print "not_yet_run: <all 18>" + exit 4 and
    # read as "go smoke-test mainnet", contradicting validate's "n/a (§21.3)".
    dbp = make_live_run(tmp_path, mode="mainnet_tiny")
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "§21.3" in captured.err
    assert "smoke_gate_passed" not in captured.out


def test_live_smoke_gate_status_refuses_unknown_genesis_mode(tmp_path, capsys):
    # A hand-built run whose genesis names no live.mode reads as "unknown" —
    # fail-safe refusal, same as any non-testnet mode.
    dbp = make_live_run(tmp_path, config_json="{}")
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    assert rc == 1
    assert "'unknown'" in capsys.readouterr().err


def test_live_smoke_gate_status_refuses_only_and_dry_run(tmp_path, capsys):
    # --gate-status is a pure store read: combining it with an action flag is
    # ambiguous operator intent (read the gate, or run/skip tests?) — refused
    # by name before anything touches the store.
    dbp = make_live_run(tmp_path)
    rc = cli_main(
        [
            "live-smoke",
            "--run-id",
            "live-BTC",
            "--db",
            str(dbp),
            "--gate-status",
            "--only",
            "signed_client_init",
        ]
    )
    assert rc == 1
    assert "drop --dry-run/--only" in capsys.readouterr().err

    rc2 = cli_main(
        ["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status", "--dry-run"]
    )
    assert rc2 == 1
    assert "drop --dry-run/--only" in capsys.readouterr().err


def test_live_smoke_only_status_without_submit_exits_1(tmp_path, capsys):
    # Q3 2026-07-28: test 4 queries the order test 3 places in the same
    # process, so this selection can never pass — refuse it at the entrance
    # instead of writing a real FAILED row that validate would present as
    # "exchange refused".
    dbp = make_live_run(tmp_path)
    rc = cli_main(
        ["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--only", "slice_order_status"]
    )
    assert rc == 1
    assert "select both" in capsys.readouterr().err


def test_live_smoke_real_run_requires_the_run_lease(tmp_path, capsys, monkeypatch):
    # live-smoke places real orders and runs recoveries — the same actions the
    # run lease keeps single-owner. A held lease must refuse the suite.
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    dbp = make_live_run(tmp_path)
    db = Database(dbp)
    acquire_run_lock(db, "live-BTC", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    assert rc == 1
    assert "places real orders" in capsys.readouterr().err


def test_live_smoke_preflight_failure_exits_4_and_releases_the_lease(tmp_path, capsys, monkeypatch):
    # A pre-flight recovery failure aborts the suite: exit 4, the error named on
    # stderr, and the lease released so the operator can immediately retry.
    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )

    def _fail_preflight(self, *, only=None):
        raise smoke_mod.SmokePreflightError("pre-flight §19.1 recovery did not pass — offline test")

    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", _fail_preflight)
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert "pre-flight" in captured.err
    # The lease must be free again: a fresh acquire under a different pid works.
    db = Database(dbp)
    acquire_run_lock(db, "live-BTC", pid=424242, now=datetime.now(timezone.utc))
    db.close()


def test_live_smoke_superseded_lease_exits_1_by_name(tmp_path, capsys, monkeypatch):
    # A mid-suite lease takeover surfaces as the named lock outcome (exit 1),
    # not main()'s generic exit 2.
    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.runtime.run_lock import RunLockError

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )

    def _superseded(self, *, only=None):
        raise RunLockError("run 'live-BTC' lease superseded by pid 4242")

    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", _superseded)
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "superseded mid-suite" in err


def test_live_smoke_disarm_warning_survives_an_unexpected_crash(tmp_path, capsys, monkeypatch):
    # The disarm-failed WARNING prints from a finally (silent-failure review,
    # 2026-07-29): a mid-suite exception escaping to main()'s generic handler
    # is exactly when a failed disarm is most likely, and the warning must not
    # be lost under that stack trace.
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )

    def _crash(self, *, only=None):
        self.kill_switch_disarm_failed = True
        raise RuntimeError("wire gone mid-suite")

    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", _crash)
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    captured = capsys.readouterr()
    assert rc == 2  # main()'s generic unexpected-error exit
    assert "kill-switch disarm FAILED" in captured.err


def test_live_smoke_staged_long_residual_warns_on_stderr(tmp_path, capsys, monkeypatch):
    # The trigger block's staged long is closed BETWEEN tests, so a close that
    # never flattened has no step row to land in: without this warning a real
    # funded position is left on the wire with only a log line — which the
    # operator may not be capturing — to show for it (review round 2026-07-29).
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )
    note = "cleanup: reduce-only close of 0.001 refused (no liquidity)"

    def _leaves_a_residual(self, *, only=None):
        self.staged_long_residual = note
        return []

    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", _leaves_a_residual)
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    err = capsys.readouterr().err
    assert rc == 4  # no verdicts recorded → the gate stays shut, as before
    assert "trigger-block staging position may still be OPEN" in err
    assert note in err  # the runner's own note, verbatim — the operator acts on it


def test_live_smoke_flat_staged_long_prints_no_residual_warning(tmp_path, capsys, monkeypatch):
    # The mirror: a suite that flattened its staged long must not cry wolf. An
    # unconditional warning trains the operator to ignore the one run where a
    # real position IS still open.
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )
    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", lambda self, *, only=None: [])
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    err = capsys.readouterr().err
    assert rc == 4
    assert "staging position may still be OPEN" not in err


def test_the_residual_warnings_quote_the_real_suite_size(tmp_path, capsys, monkeypatch):
    """Both "re-run under a NEW run-id" warnings must name the CURRENT suite size.

    The number is the cost the operator is being quoted: how many tests the
    fresh run-id starts out owing. Both sentences carried a hand-copied 18
    through the two tests PR B2 added, so the warning under-quoted the work by
    two while the RUNBOOK's copy of the same sentence -- which IS pinned, in
    test_smoke.py -- stayed right. Pin the code copies the same way: not a
    count of how many warnings say it, but "no copy says a different number".
    """
    import re

    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.live.smoke import SMOKE_TEST_KEYS

    dbp = make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_smoke_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )

    def _leaves_both_residuals(self, *, only=None):
        # The two warnings are reached by different residuals, so one run has
        # to strand both for this to see both sentences.
        self.staged_long_residual = "cleanup: reduce-only close of 0.001 refused"
        self.position_residuals = ["0.0005 left after cleanup"]
        return []

    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", _leaves_both_residuals)
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    err = capsys.readouterr().err
    assert rc == 4
    quoted = re.findall(r"all (\d+) (?:tests )?not_yet_run", err)
    # Both sentences fired, and neither of them is stale.
    assert len(quoted) == 2, err
    assert set(quoted) == {str(len(SMOKE_TEST_KEYS))}, quoted
