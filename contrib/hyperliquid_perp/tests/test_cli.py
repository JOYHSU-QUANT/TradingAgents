"""Tests for the subcommand CLI's offline paths (export / validate / dispatch).

The long-running ``paper`` loop and its network/LLM seams are exercised only up
to their credential/store guards — everything beyond needs a live exchange.
"""

from __future__ import annotations

import csv
import json
import signal
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import (
    _classify_engine_error,
    _config_drift_report,
    _raise_keyboard_interrupt,
    _run_config_subset,
    main as cli_main,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from .conftest import insert_decision_attempts

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _seed_db(tmp_path):
    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return path, db


def test_validate_exit_codes(tmp_path, capsys):
    path, db = _seed_db(tmp_path)
    db.close()
    # Consistent but far short of 30 cycles -> 4 ("keep running").
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 4
    assert "phase3_ready: no" in capsys.readouterr().out

    # Integrity failure (orphan fill) -> 5 ("store is broken"), not 4.
    db = Database(path)
    accounting.post_fill(
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


def test_validate_operator_errors_exit_1(tmp_path, capsys):
    path, db = _seed_db(tmp_path)
    db.close()
    assert cli_main(["validate", "--run-id", "ghost", "--db", str(path)]) == 1
    assert cli_main(["validate", "--run-id", "r", "--db", str(tmp_path / "nope.db")]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_export_subcommand_writes_eight_csvs(tmp_path, capsys):
    path, db = _seed_db(tmp_path)
    db.close()
    out = tmp_path / "exp"
    assert cli_main(["export", "--run-id", "r", "--output-dir", str(out), "--db", str(path)]) == 0
    files = sorted(p.name for p in out.glob("*.csv"))
    assert len(files) == 8
    with (out / "account_snapshots.csv").open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header[0] == "timestamp"


def test_export_unknown_run_exits_1(tmp_path, capsys):
    path, db = _seed_db(tmp_path)
    db.close()
    rc = cli_main(
        ["export", "--run-id", "ghost", "--output-dir", str(tmp_path / "e"), "--db", str(path)]
    )
    assert rc == 1
    assert "export_failed" in capsys.readouterr().err


def test_paper_refuses_missing_db_without_create(tmp_path, capsys):
    # Checked before any key/network work: a typo'd --db must not fork history.
    rc = cli_main(["paper", "--coin", "BTC", "--db", str(tmp_path / "missing.db")])
    assert rc == 1
    assert "--create" in capsys.readouterr().err


def test_validate_exit_0_when_phase3_ready(tmp_path, capsys):
    # A consistent store with >= 30 completed cycles is the exit-0 path.
    path, db = _seed_db(tmp_path)
    insert_decision_attempts(db, ["completed"] * 30, start=_T0)
    db.close()
    assert cli_main(["validate", "--run-id", "r", "--db", str(path)]) == 0
    assert "phase3_ready: yes" in capsys.readouterr().out


@pytest.fixture
def paper_seams(tmp_path, monkeypatch):
    """Mock the exchange seam so ``paper``'s pre-lease guards run keylessly.

    The create/resume identity checks fire only after the exchange metadata
    fetch, so the client factory and ``get_asset_meta`` are patched at the
    module seam (no network); everything the guards need stays real.
    """
    from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
    from contrib.hyperliquid_perp.exchanges.hyperliquid import (
        market_data as md_mod,
        sdk_client as sdk_mod,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        sdk_mod.HyperliquidClient,
        "from_config",
        lambda config: SimpleNamespace(info=None),
    )
    schedule = MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),))
    monkeypatch.setattr(
        md_mod.HyperliquidMarketData,
        "get_asset_meta",
        lambda self, coin: (3, schedule),
    )
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("", encoding="utf-8")
    return cfg


def _paper_argv(db_path, *, run_id, config, create=False):
    argv = ["paper", "--coin", "BTC", "--db", str(db_path), "--run-id", run_id]
    if create:
        argv.append("--create")
    return argv + ["--config", str(config)]


def test_paper_create_on_existing_run_exits_1(tmp_path, capsys, paper_seams):
    # --create makes store identity explicit in BOTH directions: pointing it at
    # an already-existing run must refuse, not silently resume/contaminate.
    path, db = _seed_db(tmp_path)
    db.close()
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams, create=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "Drop --create" in err


def test_paper_unknown_run_without_create_exits_1(tmp_path, capsys, paper_seams):
    # An existing store but an unknown run_id must not silently start a run.
    path, db = _seed_db(tmp_path)
    db.close()
    rc = cli_main(_paper_argv(path, run_id="ghost", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "--create" in err


def test_paper_missing_api_key_exits_1(tmp_path, capsys, monkeypatch):
    # Returns before any network/config work — no seams needed.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = cli_main(["paper", "--coin", "BTC", "--db", str(tmp_path / "new.db"), "--create"])
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_paper_invalid_config_exits_1(tmp_path, capsys, monkeypatch):
    # Config validation also precedes any exchange work.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    bad = tmp_path / "bad.yaml"
    bad.write_text("bogus_top_level_key: 1\n", encoding="utf-8")
    rc = cli_main(
        [
            "paper",
            "--coin",
            "BTC",
            "--db",
            str(tmp_path / "new.db"),
            "--create",
            "--config",
            str(bad),
        ]
    )
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("request timed out after 60s"), "timeout"),
        (RuntimeError("HTTP 429 Too Many Requests"), "rate_limit"),
        (RuntimeError("Connection refused by host"), "connection"),
        (RuntimeError("something exploded"), "server_error"),
        # Order pinned: when a message matches both vocabularies, timeout wins.
        (RuntimeError("connection timeout while reading"), "timeout"),
    ],
)
def test_classify_engine_error(exc, expected):
    assert _classify_engine_error(exc) == expected


def _subset_json(config: dict, coin: str) -> str:
    """Exactly what run creation persists as config_json."""
    return json.dumps(_run_config_subset(config, coin), ensure_ascii=False, default=str)


def test_config_drift_no_stored_record_returns_none():
    # A pre-drift-check store has no genesis record: nothing to compare.
    assert _config_drift_report(None, {"risk": {"leverage": 5}}, "BTC") is None


def test_config_drift_coin_mismatch_is_hard_error():
    stored = _subset_json({}, "ETH")
    kind, msg = _config_drift_report(stored, {}, "BTC")
    assert kind == "coin"
    assert "'ETH'" in msg and "'BTC'" in msg


def test_config_drift_params_lists_drifted_keys_sorted():
    stored = _subset_json(
        {"risk": {"leverage": 5}, "decision": {"min_confidence": 0.5}, "paper_trading": None},
        "BTC",
    )
    current = {
        "risk": {"leverage": 3},
        "decision": {"min_confidence": 0.9},
        "paper_trading": None,
    }
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "decision, risk" in msg  # sorted; unchanged paper_trading absent
    assert "paper_trading" not in msg


def test_config_drift_identical_config_with_decimal_returns_none():
    # Decimals stringify via default=str at creation; the comparison round-trips
    # today's config the same way, so an unchanged Decimal is not false drift.
    config = {
        "risk": {"leverage": Decimal("5")},
        "decision": None,
        "paper_trading": {"fee_rate": Decimal("0.00045")},
    }
    stored = _subset_json(config, "BTC")
    assert _config_drift_report(stored, config, "BTC") is None


def test_post_cycle_export_persists_status_breadcrumbs(tmp_path, monkeypatch):
    """Export outcomes land durably on scheduler_state (ok and failed lanes)."""
    from contrib.hyperliquid_perp.cli import _post_cycle_export
    from contrib.hyperliquid_perp.persistence import export as export_mod

    path, db = _seed_db(tmp_path)
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
    from contrib.hyperliquid_perp.paper import accounting as acc_mod
    from contrib.hyperliquid_perp.persistence.models import AccountLedger

    path, db = _seed_db(tmp_path)
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


def test_history_funding_source_escalates_after_consecutive_failures(caplog):
    """A chronic funding-history break logs ERROR, not an eternal WARNING."""
    import logging

    from contrib.hyperliquid_perp.cli import _HistoryFundingSource

    class _BrokenMarket:
        def get_funding_history(self, coin, days):
            raise RuntimeError("endpoint gone")

    source = _HistoryFundingSource(_BrokenMarket())
    threshold = source._FAILURE_ESCALATION_THRESHOLD
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.cli"):
        for _ in range(threshold):
            assert source.rate_at("BTC", when) is None
    levels = [r.levelno for r in caplog.records if "funding history fetch failed" in r.getMessage()]
    assert len(levels) == threshold
    assert all(lv == logging.WARNING for lv in levels[:-1])
    assert levels[-1] == logging.ERROR


def test_sigterm_shim_raises_keyboard_interrupt():
    # systemd/docker stop with SIGTERM; the handler must funnel it into the
    # KeyboardInterrupt shutdown-export path.
    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(signal.SIGTERM, None)


# --------------------------------------------------------------------------
# dispatch: legacy delegation vs unknown-subcommand typos
# --------------------------------------------------------------------------


def test_unknown_bare_word_is_an_error_not_legacy_usage(capsys):
    # A subcommand typo must name the real subcommands under exit 1 — not fall
    # through to the legacy parser's usage (which never mentions them) under
    # exit 2 ("unexpected error").
    assert cli_main(["expot"]) == 1
    err = capsys.readouterr().err
    assert "unknown subcommand 'expot'" in err
    assert "paper" in err and "export" in err and "validate" in err


def test_empty_and_flag_style_argv_delegate_to_legacy(monkeypatch):
    # The Phase 1/2 compatibility promise: empty argv and flag invocations
    # (legacy accepts no positionals) flow to .main verbatim.
    from contrib.hyperliquid_perp import main as legacy_mod

    seen = []

    def fake_legacy(argv):
        seen.append(argv)
        return 7

    monkeypatch.setattr(legacy_mod, "main", fake_legacy)
    assert cli_main([]) == 7
    assert cli_main(["--context-only", "--coin", "BTC"]) == 7
    assert seen == [[], ["--context-only", "--coin", "BTC"]]


def test_cli_main_wrapper_maps_interrupt_and_unexpected_error(monkeypatch, capsys):
    # The documented top-level contract for the new subcommands: Ctrl-C -> 130,
    # anything unexpected -> 2 (distinct from named operator errors -> 1).
    import contrib.hyperliquid_perp.cli as cli_mod

    def interrupt(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_cmd_validate", interrupt)
    assert cli_main(["validate", "--run-id", "r"]) == 130
    assert "interrupted" in capsys.readouterr().err

    def boom(argv):
        raise RuntimeError("wires crossed")

    monkeypatch.setattr(cli_mod, "_cmd_export", boom)
    assert cli_main(["export", "whatever"]) == 2
    err = capsys.readouterr().err
    assert "unexpected error" in err and "wires crossed" in err


# --------------------------------------------------------------------------
# config-drift breadcrumb (schema v5): resume outcomes land durably
# --------------------------------------------------------------------------


def test_config_drift_breadcrumb_roundtrip_and_vocabulary(tmp_path):
    from contrib.hyperliquid_perp.cli import _stamp_breadcrumb

    path, db = _seed_db(tmp_path)
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


def test_paper_resume_stamps_drift_breadcrumb(tmp_path, monkeypatch, capsys, paper_seams):
    # Drive the real resume path (lease -> drift check -> reconcile) and stop
    # at the loop seam: parameter drift must survive in the store, not only on
    # a possibly-uncaptured stderr stream.
    import contrib.hyperliquid_perp.cli as cli_mod

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        config_json=json.dumps(
            {"risk": {"leverage": 999}, "decision": None, "paper_trading": None, "coin": "BTC"}
        ),
    )
    db.close()

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_paper_loop", stop_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert "config drift on resume" in capsys.readouterr().err

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "drift"
    assert "config drift on resume" in state["last_config_drift_error"]
    assert state["last_config_drift_at"] is not None
    db.close()


def test_paper_resume_clean_stamps_ok_breadcrumb(tmp_path, monkeypatch, paper_seams):
    # A clean resume overwrites any earlier "drift" verdict: a reverted config
    # must not leave a stale drift as the store's last word.
    import contrib.hyperliquid_perp.cli as cli_mod

    path, db = _seed_db(tmp_path)  # no genesis config record -> nothing drifts
    db.close()

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_paper_loop", stop_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "ok"
    assert state["last_config_drift_error"] is None
    db.close()


# --------------------------------------------------------------------------
# _paper_loop wiring: tick-before-poll, per-iteration heartbeat, halt latch
# --------------------------------------------------------------------------


def test_paper_loop_wiring_and_halt_latch(tmp_path, monkeypatch):
    """Drive the production loop for two iterations with recording stubs.

    Pins the wiring that only exists in ``_paper_loop`` itself: the heartbeat
    fires every iteration, tick precedes poll, a cycle-terminal poll triggers
    the funding backfill and the replay-verify export, and a failed
    verification latches ``trading_halted`` (no further ``poll()`` calls).
    """
    from datetime import timedelta

    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult

    path, db = _seed_db(tmp_path)
    clock = ManualClock(_T0)
    calls: list[str] = []

    monkeypatch.setattr(
        run_lock_mod,
        "heartbeat_run_lock",
        lambda db_, run_id, *, pid, now: calls.append("heartbeat"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: calls.append("backfill"),
    )
    monkeypatch.setattr(
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), False)[1],
    )

    terminal = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#001",
        scheduled_at=_T0,
        attempt_count=3,
        next_decision_at=_T0 + timedelta(hours=4),
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            calls.append("tick")

    class _Scheduler:
        def poll(self):
            calls.append("poll")
            return terminal

        def next_due_at(self):  # pragma: no cover — halted branch skips it
            raise AssertionError("next_due_at must not be consulted while halted")

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli_mod._paper_loop(
            db,
            "r",
            _Engine(),
            _Scheduler(),
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )

    # Iteration 1: heartbeat -> tick -> poll -> cycle-terminal work; the failed
    # verification latches the halt, so iteration 2 ticks but never polls.
    assert calls == ["heartbeat", "tick", "poll", "backfill", "export", "heartbeat", "tick"]
    # Sleep stays inside the lease-freshness cap.
    assert sleeps and all(s <= 60.0 for s in sleeps)
    db.close()
