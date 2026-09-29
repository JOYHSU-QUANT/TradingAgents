"""Tests for the ``paper`` subcommand up to its loop."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main
from contrib.hyperliquid_perp.common import store_layout
from contrib.hyperliquid_perp.integration import decision_provider as decision_provider_mod
from contrib.hyperliquid_perp.paper import accounting as paper_accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database, connect
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting

from ..conftest import build_store_at, insert_decision_attempts
from .conftest import (
    BAD_ENGINE_ENV_KNOBS,
    BEHIND_VERSIONS,
    StopBeforeTheLoop,
    assert_position_source_binds,
    build_behind,
    build_one_behind,
    paper_argv,
    seed_db,
    stored_version,
)

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_paper_refuses_missing_db_without_create(tmp_path, capsys):
    # Checked before any key/network work: a typo'd --db must not fork history.
    rc = cli_main(["paper", "--coin", "BTC", "--db", str(tmp_path / "missing.db")])
    assert rc == 1
    assert "--create" in capsys.readouterr().err


def test_paper_create_on_existing_run_exits_1(tmp_path, capsys, paper_seams):
    # --create makes store identity explicit in BOTH directions: pointing it at
    # an already-existing run must refuse, not silently resume/contaminate.
    path, db = seed_db(tmp_path)
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams, create=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "Drop --create" in err


def test_paper_unknown_run_without_create_exits_1(tmp_path, capsys, paper_seams):
    # An existing store but an unknown run_id must not silently start a run.
    path, db = seed_db(tmp_path)
    db.close()
    rc = cli_main(paper_argv(path, run_id="ghost", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "--create" in err


def test_paper_fresh_run_missing_api_key_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # A fresh run always drives the AI, so a missing key still refuses — but the
    # check now fires in the fresh-run branch, BEFORE the run row is written, so a
    # retry with the key still sees a clean --create (no half-created run).
    import contrib.hyperliquid_perp.cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Sentinel pins the dotenv_diagnosis wiring: the abort message must embed
    # the diagnosis for the actual variable — dropping the interpolation (or
    # diagnosing the wrong var) is invisible to the substring check alone.
    # (The message is printed by _common._require_api_key — patch its module.)
    monkeypatch.setattr(cli_mod._common, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    path = tmp_path / "new.db"
    rc = cli_main(paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
    assert "DIAG[OPENROUTER_API_KEY]" in err
    db = Database(path)
    assert repo.get_run(db.conn, "fresh") is None  # not created before the key check
    db.close()


def test_paper_key_check_satisfied_by_dotenv(tmp_path, monkeypatch, paper_seams):
    # Companion of test_main's ordering test for the paper path: a key kept only
    # in the repo-root .env must satisfy _require_api_key, which fires before
    # the run row is written. The suite-wide autouse fixture stubs the loader
    # out, so this test re-binds the real one.
    from contrib.hyperliquid_perp import cli as cli_mod, config as config_mod

    monkeypatch.setattr(cli_mod, "load_dotenv_files", config_mod.load_dotenv_files)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    reached = []

    def _stop(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("stop right after the key check")

    # cli reaches initialize_run through runtime.genesis, which reads it off
    # `accounting` at call time; patch the module itself.
    monkeypatch.setattr(accounting, "initialize_run", _stop)
    # The provider pre-flight sits between the key check and initialize_run;
    # stub it so this test stays off the real tradingagents import.
    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", lambda *a, **kw: object())
    rc = cli_main(paper_argv(tmp_path / "new.db", run_id="fresh", config=paper_seams, create=True))

    # Reaching initialize_run proves the key check passed on the .env value;
    # without the load, the run would have exited 1 on the missing-key path.
    assert reached == [True]
    assert rc == 2  # the top-level wrapper maps the sentinel as unexpected
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-dotenv"


def test_paper_fresh_run_off_coin_seed_exits_1(tmp_path, capsys, paper_seams):
    # The engine manages exactly the run coin: an off-coin seed would sit in
    # the store all run, excluded from equity/SL-TP/funding — so a fresh run
    # refuses it as a config error, before the run row is written.
    paper_seams.write_text(
        "paper_trading:\n"
        "  account:\n"
        "    initial_positions:\n"
        "      - {coin: BTC, size: '0.01', entry_price: '50000'}\n"
        "      - {coin: ETH, size: '0.1', entry_price: '3000'}\n",
        encoding="utf-8",
    )
    path = tmp_path / "new.db"
    rc = cli_main(paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "'ETH'" in err and "'BTC'" in err
    assert "initial_positions" in err
    db = Database(path)
    assert repo.get_run(db.conn, "fresh") is None  # rejected before genesis
    db.close()


def test_paper_resume_with_off_coin_position_exits_1(tmp_path, capsys, paper_seams):
    # Resume-side counterpart of the fresh-run seed guard: a store created via
    # direct initialize_run may legally hold off-coin positions (multi-coin
    # genesis is an API-level feature), but the single-coin daemon refuses to
    # resume it — before reconcile writes anything — rather than run with
    # equity/SL-TP silently excluding that position. replay alone would pass
    # (seeds sit symmetrically on both sides), so this needs its own check.
    path = tmp_path / "multi.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="ETH", size=D("0.1"), entry_price=D(3000))],
    )
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "'ETH'" in err and "'BTC'" in err
    assert "protection" in err


def test_paper_resume_refuses_a_live_mode_run(tmp_path, capsys, paper_seams):
    # Run-identity discipline (decided 2026-07-17): a live run's genesis
    # carries the same coin, so the drift check alone would wave a typo'd
    # --run-id/--db through — and the paper daemon would then trade over a
    # LIVE run's books. Named refusal before the run lock touches the row.
    path = tmp_path / "live_store.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=D(1000),
        schema_version=1,
    )
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "is a live run" in err


def test_paper_resume_ignores_flat_off_coin_position(tmp_path, capsys, monkeypatch, paper_seams):
    # The resume guard blocks only OPEN off-coin positions: a closed one is
    # inert everywhere (zero equity contribution, nothing to protect), so it
    # must not strand the store. The keyless flat-restart refusal firing
    # proves startup got past the off-coin check.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "closed.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="ETH", size=D("0.1"), entry_price=D(3000))],
    )
    paper_accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|close|0",
        order_id="o-close",
        symbol="ETH",
        side="sell",
        qty=D("0.1"),
        price=D(3000),
        fee_rate=D(0),
        timestamp=_T0,
    )
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err  # the refusal came from the key check
    assert "manages only" not in err


def test_paper_healthy_restart_missing_api_key_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # A FLAT healthy restart will poll the AI and has nothing to protect, so a
    # missing key still aborts (checked after reconcile + engine construction —
    # a keyless restart holding live work falls back to protection-only instead;
    # see the companion test below).
    path, db = seed_db(tmp_path)
    db.close()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_paper_keyless_healthy_restart_with_live_work_enters_protection_only(
    tmp_path, capsys, monkeypatch, paper_seams
):
    """A keyless healthy restart over live work must NOT exit: reconcile already
    canceled the plans, so exiting would leave the position with nobody watching
    its SL/TP — the exact harm protection-only mode exists to prevent (a
    replay-mismatch restart, with *less* trustworthy books, already gets it).
    Same construction contract as the mismatch fork: scheduler/provider never
    built, and the settle-exit messaging carries the missing-key reason."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )

    def _forbid_provider(*args, **kwargs):
        raise AssertionError("keyless protection-only must not build the decision provider")

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _forbid_provider)
    # Sentinel pins the dotenv_diagnosis wiring in the protection-only message
    # (same contract as the fresh-run abort's sentinel above).
    monkeypatch.setattr(cli_mod.paper, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        seen["engine_active"] = engine.has_active_work()
        seen["trading_halted"] = kwargs["trading_halted"]
        seen["halt_reason"] = kwargs["halt_reason"]
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["scheduler"] is None
    assert seen["engine_active"] is True  # the seeded live position
    assert seen["trading_halted"] is True
    assert seen["halt_reason"] == "missing-key"
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is not set but this run holds a live position" in err
    assert "protection-only" in err
    assert "DIAG[OPENROUTER_API_KEY]" in err


def test_paper_provider_import_failure_exits_1_named(tmp_path, capsys, monkeypatch, paper_seams):
    # _EngineDecisionProvider construction runs _build_engine_config, whose
    # named RuntimeError must map to the documented exit 1 (see
    # _build_engine_config for the causes), not the exit-2 last-resort handler.
    # On a fresh run it fires pre-flight, BEFORE the run row is written — same
    # ordering rule as the key check — so fixing the cause (e.g. re-saving the
    # .env as UTF-8) lets the SAME --create succeed instead of bouncing off
    # "already exists".
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.engine_bridge import EngineImportError

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def _boom(*args, **kwargs):
        raise EngineImportError(
            "importing tradingagents failed, most likely while its package init "
            "read a repo .env file"
        )

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _boom)
    path = tmp_path / "new.db"
    rc = cli_main(paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    assert "error: importing tradingagents failed" in capsys.readouterr().err
    db = Database(path)
    assert repo.get_run(db.conn, "fresh") is None  # failed before genesis
    db.close()

    # The operator fixes the environment and retries the SAME command: the
    # provider now builds and --create must not hit "already exists".
    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", lambda *a, **kw: object())
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["run_id"] = run_id
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    rc = cli_main(paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 0
    assert seen["run_id"] == "fresh"
    assert "created paper run" in capsys.readouterr().err


def test_paper_restart_provider_import_failure_exits_1_named(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # Restart-lane counterpart of the fresh-run pre-flight above: a healthy
    # keyed restart builds the provider only after reconciliation settles that
    # it trades, and an EngineImportError there must still map to the named
    # exit 1, not the exit-2 last-resort handler. This is the FLAT case —
    # nothing to protect, so the abort stands; a restart holding live work
    # degrades to protection-only instead (companion test below).
    from contrib.hyperliquid_perp.engine_bridge import EngineImportError
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path, db = seed_db(tmp_path)
    db.close()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )

    def _boom(*args, **kwargs):
        raise EngineImportError(
            "importing tradingagents failed, most likely while its package init "
            "read a repo .env file"
        )

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _boom)
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "error: importing tradingagents failed" in capsys.readouterr().err


def test_paper_restart_import_failure_with_live_work_enters_protection_only(
    tmp_path, capsys, monkeypatch, paper_seams
):
    """Corrupt-.env twin of the keyless protection-only fork: a healthy keyed
    restart over live work whose provider build raises EngineImportError must
    degrade to protection-only instead of exiting — the fault is as
    operator-fixable as a missing key, and under supervised restart (RUNBOOK
    §3) exit 1 would loop forever with the position unwatched. Flat, the named
    exit 1 stands (companion test above). Same construction contract as the
    other halted forks: no scheduler, and the loop messaging carries the
    engine-config-error reason (shared with every other operator-fixable
    engine build failure, e.g. a rejected completion cap)."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.engine_bridge import EngineImportError
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )

    def _boom(*args, **kwargs):
        raise EngineImportError(
            "importing tradingagents failed, most likely while its package init "
            "read a repo .env file"
        )

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _boom)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        seen["engine_active"] = engine.has_active_work()
        seen["trading_halted"] = kwargs["trading_halted"]
        seen["halt_reason"] = kwargs["halt_reason"]
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["scheduler"] is None
    assert seen["engine_active"] is True  # the seeded live position
    assert seen["trading_halted"] is True
    assert seen["halt_reason"] == "engine-config-error"
    err = capsys.readouterr().err
    assert "importing tradingagents failed" in err  # the fixable cause is shown
    assert "protection-only" in err


@pytest.mark.parametrize("key,bad,env", BAD_ENGINE_ENV_KNOBS)
def test_paper_restart_bad_engine_env_knob_with_live_work_enters_protection_only(
    tmp_path, capsys, monkeypatch, paper_seams, key, bad, env
):
    """A rejected engine env knob must degrade like a failed import, not exit.

    The cap (issue #177), the retry budget (issue #266) and the temperature
    (issue #269) are validated at
    startup so a typo cannot stall every cycle unclassified. But raising
    over a live position must NOT kill
    the process: that would leave the position with nobody watching SL/TP —
    strictly worse than the stall it replaces, and under systemd Restart= a
    crash-loop with no protection at all. This pins the lane, not just the
    validator: it is what the raise's error TYPE buys.
    """
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation
    from tradingagents.default_config import DEFAULT_CONFIG

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )
    # The operator's typo, exactly as it would arrive from the host .env.
    monkeypatch.setitem(DEFAULT_CONFIG, key, bad)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        seen["engine_active"] = engine.has_active_work()
        seen["trading_halted"] = kwargs["trading_halted"]
        seen["halt_reason"] = kwargs["halt_reason"]
        seen["halt_cause"] = kwargs["halt_cause"]
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["scheduler"] is None
    assert seen["engine_active"] is True  # the seeded live position
    assert seen["trading_halted"] is True
    assert seen["halt_reason"] == "engine-config-error"
    assert env in seen["halt_cause"]  # the refusal text, for the settle-exit line
    err = capsys.readouterr().err
    assert env in err  # the fixable cause is named
    assert "protection-only" in err


def test_paper_restart_treats_an_unreadable_book_as_live_work(
    tmp_path, capsys, monkeypatch, paper_seams
):
    """The paper side of ``holds_live_work`` (#270 review): the "anything to
    guard?" read raising (a locked store, or the engine's own halted guard)
    on an engine-config-error restart enters protection-only, not a crash
    over a position nobody watches — the live loop's twin pin is
    test_the_live_loop_treats_an_unreadable_book_as_live_work. The keyless
    restart makes the same call, so it is driven too.
    """
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.engine import PaperExecutionEngine
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation
    from tradingagents.default_config import DEFAULT_CONFIG

    path, db = seed_db(tmp_path)  # a FLAT book: only the raise makes it "live"
    db.close()
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )

    def _locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(PaperExecutionEngine, "has_active_work", _locked)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["halt_reason"] = kwargs["halt_reason"]
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    # Engine-config-error restart.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setitem(DEFAULT_CONFIG, "temperature", "abc")
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["halt_reason"] == "engine-config-error"
    assert "protection-only" in capsys.readouterr().err
    # Keyless restart: the same decision, the same fail-toward-guarding.
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delitem(DEFAULT_CONFIG, "temperature")
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["halt_reason"] == "missing-key"
    assert "protection-only" in capsys.readouterr().err


def test_paper_protection_only_survives_a_broken_stranded_attempt_lookup(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # The stranded-attempt note's lookup is best-effort on paper too (#270
    # review): a store holding two in-progress rows makes it raise by design,
    # and before the shared helper that raise crashed the restart over the
    # live position it was about to guard.
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.cli import _common as common_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation
    from tradingagents.default_config import DEFAULT_CONFIG

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_max_retries", "abc")
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )

    def _wedged(conn, run_id):
        raise ValueError(f"run {run_id!r} has 2 in-progress attempts")

    monkeypatch.setattr(common_mod.repo, "find_in_progress_attempt", _wedged)
    monkeypatch.setattr(cli_mod.paper, "_paper_loop", lambda *a, **kw: 0)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    err = capsys.readouterr().err
    assert "protection-only" in err
    assert "note: decision attempt" not in err


@pytest.mark.parametrize("key,bad,env", BAD_ENGINE_ENV_KNOBS)
def test_paper_bad_engine_env_knob_with_no_live_work_is_a_named_exit_1(
    tmp_path, capsys, monkeypatch, paper_seams, key, bad, env
):
    """Flat, the same refusal is a named exit 1 in both lanes: nothing to guard.

    The fresh ``--create`` lane fails pre-flight, BEFORE the run row is
    written (the key check's ordering rule, so the retry after fixing the
    .env is not bounced as "already exists"); the healthy-restart lane with
    an empty book exits by name instead of entering protection-only. The
    test above pins the live-position half of the restart's
    ``EngineConfigError`` decision (``gate_restart``); this pins the other
    half, for every env knob in the family.
    """
    import contrib.hyperliquid_perp.cli as cli_mod
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setitem(DEFAULT_CONFIG, key, bad)
    monkeypatch.setattr(
        cli_mod.paper, "_paper_loop", lambda *a, **kw: pytest.fail("the loop must not start")
    )
    # Fresh --create: refused before genesis.
    fresh = tmp_path / "fresh.db"
    assert cli_main(paper_argv(fresh, run_id="fresh", config=paper_seams, create=True)) == 1
    err = capsys.readouterr().err
    assert env in err and "protection-only" not in err
    db = Database(fresh)
    assert repo.get_run(db.conn, "fresh") is None
    db.close()
    # Healthy restart, empty book: named exit 1, not protection-only.
    path, db = seed_db(tmp_path)
    db.close()
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 1
    err = capsys.readouterr().err
    assert env in err and "protection-only" not in err


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

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", stop_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert "config drift on resume" in capsys.readouterr().err

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "drift"
    assert "config drift on resume" in state["last_config_drift_error"]
    assert state["last_config_drift_at"] is not None
    db.close()


@pytest.mark.parametrize(
    ("window", "expected_status"),
    [
        # Off (the default): the #98 reproduction end to end — the only diff
        # against the genesis is the `: 0` line, and the store must say "ok".
        (0, "ok"),
        # On: the prompt grows a section, and the store must say so.
        (30, "drift"),
    ],
)
def test_paper_resume_stamps_the_breadcrumb_on_the_value_not_the_new_key(
    tmp_path, monkeypatch, capsys, paper_seams, window, expected_status
):
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
            {"market_data": {"candle_interval": "4h", "candle_lookback": 200}, "coin": "BTC"}
        ),
    )
    db.close()
    paper_seams.write_text(
        "market_data:\n"
        "  candle_interval: 4h\n"
        "  candle_lookback: 200\n"
        f"  volume_profile_window_candles: {window}\n",
        encoding="utf-8",
    )

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", stop_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert ("config drift on resume" in capsys.readouterr().err) == (expected_status == "drift")

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == expected_status
    db.close()


def test_paper_resume_clean_stamps_ok_breadcrumb(tmp_path, monkeypatch, paper_seams):
    # A clean resume overwrites any earlier "drift" verdict: a reverted config
    # must not leave a stale drift as the store's last word.
    import contrib.hyperliquid_perp.cli as cli_mod

    path, db = seed_db(tmp_path)  # no genesis config record -> nothing drifts
    db.close()

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", stop_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "ok"
    assert state["last_config_drift_error"] is None
    db.close()


def test_paper_ctrl_c_shutdown_retries_pending_funding_before_final_export(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # The Ctrl-C/SIGTERM lane is the other "last word" export: drive a real
    # KeyboardInterrupt through _cmd_paper and pin backfill-before-export.
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.engine import PaperExecutionEngine
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    # Keyless restart over live work → protection-only: the REAL _paper_loop
    # runs without a scheduler, so the interrupt is the only exit path.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )
    monkeypatch.setattr(PaperExecutionEngine, "tick", lambda self: None)
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: calls.append("backfill"),
    )
    monkeypatch.setattr(
        cli_mod.paper_export,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), True)[1],
    )
    monkeypatch.setattr(
        cli_mod.paper.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 0
    assert "final export" in capsys.readouterr().err
    assert calls == ["backfill", "export"]


def test_paper_protection_only_startup_notes_stranded_in_progress_attempt(
    tmp_path, capsys, monkeypatch, paper_seams
):
    """A crash mid-decision-cycle leaves the attempt in_progress (§3.1 persists
    the try before the AI call); a restart into protection-only never polls the
    scheduler, so nothing can resume or terminalize it for the whole halted
    lifetime. Deliberately kept that way — only a healthy restart may resume
    the SAME attempt — but the operator must be told, not left to find a
    perpetually-open cycle in a post-mortem."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.engine import PaperExecutionEngine
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    insert_decision_attempts(db, ["in_progress"], start=_T0)
    db.close()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # keyless → protection-only
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )
    monkeypatch.setattr(PaperExecutionEngine, "tick", lambda self: None)
    monkeypatch.setattr(
        cli_mod.paper_export, "_post_cycle_export", lambda db_, run_id, export_dir: True
    )
    monkeypatch.setattr(
        cli_mod.paper.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 0
    err = capsys.readouterr().err
    assert "remains in_progress" in err
    assert "next healthy restart" in err
    # The attempt itself was left untouched — resumable state must survive.
    db = Database(path)
    row = repo.find_in_progress_attempt(db.conn, "r")
    assert row is not None and row["attempt_count"] == 1
    db.close()


# --------------------------------------------------------------------------
# exception lanes through the paper stack: each raising seam actually fires
# (not mocked to a never-raising no-op), pinning the cross-frame wiring
# --------------------------------------------------------------------------


def test_paper_lease_takeover_exits_1_without_export_and_preserves_successor(
    tmp_path, capsys, monkeypatch, paper_seams
):
    """Drive a REAL lease takeover through the full paper stack: a successor
    re-acquires mid-run, the next real ``heartbeat_run_lock`` hits its pid
    fence and raises ``RunLockError`` out of ``_paper_loop``, ``_run_locked``
    maps it to exit 1 WITHOUT the shutdown export (every store write would
    corrupt the successor's view), and the outer ``finally``'s pid-guarded
    release must NOT clear the successor's fresh lease."""
    import os

    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.engine import PaperExecutionEngine
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    db.close()
    # Keyless restart over live work → protection-only: the REAL _paper_loop
    # runs without a scheduler (poll skipped), isolating the lease wiring.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error=None,
            replay_status="ok",
        ),
    )
    monkeypatch.setattr(PaperExecutionEngine, "tick", lambda self: None)
    exports: list[str] = []
    monkeypatch.setattr(
        cli_mod.paper_export,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
    )
    monkeypatch.setattr(cli_mod.paper.time, "sleep", lambda s: None)

    real_heartbeat = run_lock_mod.heartbeat_run_lock
    our_pid = os.getpid()
    successor_pid = our_pid + 1
    beats: list[int] = []

    def hijacking_heartbeat(db_, run_id, *, pid, now):
        beats.append(pid)
        if len(beats) == 2:
            # The successor took over between two heartbeats (our lease looked
            # stale from its side): real release+acquire, then the REAL
            # heartbeat below must hit its pid fence and raise.
            run_lock_mod.release_run_lock(db_, run_id, pid=pid, now=now)
            run_lock_mod.acquire_run_lock(db_, run_id, pid=successor_pid, now=now)
        real_heartbeat(db_, run_id, pid=pid, now=now)

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", hijacking_heartbeat)

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "no longer held by pid" in capsys.readouterr().err
    assert beats == [our_pid, our_pid]  # the raise came from the 2nd heartbeat
    assert exports == []  # no shutdown export — the successor owns the store now
    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["lock_pid"] == successor_pid  # finally's release no-oped (pid guard)
    db.close()


def test_paper_flat_restart_reconciliation_error_exits_1_and_stamps_breadcrumb(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # Flat restart over unverifiable books: the raiser is unit-tested in
    # test_reconcile; this pins the cli wiring — exit 1 plus the refusal's own
    # lane vocabulary ("failed") reaching the durable replay breadcrumb.
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod

    path, db = seed_db(tmp_path)
    db.close()

    def raising_reconcile(db_, *, run_id, now, funding_source):
        raise reconcile_mod.ReconciliationError("books corrupt", replay_status="failed")

    monkeypatch.setattr(reconcile_mod, "reconcile_on_restart", raising_reconcile)
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "books corrupt" in capsys.readouterr().err
    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "failed"
    assert "books corrupt" in state["last_replay_error"]
    db.close()


def test_paper_exchange_error_before_lease_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # The asset-meta fetch precedes the lease: an ExchangeError must land in
    # the named exit-1 lane with the message, not a traceback (exit 2).
    from contrib.hyperliquid_perp.exchanges.hyperliquid import market_data as md_mod
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeError

    def raising_meta(self, coin):
        raise ExchangeError("meta endpoint down")

    monkeypatch.setattr(md_mod.HyperliquidMarketData, "get_asset_meta", raising_meta)
    rc = cli_main(paper_argv(tmp_path / "new.db", run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    assert "meta endpoint down" in capsys.readouterr().err


def test_paper_acquire_conflict_with_live_holder_exits_1(tmp_path, capsys, paper_seams):
    # A second process on the same run refuses at startup while the holder's
    # heartbeat is fresh (raiser unit-tested in test_run_lock; this pins the
    # _cmd_paper wiring and its exit-1 mapping).
    import os

    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    path, db = seed_db(tmp_path)
    run_lock_mod.acquire_run_lock(db, "r", pid=os.getpid() + 1, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "already being driven" in capsys.readouterr().err


@pytest.mark.parametrize("behind_version", BEHIND_VERSIONS)
def test_paper_lease_conflict_leaves_a_behind_store_unmigrated(
    tmp_path, capsys, paper_seams, behind_version
):
    # Issue #129: ``paper`` opened the store with the default migrate-on-open
    # and only THEN reached the lease check, so a new build started by hand on
    # the deploy box upgraded the schema underneath the still-running old
    # daemon on its way to being refused. Same policy live-smoke adopted: the
    # refusal must leave the store byte-for-byte the version the daemon owns.
    # At the lease floor too (issue #147): the refusal's own reads must not
    # need anything younger than the lease columns.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def build():
        path, db = seed_db(tmp_path)
        run_lock_mod.acquire_run_lock(db, "r", pid=os.getpid() + 1, now=datetime.now(timezone.utc))
        db.close()
        return path

    behind, path = build_behind(build, behind_version)
    assert stored_version(path) == behind

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "already being driven" in capsys.readouterr().err
    assert stored_version(path) == behind


def test_paper_migrates_a_behind_store_once_it_holds_the_lease(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # The other half of deferring: once the lease IS ours the upgrade must
    # happen — an owning command that never migrated would run the new build's
    # SQL against the old schema. The keyless flat-restart refusal fires
    # inside the lease-holding tail, so exit 1 here proves the migration ran
    # before it and the store is now current.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def build():
        path, db = seed_db(tmp_path)
        db.close()
        return path

    behind, path = build_one_behind(build)
    assert stored_version(path) == behind

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
    assert stored_version(path) == SCHEMA_VERSION


def test_paper_refuses_a_store_older_than_the_lease_floor_by_name(tmp_path, capsys, paper_seams):
    # Issue #147 (item 5), at the surface it was filed against: an owning
    # command reads the lease before it migrates, and a store from before the
    # lease columns existed used to die on that read as an OperationalError
    # — main()'s exit-2 traceback. Now a named exit 1 that says what to run,
    # with the store left at the version it was.
    from contrib.hyperliquid_perp.persistence.schema import LEASE_READABLE_SINCE

    below = LEASE_READABLE_SINCE - 1
    path = tmp_path / "pre-lease.db"
    build_store_at(path, below)
    assert stored_version(path) == below

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert f"error: store schema is v{below}" in err
    assert "safe-mode --status" in err
    assert "fatal:" not in err  # main()'s exit-2 last resort never ran
    assert stored_version(path) == below


def test_paper_will_not_migrate_under_a_sibling_runs_fresh_lease(tmp_path, capsys, paper_seams):
    # The store-wide half of #129: the lease is per-run, the migration is
    # per-file. Two paper runs sharing one --db (the default layout for two
    # coins) — the OLD build drives "other", the new build starts "r": its
    # own lease is free, but upgrading now would rewrite the schema under
    # "other". Refused by name; version unchanged; the lease "r" just took is
    # released (the refusal sits inside the lease-releasing try).
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def build():
        path, db = seed_db(tmp_path)
        accounting.initialize_run(
            db, run_id="other", mode="paper", initial_balance_usdc=D(1000), schema_version=1
        )
        run_lock_mod.acquire_run_lock(
            db, "other", pid=os.getpid() + 1, now=datetime.now(timezone.utc)
        )
        db.close()
        return path

    behind, path = build_one_behind(build)
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "run 'other'" in err and "needs to migrate the store" in err
    assert stored_version(path) == behind
    probe = connect(path)
    assert (
        probe.execute("SELECT lock_pid FROM scheduler_state WHERE run_id = 'r'").fetchone()[0]
        is None
    )
    probe.close()


def test_paper_restart_beside_a_sibling_is_unaffected_when_the_store_is_current(
    tmp_path, capsys, monkeypatch, paper_seams
):
    # Negative control for the guard above: nothing owed → no sibling check,
    # so a routine restart next to a running sibling proceeds to its ordinary
    # next refusal (here the keyless flat-restart one).
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path, db = seed_db(tmp_path)
    accounting.initialize_run(
        db, run_id="other", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    run_lock_mod.acquire_run_lock(db, "other", pid=os.getpid() + 1, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "needs to migrate the store" not in err
    assert "OPENROUTER_API_KEY" in err


def test_paper_refuses_a_newer_store_before_stamping_its_lease(tmp_path, capsys, paper_seams):
    # The deferred open must not cost paper the at-open refusal of a store
    # migrated by a NEWER build: an older binary would otherwise write its
    # lease into columns it does not know and only then refuse. Named exit 1,
    # scheduler_state untouched.
    path, db = seed_db(tmp_path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2099-01-01T00:00:00+00:00"),
        )
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "NEWER build" in capsys.readouterr().err
    probe = connect(path)
    assert (
        probe.execute("SELECT lock_pid FROM scheduler_state WHERE run_id = 'r'").fetchone() is None
    )
    probe.close()


def test_paper_builds_an_empty_store_file_in_full(tmp_path, capsys, paper_seams):
    # A file with no schema (a `touch`, or an open that died before its first
    # migration committed) has no owner and no lease table to consult, so it
    # is not "an existing store to defer on" — it is built on the way in and
    # the command proceeds to the ordinary "run does not exist" refusal.
    path = tmp_path / "touched.db"
    path.touch()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "run 'r' does not exist" in capsys.readouterr().err  # not an exit-2 traceback
    assert stored_version(path) == SCHEMA_VERSION


def test_paper_refuses_a_db_that_is_another_application_s_database(tmp_path, capsys, paper_seams):
    # Issue #174 end to end, through the CLI an operator actually types: the
    # damage was reachable by a typo in --db, and the fix is only worth
    # anything if it surfaces as a named exit 1 rather than a main() exit-2
    # traceback. --create is the more dangerous half — it is the flag that
    # says "build me a store here".
    path = tmp_path / "someone-elses.db"
    other = sqlite3.connect(str(path))
    try:
        other.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT)")
        other.commit()
    finally:
        other.close()
    before = path.read_bytes()

    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams, create=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not one of this project's stores" in err
    assert "customers" in err  # names what it found, so the operator sees WHICH file
    assert path.read_bytes() == before  # and did not build a store into it


def test_paper_create_builds_a_new_store_in_full(tmp_path, monkeypatch, paper_seams):
    # A store that does not exist yet has no daemon to own it, so --create
    # still migrates on open (the lease table it needs is itself a migration).
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "new.db"
    rc = cli_main(paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 1  # the keyless fresh-run refusal, after the store exists
    assert stored_version(path) == SCHEMA_VERSION


def test_paper_configures_logging_for_the_daemon(tmp_path, monkeypatch):
    # The multi-day daemon wires timestamped INFO logging at entry (the other
    # subcommands stay unconfigured); basicConfig itself no-ops when an
    # embedding app already installed handlers, so record the call instead of
    # inspecting global logger state.
    import contrib.hyperliquid_perp.cli as cli_mod

    seen: dict = {}
    monkeypatch.setattr(cli_mod.logging, "basicConfig", lambda **kwargs: seen.update(kwargs))
    rc = cli_main(["paper", "--coin", "BTC", "--db", str(tmp_path / "missing.db")])
    assert rc == 1  # missing store without --create: the early named exit
    assert seen["level"] == cli_mod.logging.INFO
    assert "%(asctime)s" in seen["format"]


def test_paper_protection_only_restart_skips_provider_and_stamps_failed(
    tmp_path, monkeypatch, paper_seams
):
    """A protection-only restart must not require the decision stack at all: no
    API key (settled), and — same principle — no ``_EngineDecisionProvider``
    construction (its deep tradingagents import could only add failure modes to
    a startup whose one job is keeping SL/TP alive). The replay-raise lane also
    stamps the "failed" breadcrumb, mirroring the mid-run verify."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path, db = seed_db(tmp_path)
    db.close()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        reconcile_mod,
        "reconcile_on_restart",
        lambda db_, *, run_id, now, funding_source: RestartReconciliation(
            canceled_plan_ids=(),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=False,
            replay_error="replay raised: boom",
            replay_status="failed",
        ),
    )

    def _forbid_provider(*args, **kwargs):
        raise AssertionError("protection-only must not build the decision provider")

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _forbid_provider)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        return 0

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", fake_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["scheduler"] is None

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "failed"
    assert "boom" in state["last_replay_error"]
    db.close()


def test_paper_corrupt_genesis_config_json_resumes_with_drift_warning(
    tmp_path, monkeypatch, capsys, paper_seams
):
    # A genesis config_json this process cannot parse makes the homogeneity
    # check impossible — that is breadcrumb-grade (warn like parameter drift),
    # never a startup abort that would fire before the protection-only fork.
    import contrib.hyperliquid_perp.cli as cli_mod

    path, db = seed_db(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE runs SET config_json = '{not json' WHERE run_id = 'r'")
    db.close()

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.paper, "_paper_loop", stop_loop)
    assert cli_main(paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert "could not verify config drift" in capsys.readouterr().err

    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_config_drift_status"] == "drift"
    assert "could not verify" in state["last_config_drift_error"]
    db.close()


def test_paper_bad_paper_trading_value_exits_1(tmp_path, capsys, paper_seams):
    # A bad paper_trading: value is an operator config mistake — the named
    # exit-1 lane (via _load_risk_decision's validation parse), never an
    # exit-2 "unexpected error" traceback.
    paper_seams.write_text(
        "paper_trading:\n  account:\n    initial_balance_usdc: -5\n", encoding="utf-8"
    )
    path, db = seed_db(tmp_path)
    db.close()
    rc = cli_main(paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "paper_trading" in err
    assert "Fix the YAML" in err


def test_the_paper_daemon_wires_the_books_as_the_provider_position_source(
    tmp_path, monkeypatch, paper_seams
):
    # The paper lane: build_decision_provider binds read_books over the run's
    # store, so a fresh run's very first prompt already carries the section
    # (the books are seeded before the first cycle). The recorder stops the
    # command right at the provider pre-flight, before initialize_run.
    captured = {}

    class _Recording:
        def __init__(self, *args, **kwargs):
            # Exercised HERE, while the store is still open (the command
            # closes it on the way out): bound over books that were never
            # seeded, the read says None (section omitted), not a crash.
            captured["book"] = kwargs["position_source"]()
            captured["source"] = kwargs["position_source"]
            captured["payload_dir"] = kwargs["payload_dir"]
            raise StopBeforeTheLoop

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _Recording)
    rc = cli_main(paper_argv(tmp_path / "new.db", run_id="fresh", config=paper_seams, create=True))
    assert rc == 2  # the sentinel surfaces as the top-level "unexpected error"
    assert captured["book"] is None
    # The binding itself, not only its no-books result: a swapped run_id/coin
    # would ALSO read "no books" here (no ledger is keyed on "BTC" either).
    assert_position_source_binds(captured["source"], run_id="fresh", coin="BTC")
    # And where this run's AI payloads go: the one layout the backfill reads
    # back from (issue #221) — the live and smoke writers have the same pin
    # through assert_payload_dir; the paper writer is this one.
    assert captured["payload_dir"] == store_layout.payload_dir(tmp_path / "new.db", "fresh")
