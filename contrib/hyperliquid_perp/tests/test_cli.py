"""Tests for the subcommand CLI's offline paths (export / validate / dispatch).

The long-running ``paper`` loop and its network/LLM seams are exercised only up
to their credential/store guards — everything beyond needs a live exchange.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import (
    _UNVERIFIED_MARKER,
    _classify_engine_error,
    _config_drift_report,
    _live_heartbeat,
    _mark_export_verification,
    _post_cycle_export,
    _raise_keyboard_interrupt,
    _run_config_subset,
    _still_owns_run,
    main as cli_main,
)
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.schema import PerpMarketContext
from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.scheduler import DecisionInput
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database, connect
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from .conftest import (
    _assert_paired_sweep_refreshes,
    _assert_payload_dir,
    insert_decision_attempts,
    record_reconciliation_sweep_wiring,
)
from .test_live_startup import _clearinghouse

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _perp_ctx(as_of: datetime, coin: str = "BTC") -> PerpMarketContext:
    return PerpMarketContext(
        coin=coin,
        as_of=as_of,
        candle_interval="4h",
        candle_count=200,
        mark_price=D(50000),
        oracle_price=D(50000),
        prev_day_price=D(50000),
        mid_price=D(50000),
        day_change_pct=None,
        open_interest=D(0),
        day_ntl_volume=D(0),
        funding_rate=D("0.0001"),
        funding_premium=None,
        funding_zscore_30d=None,
        funding_window_days=30,
        funding_sample_count=0,
        # A healthy indicator set: build_input now applies the shared
        # _context_refusal_error guards, and a context without a usable atr_14
        # is (correctly) refused before it reaches the code under test.
        indicators={"rsi_14": 55.0, "ema_20": 50000.0, "ema_50": 49000.0, "atr_14": 1200.0},
    )


def _seed_db(tmp_path):
    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return path, db


def test_request_decision_drives_engine_with_cycle_as_of_not_now(monkeypatch):
    # DA3: the base engine's trade_date must track the cycle's as_of (a single
    # time base with the perp context), not wall-clock now — a late/recovery
    # cycle otherwise feeds today's news alongside the (older) market context.
    import contrib.hyperliquid_perp.integration.trading_graph as tg
    from contrib.hyperliquid_perp.cli import _EngineDecisionProvider

    captured: dict = {}

    class _FakeGraph:
        def propagate(self, coin, trade_date, asset_type="crypto"):
            captured.update(coin=coin, trade_date=trade_date, asset_type=asset_type)
            return (
                {
                    "final_trade_decision": (
                        '{"decision_mode": "set_target", "target_side": "long", '
                        '"requested_target_margin_pct": 1, "confidence": 0.8, '
                        '"rationale": "r", "key_risks": ["a risk"]}'
                    )
                },
                "signal",
            )

    monkeypatch.setattr(tg, "build_graph", lambda **_kw: _FakeGraph())

    # Bypass the heavy __init__ (engine-config build); request_decision only
    # reads the five attributes set below.
    provider = object.__new__(_EngineDecisionProvider)
    provider._context_text = "ctx"
    provider._format_text = "fmt"
    provider._analysts = []
    provider._engine_config = {"deep_think_llm": "model-x"}
    provider._decision = DecisionConfig()

    as_of = datetime(2026, 3, 15, 2, 30, tzinfo=timezone.utc)
    provider.request_decision(DecisionInput(context=_perp_ctx(as_of)))

    assert captured["trade_date"] == "2026-03-15"  # the cycle's as_of date, not today
    assert captured["coin"] == "BTC"
    assert captured["asset_type"] == "crypto"


def test_build_input_payload_write_failure_rides_retry_ladder(tmp_path, monkeypatch):
    # An environmental filesystem failure on the audit payload (disk full,
    # permissions) must not tear down the daemon (exit 2, SL/TP unwatched):
    # build_input classifies it into the §3.1 ladder like its sibling
    # environmental failures, so the worst case is an api_failed cycle whose
    # error_message names the cause.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.cli import _EngineDecisionProvider
    from contrib.hyperliquid_perp.paper.scheduler import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of)
    # **kw absorbs on_blocking_read: the live provider passes the kill-switch
    # refresh so _build_context's market reads do not form one unrefreshed chain.
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))
    monkeypatch.setattr(bridge_mod, "_warmup_threshold", lambda config: 1)

    provider = object.__new__(_EngineDecisionProvider)
    provider._config = {}
    provider._risk = RiskConfig(leverage=D(5), max_target_margin_pct=60)
    provider._decision = DecisionConfig()
    # A FILE where the payload directory must go: mkdir(exist_ok=True) still
    # raises FileExistsError (an OSError) — the same landing zone as ENOSPC.
    blocked = tmp_path / "payloads"
    blocked.write_text("not a directory", encoding="utf-8")
    provider._payload_dir = blocked

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=as_of)
    assert exc_info.value.error_type == "server_error"
    assert "payload write failed" in exc_info.value.message


@pytest.mark.parametrize(
    ("candle_count", "indicators", "expected_msg"),
    [
        # Under-warmed feed: candle_count 100 sits below the monkeypatched
        # threshold (150) but above the default indicator set's 50, so the
        # daemon's build_input must both ride the one-shot path's warm-up
        # guard and actually consult _warmup_threshold(config) — a guard
        # falling back to a hardcoded/default threshold clears 100 and
        # reports a different refusal. Keys present with all-None values
        # (compute_indicators' real under-warm output): the shape also
        # satisfies the dead-set/regime guards, so a guard-order swap would
        # surface their messages instead and fail this case.
        (
            100,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "under-warmed",
        ),
        # Fully-dead known-indicator set (stockstats broken): must become an
        # api_failed cycle (no AI spend), not a prompt asserting a
        # fabricated-calm RANGING regime every 4h.
        (
            200,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "every technical indicator failed",
        ),
        # atr_14 absent (dropped from `indicators:` on a pre-upgrade config):
        # classify_regime would silently default to RANGING, hiding a volatile
        # market.
        (
            200,
            {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0},
            "atr_14 is unavailable",
        ),
        # A dead EMA is just as regime-critical: RANGING would also hide a
        # trending market.
        (
            200,
            {"rsi_14": 55.0, "ema_20": None, "ema_50": 59000.0, "atr_14": 250.0},
            "ema_20 is unavailable",
        ),
    ],
)
def test_build_input_refuses_untradeable_indicators(
    monkeypatch, candle_count, indicators, expected_msg
):
    # The daemon shares the one-shot path's pre-LLM context guards (see
    # engine_bridge._context_refusal_error) and rides them down the retry ladder.
    from types import SimpleNamespace

    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.cli import _EngineDecisionProvider
    from contrib.hyperliquid_perp.paper.scheduler import RetryableDecisionError

    # Threshold monkeypatched to 150 — a value the default indicator set's 50
    # can't mimic: candle_count 200 clears the warm-up gate, 100 exercises it
    # only if the guard really reads _warmup_threshold(config).
    ctx = SimpleNamespace(candle_count=candle_count, indicators=indicators)
    # Keyword-only `on_blocking_read` included: the provider always passes it,
    # so a two-arg stand-in raises TypeError before the guard under test runs.
    monkeypatch.setattr(
        bridge_mod, "_build_context", lambda config, coin, on_blocking_read=None: (ctx, None)
    )
    monkeypatch.setattr(bridge_mod, "_warmup_threshold", lambda config: 150)

    # Only _config is needed: the guard fires before the risk/decision/payload
    # attributes are ever read.
    provider = object.__new__(_EngineDecisionProvider)
    provider._config = {}

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc))
    assert exc_info.value.error_type == "server_error"
    assert expected_msg in exc_info.value.message


def test_build_input_refuses_a_stalled_candle_feed(monkeypatch):
    # A stalled feed clears the other three guards (every indicator computes,
    # the regime reads healthy), so without this one the daemon would spend a
    # paid cycle reasoning about a market 20h in the past — and drag the
    # analysts' research window back with it, since as_of becomes trade_date
    # (see test_request_decision_drives_engine_with_cycle_as_of_not_now).
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.cli import _EngineDecisionProvider
    from contrib.hyperliquid_perp.paper.scheduler import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=20))  # 4h bars: past the 3 x 4h bound
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))

    provider = object.__new__(_EngineDecisionProvider)
    provider._config = {}
    provider._on_blocking_read = None

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=as_of)
    # server_error, so it rides the §3.1 ladder to an api_failed cycle rather
    # than tearing the daemon down while SL/TP still need watching.
    assert exc_info.value.error_type == "server_error"
    assert "freshness limit" in exc_info.value.message
    assert "2026-03-14T12:00:00Z" in exc_info.value.message


def test_build_input_measures_freshness_against_the_cycle_clock(tmp_path, monkeypatch):
    # The discriminator for WHICH clock the guard reads: this candle is one
    # hour old relative to the cycle's own as_of, and months stale against the
    # real wall clock (the fixture date is fixed, so the gap only grows). A
    # guard calling datetime.now() itself would refuse it; the daemon's single
    # time base must not.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.cli import _EngineDecisionProvider

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))

    provider = object.__new__(_EngineDecisionProvider)
    provider._config = {}
    provider._on_blocking_read = None
    provider._risk = RiskConfig(leverage=D(5), max_target_margin_pct=60)
    provider._decision = DecisionConfig()
    provider._payload_dir = tmp_path / "payloads"
    provider._engine_config = {"deep_think_llm": "model-x"}

    decision_input = provider.build_input(coin="BTC", as_of=as_of)
    assert decision_input.candle_end == ctx.as_of  # built through, not refused


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


def test_validate_exit_5_when_replay_raises(tmp_path, capsys):
    # A store corrupt enough to crash the replay itself is the strongest
    # "investigate the store" signal: exit 5 with a partial report (ledger
    # metrics n/a), not the generic exit-2 crash lane.
    path, db = _seed_db(tmp_path)
    accounting.post_fill(
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


def test_validate_exit_5_on_unreadable_store(tmp_path, capsys):
    # A file that is not SQLite at all takes the same exit-5 "investigate the
    # store" verdict — not exit 2 ("tool bug") and not exit 1 (operator error).
    bogus = tmp_path / "bogus.db"
    bogus.write_text("this is not a database", encoding="utf-8")
    assert cli_main(["validate", "--run-id", "r", "--db", str(bogus)]) == 5
    assert "store integrity failure" in capsys.readouterr().err


def test_validate_operator_errors_exit_1(tmp_path, capsys):
    path, db = _seed_db(tmp_path)
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
    path, db = _seed_db(tmp_path)
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


def test_paper_fresh_run_missing_api_key_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # A fresh run always drives the AI, so a missing key still refuses — but the
    # check now fires in the fresh-run branch, BEFORE the run row is written, so a
    # retry with the key still sees a clean --create (no half-created run).
    import contrib.hyperliquid_perp.cli as cli_mod

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Sentinel pins the dotenv_diagnosis wiring: the abort message must embed
    # the diagnosis for the actual variable — dropping the interpolation (or
    # diagnosing the wrong var) is invisible to the substring check alone.
    monkeypatch.setattr(cli_mod, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    path = tmp_path / "new.db"
    rc = cli_main(_paper_argv(path, run_id="fresh", config=paper_seams, create=True))
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

    # cli lazy-imports `from .paper import accounting`; patch the module itself.
    monkeypatch.setattr(accounting, "initialize_run", _stop)
    # The provider pre-flight sits between the key check and initialize_run;
    # stub it so this test stays off the real tradingagents import.
    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", lambda *a, **kw: object())
    rc = cli_main(_paper_argv(tmp_path / "new.db", run_id="fresh", config=paper_seams, create=True))

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
    rc = cli_main(_paper_argv(path, run_id="fresh", config=paper_seams, create=True))
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
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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
    accounting.post_fill(
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
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err  # the refusal came from the key check
    assert "manages only" not in err


def test_paper_healthy_restart_missing_api_key_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # A FLAT healthy restart will poll the AI and has nothing to protect, so a
    # missing key still aborts (checked after reconcile + engine construction —
    # a keyless restart holding live work falls back to protection-only instead;
    # see the companion test below).
    path, db = _seed_db(tmp_path)
    db.close()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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

    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _forbid_provider)
    # Sentinel pins the dotenv_diagnosis wiring in the protection-only message
    # (same contract as the fresh-run abort's sentinel above).
    monkeypatch.setattr(cli_mod, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        seen["engine_active"] = engine.has_active_work()
        seen["trading_halted"] = kwargs["trading_halted"]
        seen["halt_reason"] = kwargs["halt_reason"]
        return 0

    monkeypatch.setattr(cli_mod, "_paper_loop", fake_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0
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

    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _boom)
    path = tmp_path / "new.db"
    rc = cli_main(_paper_argv(path, run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    assert "error: importing tradingagents failed" in capsys.readouterr().err
    db = Database(path)
    assert repo.get_run(db.conn, "fresh") is None  # failed before genesis
    db.close()

    # The operator fixes the environment and retries the SAME command: the
    # provider now builds and --create must not hit "already exists".
    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", lambda *a, **kw: object())
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["run_id"] = run_id
        return 0

    monkeypatch.setattr(cli_mod, "_paper_loop", fake_loop)
    rc = cli_main(_paper_argv(path, run_id="fresh", config=paper_seams, create=True))
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
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.engine_bridge import EngineImportError
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    path, db = _seed_db(tmp_path)
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

    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _boom)
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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
    import-error reason."""
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

    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _boom)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        seen["engine_active"] = engine.has_active_work()
        seen["trading_halted"] = kwargs["trading_halted"]
        seen["halt_reason"] = kwargs["halt_reason"]
        return 0

    monkeypatch.setattr(cli_mod, "_paper_loop", fake_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0
    assert seen["scheduler"] is None
    assert seen["engine_active"] is True  # the seeded live position
    assert seen["trading_halted"] is True
    assert seen["halt_reason"] == "import-error"
    err = capsys.readouterr().err
    assert "importing tradingagents failed" in err  # the fixable cause is shown
    assert "protection-only" in err


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
    path, db = _seed_db(tmp_path)
    export_dir = tmp_path / "exp"
    from contrib.hyperliquid_perp.paper import accounting as acc_mod

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
        # A bare "429" used to match here, so any larger number containing those
        # three digits — an oid, an epoch-ms timestamp, a price — filed as a rate
        # limit. The sibling classifier in sdk_client.py deleted the marker for
        # this reason; this copy kept it (2026-08-01 round-18 concept scan).
        (RuntimeError("run 1429 aborted at 14290 ms"), "server_error"),
        # And the class name alone still carries a real one, which is how the
        # SDKs actually surface it.
        (type("RateLimitError", (RuntimeError,), {})("slow down"), "rate_limit"),
        # The spaced phrase — the other half of the argument for deleting "429",
        # and the half nothing asserted (2026-08-01 round-19 mutation probe).
        (RuntimeError("provider rate limit exceeded"), "rate_limit"),
        # Order pinned in the two remaining collisions: rate_limit beats
        # connection, and timeout beats rate_limit.
        (RuntimeError("connection reset while rate limit backoff ran"), "rate_limit"),
        (RuntimeError("rate limit wait timed out"), "timeout"),
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


def test_config_drift_paper_trading_account_change_is_inert():
    # account (initial balance / seeds) is genesis-only: a resume-time edit
    # changes nothing, so it must not trip the "behaviour changes" warning.
    genesis = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 1000},
            "execution": {"fill_model": {"slippage_bps": 5}},
        }
    }
    stored = _subset_json(genesis, "BTC")
    edited_account = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 2000},
            "execution": {"fill_model": {"slippage_bps": 5}},
        }
    }
    assert _config_drift_report(stored, edited_account, "BTC") is None
    # ...while an execution edit (which DOES apply on resume) still warns.
    edited_execution = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 1000},
            "execution": {"fill_model": {"slippage_bps": 9}},
        }
    }
    kind, msg = _config_drift_report(stored, edited_execution, "BTC")
    assert kind == "params"
    assert "paper_trading" in msg


def test_config_drift_covers_engine_market_data_and_indicators():
    # A model/analyst swap, a candle-window change, or an indicator-set change
    # redefines every subsequent decision — all three warn like risk drift.
    genesis = {
        "engine": {"deep_think_llm": "model-a"},
        "market_data": {"candle_interval": "4h", "candle_lookback": 200},
        "indicators": ["atr_14"],
    }
    stored = _subset_json(genesis, "BTC")
    current = {
        "engine": {"deep_think_llm": "model-b"},
        "market_data": {"candle_interval": "1h", "candle_lookback": 200},
        "indicators": ["atr_14", "rsi_14"],
    }
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "engine" in msg and "market_data" in msg and "indicators" in msg


def _live_subset_json(config: dict, coin: str) -> str:
    """Exactly what LIVE run creation persists: the shared subset plus `live:`."""
    subset = _run_config_subset(config, coin)
    subset["live"] = config.get("live")
    return json.dumps(subset, ensure_ascii=False, default=str)


def test_config_drift_live_network_change_is_hard_error():
    # live.network is run IDENTITY, like coin (decided 2026-07-17): resuming a
    # testnet-created run against a mainnet config would arm the wallet-wide
    # kill switch on the mainnet wallet and reconcile a testnet ledger against
    # the mainnet exchange — every leg mismatching, with nothing naming why.
    genesis = {"live": {"network": "testnet", "allow_real_orders": True}}
    stored = _live_subset_json(genesis, "BTC")
    current = {"live": {"network": "mainnet", "allow_real_orders": True}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "network"
    assert "'testnet'" in msg and "'mainnet'" in msg


def test_config_drift_live_network_case_change_is_not_drift():
    # LiveConfig reads network case-insensitively, so `TestNet` must not
    # false-flag a hard error against a stored `testnet`.
    stored = _live_subset_json({"live": {"network": "testnet"}}, "BTC")
    assert _config_drift_report(stored, {"live": {"network": "TestNet"}}, "BTC") is None


def test_config_drift_non_network_live_change_warns():
    # Safety caps / kill-switch timings redefine behaviour from here on: the
    # operator may intend it, so warn rather than refuse.
    genesis = {"live": {"network": "testnet", "safety": {"max_notional_usdc": 100}}}
    stored = _live_subset_json(genesis, "BTC")
    current = {"live": {"network": "testnet", "safety": {"max_notional_usdc": 500}}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "live" in msg


def test_config_drift_paper_genesis_never_reaches_the_live_checks():
    # A paper run's genesis never stores `live:`, but the same YAML may well
    # carry one — absence in the record means "not part of this run's identity",
    # so neither the network hard-fail nor the live warning may fire.
    stored = _subset_json({"risk": {"leverage": 5}}, "BTC")
    current = {"risk": {"leverage": 5}, "live": {"network": "mainnet"}}
    assert _config_drift_report(stored, current, "BTC") is None


def test_config_drift_pre_upgrade_record_skips_later_keys():
    # A genesis record written before engine/market_data/indicators joined the
    # subset lacks those keys entirely; absence means "unknown", not "was
    # empty" — the comparison skips them instead of false-flagging every old
    # run whose config carries the blocks today.
    stored = json.dumps(
        {"risk": {"leverage": 5}, "decision": None, "paper_trading": None, "coin": "BTC"}
    )
    current = {
        "risk": {"leverage": 5},
        "engine": {"deep_think_llm": "model-a"},
        "market_data": {"candle_interval": "4h"},
        "indicators": ["atr_14"],
    }
    assert _config_drift_report(stored, current, "BTC") is None


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
# _live_heartbeat: the §18.2 live-loop lease heartbeat's containment contract
# --------------------------------------------------------------------------


class _RecordingSafeMode:
    """Records enter() calls; optionally raises (the safe-mode write can fail too)."""

    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.entered: list[tuple[tuple, dict]] = []

    def enter(self, *args, **kwargs):
        self.entered.append((args, kwargs))
        if self.raises:
            raise RuntimeError("safe-mode write failed")
        return True


def test_live_heartbeat_run_lock_error_stays_fatal(monkeypatch):
    # The pid fence (RunLockError: this process was superseded by a newer one)
    # must PROPAGATE — two writers must never flip-flop the lease — and must
    # not be softened into a safe-mode entry.
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod

    def fenced(db_, run_id, *, pid, now):
        raise run_lock_mod.RunLockError("superseded by a newer process")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", fenced)
    safe_mode = _RecordingSafeMode()
    with pytest.raises(run_lock_mod.RunLockError):
        _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)
    assert safe_mode.entered == []


def test_live_heartbeat_transient_failure_is_contained_in_safe_mode(monkeypatch):
    # Any OTHER heartbeat failure (a busy SQLite store, say) is contained: no
    # raise — tearing the loop down would run the §18.2 shutdown sweep and
    # strip the resting SL/TP — and exactly one recoverable safe-mode entry
    # with the live-tick-error reason.
    from contrib.hyperliquid_perp.live.safe_mode import REASON_LIVE_TICK_ERROR
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    safe_mode = _RecordingSafeMode()
    _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)  # no raise
    assert len(safe_mode.entered) == 1
    args, kwargs = safe_mode.entered[0]
    assert args == ("recoverable", REASON_LIVE_TICK_ERROR)
    assert kwargs.get("detail")


def test_live_heartbeat_contains_a_failing_safe_mode_write(monkeypatch):
    # The containment must not depend on the safe-mode write succeeding: a
    # store busy enough to fail the heartbeat can fail that write too, and a
    # raise from EITHER must not end the loop.
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    safe_mode = _RecordingSafeMode(raises=True)
    _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)  # still no raise
    assert len(safe_mode.entered) == 1


# --------------------------------------------------------------------------
# _still_owns_run: the positive re-check guarding the §18.2 shutdown sweep
# --------------------------------------------------------------------------


def test_still_owns_run_false_once_a_successor_holds_the_lease(tmp_path):
    # The shutdown sweep's ``superseded`` flag is absence-of-evidence: it is set
    # only when the loop's heartbeat actually RAISED, and the Ctrl-C / SIGTERM
    # lane leaves the loop with no heartbeat at all. So after a tick that blocked
    # past LOCK_STALE_SECONDS a successor can already own the run while this
    # process still reads ``superseded is False`` — and the §18.2 sweep would
    # then cancel the SUCCESSOR's SL/TP and clear the wallet's dead-man switch.
    # This is the re-ASK that catches it.
    from contrib.hyperliquid_perp.paper.run_lock import LOCK_STALE_SECONDS, acquire_run_lock

    db = Database(tmp_path / "own.db")
    acquire_run_lock(db, "r", pid=101, now=_T0)
    takeover_at = _T0 + timedelta(seconds=LOCK_STALE_SECONDS)
    acquire_run_lock(db, "r", pid=202, now=takeover_at)  # legitimate takeover
    assert _still_owns_run(db, "r", pid=101, now=takeover_at + timedelta(seconds=30)) is False
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["lock_pid"] == 202  # and the loser must not stamp itself back on
    assert row["lock_heartbeat_at"] == takeover_at.isoformat()
    db.close()


def test_still_owns_run_true_for_the_holder_and_refreshes_the_lease(tmp_path):
    # The ordinary shutdown: the lease is ours, the sweep must run. The refresh
    # is not incidental — the check goes through heartbeat_run_lock, so the
    # lease stays warm for however long the sweep's cancels take on the wire.
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    db = Database(tmp_path / "own.db")
    acquire_run_lock(db, "r", pid=101, now=_T0)
    beat = _T0 + timedelta(seconds=60)
    assert _still_owns_run(db, "r", pid=101, now=beat) is True
    assert repo.get_scheduler_state(db.conn, "r")["lock_heartbeat_at"] == beat.isoformat()
    db.close()


def test_still_owns_run_fails_open_when_the_store_blips(tmp_path, caplog, monkeypatch):
    # Deliberately fail-OPEN, the opposite of _live_heartbeat's containment: a
    # busy/locked SQLite store is not evidence of supersession, and treating it
    # as one would skip the §18.2 sweep and strand this run's own resting orders
    # on the wallet with nothing left to cancel them. Only RunLockError — a
    # positive answer that someone else holds the lease — gives the run away.
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise sqlite3.OperationalError("database is locked")

    db = Database(tmp_path / "own.db")
    run_lock_mod.acquire_run_lock(db, "r", pid=101, now=_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    with caplog.at_level("WARNING"):
        assert _still_owns_run(db, "r", pid=101, now=_T0 + timedelta(seconds=60)) is True
    assert "could not re-verify the run lease" in caplog.text  # never silent
    db.close()


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


def test_cli_main_loads_dotenv_on_every_invocation(monkeypatch):
    # The subcommand entry loads .env as its first act — before dispatch and
    # before anything reads os.environ — so a key kept only in the repo-root
    # .env satisfies the paper startup checks (main.py's legacy path has the
    # companion ordering test in test_main.py).
    import contrib.hyperliquid_perp.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod, "load_dotenv_files", lambda: calls.append(True))
    assert cli_main(["expot"]) == 1  # even a subcommand typo went through the load
    assert calls == [True]


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
    verification latches ``trading_halted`` AND cancels the in-flight plans
    (no further ``poll()`` calls, no fills on unverifiable books).
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

        def cancel_active_plans(self):
            calls.append("cancel")
            return True

    class _Scheduler:
        def poll(self):
            calls.append("poll")
            return terminal

        def next_due_at(self):  # pragma: no cover — halted branch skips it
            raise AssertionError("next_due_at must not be consulted while halted")

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)  # the tick throttle keys off elapsed clock time
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
    # verification latches the halt and cancels the in-flight plans, so
    # iteration 2 ticks but never polls.
    assert calls == [
        "heartbeat",
        "tick",
        "poll",
        "backfill",
        "export",
        "cancel",
        "heartbeat",
        "tick",
    ]
    # Sleep stays inside the lease-freshness cap.
    assert sleeps and all(s <= 60.0 for s in sleeps)
    db.close()


def test_paper_loop_tick_throttled_to_interval_above_heartbeat_cap(tmp_path, monkeypatch):
    """The wake cadence and the tick cadence must stay decoupled: the loop
    wakes every <=60s for the lease heartbeat, but the tick (the market-data
    fetch) fires only once the configured interval has elapsed — not on every
    wake. Config rejects intervals above the 30s TWAP slice cadence, so this
    pins the loop's defensive invariant with a direct call (interval=120),
    not an operator-reachable configuration."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    clock = ManualClock(_T0)
    calls: list[str] = []
    monkeypatch.setattr(
        run_lock_mod,
        "heartbeat_run_lock",
        lambda db_, run_id, *, pid, now: calls.append("heartbeat"),
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            calls.append(f"tick@{int((clock.now() - _T0).total_seconds())}")

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli_mod._paper_loop(
            db,
            "r",
            _Engine(),
            None,  # halted: poll never runs, isolating the tick cadence
            clock,
            120,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=True,
        )

    # Wakes at 0/60/120/180s; ticks only at 0 and 120 (the 120s interval),
    # while every sleep stays inside the 60s lease-freshness cap.
    assert [c for c in calls if c.startswith("tick")] == ["tick@0", "tick@120"]
    assert calls.count("heartbeat") == 4
    assert sleeps == [60.0, 60.0, 60.0, 60.0]
    db.close()


def test_paper_loop_halted_with_nothing_to_protect_exits_1(tmp_path, monkeypatch, capsys):
    """Protection-only exists for the live position: once SL/TP closes it (no
    active work left), the loop must export the final state (the closing fill
    reaches the CSVs) and exit 1 — not idle as a zombie holding the lease.

    ``scheduler=None`` pins the protection-only construction contract: a halted
    start never builds the scheduler/decision provider, so the loop must never
    touch it."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def record_export(db_, run_id, export_dir):
        calls.append("export")
        return True

    def forbid_sleep(seconds):
        raise AssertionError("must exit before sleeping")

    monkeypatch.setattr(cli_mod, "_post_cycle_export", record_export)
    monkeypatch.setattr(cli_mod.time, "sleep", forbid_sleep)

    class _Engine:
        def __init__(self):
            self.work = True

        def has_active_work(self):
            return self.work

        def tick(self):
            self.work = False  # this tick's SL/TP closes the position

    rc = cli_mod._paper_loop(
        db,
        "r",
        _Engine(),
        None,  # protection-only never builds the scheduler
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert calls == ["export"]  # the final state (closing fill) was published
    err = capsys.readouterr().err
    assert "nothing left to protect" in err
    assert "books never re-verified" in err  # default halt reason: replay
    db.close()


def test_paper_loop_missing_key_settle_exit_names_the_key(tmp_path, monkeypatch, capsys):
    # Same settle-exit lane, but a keyless-healthy halt must tell the operator
    # to set the key — not to investigate a store that verified fine.
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(cli_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True)
    monkeypatch.setattr(
        cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must exit first"))
    )

    class _Engine:
        def __init__(self):
            self.work = True

        def has_active_work(self):
            return self.work

        def tick(self):
            self.work = False

    rc = cli_mod._paper_loop(
        db,
        "r",
        _Engine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
        halt_reason="missing-key",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
    assert "books never re-verified" not in err
    db.close()


def test_paper_loop_import_error_settle_exit_names_the_cause(tmp_path, monkeypatch, capsys):
    # Third settle-exit wording: an import-error halt has healthy books, so the
    # exit message must point at the environment fix — not at investigating a
    # store that verified fine, and not at the API key.
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(cli_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True)
    monkeypatch.setattr(
        cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must exit first"))
    )

    class _Engine:
        def __init__(self):
            self.work = True

        def has_active_work(self):
            return self.work

        def tick(self):
            self.work = False

    rc = cli_mod._paper_loop(
        db,
        "r",
        _Engine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
        halt_reason="import-error",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "failed to import" in err
    assert "books never re-verified" not in err
    assert "OPENROUTER_API_KEY" not in err
    db.close()


def test_paper_loop_halted_retries_pending_funding_hourly(tmp_path, monkeypatch):
    """Protection-only never polls the scheduler, so it never reaches the
    cycle-terminal funding retry — the loop must retry pending funding on its
    own wall-clock cadence instead, or a transiently unresolvable hour stays
    pending (its P&L uncounted) for the run's whole halted lifetime. The first
    retry waits a full period: every entry into halted mode has just run a
    backfill."""
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    clock = ManualClock(_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    retries: list[int] = []
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: retries.append(
            int((now - _T0).total_seconds())
        ),
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            pass

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 250:  # ~2h05m of 30s (interval) wakes — two retry periods
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli_mod._paper_loop(
            db,
            "r",
            _Engine(),
            None,  # protection-only never builds the scheduler
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=True,
        )
    # Fires once per hour on the wall clock — not on every 30s wake, and not
    # immediately on entry (a backfill just ran on every path into halted mode).
    assert retries == [3600, 7200]
    db.close()


def test_paper_loop_mid_run_halt_arms_hourly_funding_retry(tmp_path, monkeypatch):
    # The other entry into halted mode: a mid-run replay failure. The halt must
    # arm the hourly retry timer too (one full period out — the cycle-terminal
    # backfill just ran), or a mid-run-halted loop would never retry again.
    from datetime import timedelta

    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult

    path, db = _seed_db(tmp_path)
    clock = ManualClock(_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    retries: list[int] = []
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: retries.append(
            int((now - _T0).total_seconds())
        ),
    )
    # The failing verification flips the loop into halted mode on iteration 1.
    monkeypatch.setattr(cli_mod, "_post_cycle_export", lambda db_, run_id, export_dir: False)

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
            pass

        def cancel_active_plans(self):
            return False

    class _Scheduler:
        def poll(self):
            return terminal

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 125:  # ~1h02m of 30s (interval) wakes — one retry period
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
    # t=0: the cycle-terminal lane's direct backfill (then the halt); t=3600:
    # the halted-mode timer's first fire, one full period after the halt.
    assert retries == [0, 3600]
    db.close()


def test_paper_loop_settle_exit_retries_pending_funding_before_final_export(tmp_path, monkeypatch):
    # The settle-exit CSVs are the run's last word — pending funding that can
    # resolve now must be posted before that final export, not left uncounted
    # forever because the process exits.
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: calls.append("backfill"),
    )
    monkeypatch.setattr(
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), True)[1],
    )
    monkeypatch.setattr(
        cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must exit first"))
    )

    class _Engine:
        def __init__(self):
            self.work = True

        def has_active_work(self):
            return self.work

        def tick(self):
            self.work = False  # this tick's SL/TP closes the position

    rc = cli_mod._paper_loop(
        db,
        "r",
        _Engine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert calls == ["backfill", "export"]
    db.close()


def test_paper_loop_shutdown_funding_retry_is_best_effort(tmp_path, monkeypatch):
    # Contrast with the fail-loud cycle-terminal lane: in the settle-exit lane
    # the retry exists to complete the final CSVs — a raising retry must not
    # cost us the export itself (or, in the halted timer, kill the loop that
    # keeps SL/TP alive).
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock

    path, db = _seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def raising_backfill(db_, *, run_id, now, funding_source):
        raise RuntimeError("funding source broke mid-backfill")

    monkeypatch.setattr(reconcile_mod, "backfill_pending_funding", raising_backfill)
    exports: list[str] = []
    monkeypatch.setattr(
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
    )
    monkeypatch.setattr(
        cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must exit first"))
    )

    class _Engine:
        def __init__(self):
            self.work = True

        def has_active_work(self):
            return self.work

        def tick(self):
            self.work = False

    rc = cli_mod._paper_loop(
        db,
        "r",
        _Engine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert exports == ["r"]  # the final export still happened
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
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), True)[1],
    )
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))

    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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
    monkeypatch.setattr(cli_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True)
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))

    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
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
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
    )
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)

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

    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
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

    path, db = _seed_db(tmp_path)
    db.close()

    def raising_reconcile(db_, *, run_id, now, funding_source):
        raise reconcile_mod.ReconciliationError("books corrupt", replay_status="failed")

    monkeypatch.setattr(reconcile_mod, "reconcile_on_restart", raising_reconcile)
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "books corrupt" in capsys.readouterr().err
    db = Database(path)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["last_replay_status"] == "failed"
    assert "books corrupt" in state["last_replay_error"]
    db.close()


def test_paper_loop_does_not_swallow_backfill_runtime_error(tmp_path, monkeypatch):
    # RuntimeError out of backfill_pending_funding is fail-loud by design:
    # nothing in the loop may contain it (main() maps it to exit 2, already
    # pinned by test_cli_main_wrapper_maps_interrupt_and_unexpected_error).
    from datetime import timedelta

    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod, run_lock as run_lock_mod
    from contrib.hyperliquid_perp.paper.clock import ManualClock
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult

    path, db = _seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def raising_backfill(db_, *, run_id, now, funding_source):
        raise RuntimeError("funding source broke mid-backfill")

    monkeypatch.setattr(reconcile_mod, "backfill_pending_funding", raising_backfill)
    exports: list[str] = []
    monkeypatch.setattr(
        cli_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
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
            pass

    class _Scheduler:
        def poll(self):
            return terminal

    with pytest.raises(RuntimeError, match="mid-backfill"):
        cli_mod._paper_loop(
            db,
            "r",
            _Engine(),
            _Scheduler(),
            ManualClock(_T0),
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )
    assert exports == []  # backfill precedes the export in the terminal branch
    db.close()


def test_post_cycle_export_breadcrumb_write_failure_is_fail_loud(tmp_path, monkeypatch):
    # Settled 9th-loop lane: the durable breadcrumb is trading-write-grade — a
    # stamp failure must escape _post_cycle_export (killing the loop, exit 2
    # via main), not be contained like the export/replay outcomes it records.
    import sqlite3

    import contrib.hyperliquid_perp.cli as cli_mod

    path, db = _seed_db(tmp_path)

    def raising_stamp(db_, run_id, kind, status, error):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli_mod, "_stamp_breadcrumb", raising_stamp)
    with pytest.raises(sqlite3.OperationalError):
        _post_cycle_export(db, "r", tmp_path / "exp")
    db.close()


def test_paper_exchange_error_before_lease_exits_1(tmp_path, capsys, monkeypatch, paper_seams):
    # The asset-meta fetch precedes the lease: an ExchangeError must land in
    # the named exit-1 lane with the message, not a traceback (exit 2).
    from contrib.hyperliquid_perp.exchanges.hyperliquid import market_data as md_mod
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeError

    def raising_meta(self, coin):
        raise ExchangeError("meta endpoint down")

    monkeypatch.setattr(md_mod.HyperliquidMarketData, "get_asset_meta", raising_meta)
    rc = cli_main(_paper_argv(tmp_path / "new.db", run_id="fresh", config=paper_seams, create=True))
    assert rc == 1
    assert "meta endpoint down" in capsys.readouterr().err


def test_paper_acquire_conflict_with_live_holder_exits_1(tmp_path, capsys, paper_seams):
    # A second process on the same run refuses at startup while the holder's
    # heartbeat is fresh (raiser unit-tested in test_run_lock; this pins the
    # _cmd_paper wiring and its exit-1 mapping).
    import os

    from contrib.hyperliquid_perp.paper import run_lock as run_lock_mod

    path, db = _seed_db(tmp_path)
    run_lock_mod.acquire_run_lock(db, "r", pid=os.getpid() + 1, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    assert "already being driven" in capsys.readouterr().err


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

    path, db = _seed_db(tmp_path)
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

    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _forbid_provider)
    seen: dict[str, object] = {}

    def fake_loop(db_, run_id, engine, scheduler, *args, **kwargs):
        seen["scheduler"] = scheduler
        return 0

    monkeypatch.setattr(cli_mod, "_paper_loop", fake_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0
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

    path, db = _seed_db(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE runs SET config_json = '{not json' WHERE run_id = 'r'")
    db.close()

    def stop_loop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_paper_loop", stop_loop)
    assert cli_main(_paper_argv(path, run_id="r", config=paper_seams)) == 0
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
    path, db = _seed_db(tmp_path)
    db.close()
    rc = cli_main(_paper_argv(path, run_id="r", config=paper_seams))
    assert rc == 1
    err = capsys.readouterr().err
    assert "paper_trading" in err
    assert "Fix the YAML" in err


# --------------------------------------------------------------------------
# live — the Phase 3 PR 1 startup skeleton
# --------------------------------------------------------------------------

_LIVE_WALLET = "0x" + "aa" * 20
_LIVE_KEY = "0x" + "11" * 32
_LIVE_ENV = "HYPERLIQUID_AGENT_KEY_TESTNET"


def _live_yaml(
    tmp_path,
    *,
    wallet: str | None = _LIVE_WALLET,
    top_level_network: str | None = None,
    live_lines: str | None = "  mode: testnet_live\n  network: testnet\n",
    risk_lines: str | None = "  leverage: 1\n  margin_mode: cross\n  max_target_margin_pct: 60\n",
    # RUNBOOK §1.5's value, defaulted for the same reason the live-smoke helper
    # defaults it: the 30s top-level default is deliberately illegal in live
    # (30+30+30+15+30 = 135 >= 120 fires the timing preflight), so a helper that
    # omitted it produced configs no operator could actually run — masked until
    # the fake client stopped under-reporting its timeout (2026-08-01 round-16).
    network_timeout_s: float | None = 8,
):
    path = tmp_path / "live-cfg.yaml"
    text = ""
    if wallet is not None:
        text += f'wallet_address: "{wallet}"\n'
    if network_timeout_s is not None:
        text += f"network_timeout_s: {network_timeout_s}\n"
    if top_level_network is not None:
        text += f"network: {top_level_network}\n"
    if risk_lines is not None:
        # The live subcommand requires an explicit risk: block (§24) — the
        # cross-check compares operator intent, never implicit defaults.
        text += "risk:\n" + risk_lines
    if live_lines is not None:
        text += "live:\n" + live_lines
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def live_seams(monkeypatch):
    """Fake every network seam ``_cmd_live`` touches; return mutable knobs.

    The read-only client, account read, authorization check, and signed client
    are all patched at their module seams (the function-local imports bind at
    call time), so tests drive the gate logic offline.
    """
    from contrib.hyperliquid_perp.exchanges.hyperliquid import (
        account as account_mod,
        sdk_client as sdk_mod,
        signed_client as signed_mod,
    )
    from contrib.hyperliquid_perp.live import authorization as auth_mod
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorization

    state = SimpleNamespace(
        equity=D(1000),
        # A flat account by default — the shape every existing live CLI test
        # already assumed the exchange had.
        positions=[],
        account_error=None,
        auth_error=None,
        # Relative to the wall clock because _cmd_live's near-expiry warning
        # compares against real now; 90 days out never trips the 7-day horizon.
        auth_valid_until=datetime.now(timezone.utc) + timedelta(days=90),
        signed_error=None,
        client_error=None,
        auth_calls=[],
        health_calls=[],
        client_networks=[],
        snapshot_requests=[],
        signed_gates=[],
        rest_calls=[],
    )

    from contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client import (
        DEFAULT_NETWORK_TIMEOUT_S,
    )

    class _FakeClient:
        def __init__(self, network="mainnet", *, timeout=None):
            if state.client_error is not None:
                raise state.client_error
            state.client_networks.append(network)
            self.network = network
            self.timeout = timeout
            self.info = SimpleNamespace()

        @classmethod
        def from_config(cls, config, *, timeout=None, network=None):
            # Mirrors the real from_config's network-override contract AND its
            # timeout resolution. Returning None for a config that states one made
            # this double claim "no timeout", which silently dropped a term of the
            # kill switch's timing invariant: `_cmd_live`'s preflight then passed
            # configs that exit 1 in production, so the exit-4 smoke-gate contract
            # these tests assert was never reachable there. The sibling double in
            # test_live_smoke_cli.py was fixed one round earlier; this one was
            # missed (2026-08-01 round-16 review).
            if timeout is None:
                raw = config.get("network_timeout_s")
                timeout = float(raw) if raw is not None else DEFAULT_NETWORK_TIMEOUT_S
            return cls(network=network or config.get("network", "mainnet"), timeout=timeout)

    class _FakeAccount:
        def __init__(self, client):
            pass

        def get_account_snapshot(self, addr):
            state.snapshot_requests.append(addr)
            if state.account_error is not None:
                raise state.account_error
            # ``positions`` mirrors the real snapshot: _live_startup_recovery
            # reads it to reject off-coin holdings on the ``--create`` path.
            # Round 17 added it while pinning the daemon's KillSwitchManager
            # kwargs and claimed the pin needed it; it does not — that test
            # resumes an existing run and never enters the reading branch, and
            # the suite is green without this field. Kept because the double is
            # more faithful with it, and the daemon path really does read it
            # under ``--create`` (2026-08-01 round-18 mutation probe).
            return SimpleNamespace(account_value=state.equity, positions=state.positions)

    def _fake_verify(info, *, wallet_address, agent_key, now=None):
        state.auth_calls.append(wallet_address)
        if state.auth_error is not None:
            raise state.auth_error
        return AgentAuthorization(
            agent_address="0x" + "cc" * 20,
            valid_until=state.auth_valid_until,
        )

    class _FakeSigned:
        def __init__(self, network, agent_key, *, wallet_address, gate, timeout=None):
            self.network = network
            self.wallet_address = wallet_address
            # Mirrors the real signed client, which keeps its resolved timeout —
            # the CLI hands it to KillSwitchManager as the failed-attempt term of
            # the refresh-timing invariant. This is the third double in the suite
            # to have been missing it; the two siblings were fixed in rounds 15
            # and 16, and only a test that reaches manager construction can tell.
            self.timeout = timeout
            # PR 2: the §4.1 gate is bound at construction; the config-only
            # command must hand over a fail-closed gate.
            state.signed_gates.append(gate)

        def health_check(self):
            state.health_calls.append(self.network)
            if state.signed_error is not None:
                raise state.signed_error

        # The recovery components BIND these three at construction (the sweep
        # pin below is what needs them). The real client has all three; this
        # double deliberately has no REST behaviour, so reaching one is a
        # broken test rather than a scenario — recorded, so that claim is
        # checkable instead of being a comment (2026-08-19 review).
        def _no_rest(name):
            def _call(*_args, **_kwargs):
                state.rest_calls.append(name)
                raise NotImplementedError(f"the signed double has no {name}")

            return _call

        user_fills_by_time = _no_rest("user_fills_by_time")
        open_orders = _no_rest("open_orders")
        query_order_by_cloid = _no_rest("query_order_by_cloid")
        del _no_rest

        def __repr__(self):
            return f"FakeSigned(network={self.network!r})"

    monkeypatch.setattr(sdk_mod, "HyperliquidClient", _FakeClient)
    monkeypatch.setattr(account_mod, "HyperliquidAccount", _FakeAccount)
    monkeypatch.setattr(auth_mod, "verify_agent_authorization", _fake_verify)
    monkeypatch.setattr(signed_mod, "HyperliquidSignedClient", _FakeSigned)
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    return state


def test_live_missing_live_block_exits_1(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path, live_lines=None))])
    assert rc == 1
    assert "no live: block" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--create", "--adopt-positions"])
def test_live_create_flags_without_run_id_are_rejected_by_name(tmp_path, capsys, live_seams, flag):
    # Without --run-id the command is config-check mode: it creates and seeds
    # nothing, so these flags had no effect and were silently ignored — an
    # operator would read the "gates OK" exit 0 as "run created". Named
    # rejection, the same discipline the resume/safe-mode flag guards use.
    cfg = _live_yaml(tmp_path)
    rc = cli_main(["live", "--config", str(cfg), flag])
    assert rc == 1
    assert "require --run-id" in capsys.readouterr().err


def test_live_paper_mode_exits_1(tmp_path, capsys, live_seams):
    cfg = _live_yaml(tmp_path, live_lines="  mode: paper\n  network: testnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "paper subcommand" in capsys.readouterr().err


def test_live_missing_network_exits_1(tmp_path, capsys, live_seams):
    # live.network is required (no guessed default to blame the operator for).
    cfg = _live_yaml(tmp_path, live_lines="  mode: testnet_live\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "live.network is required" in capsys.readouterr().err


def test_live_mainnet_live_mode_exits_1(tmp_path, capsys, live_seams):
    # The §22 hard rejection must surface as the named config exit, not a crash.
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_live\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid live: config" in err
    assert "mainnet_live" in err


def test_live_missing_wallet_exits_1(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path, wallet=None))])
    assert rc == 1
    assert "wallet_address" in capsys.readouterr().err


def test_live_missing_key_with_require_exits_1(tmp_path, capsys, live_seams, monkeypatch):
    # require_agent_wallet defaults true: no key -> named startup refusal that
    # names the exact env var for this network.
    monkeypatch.delenv(_LIVE_ENV, raising=False)
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert _LIVE_ENV in err
    assert "require_agent_wallet" in err
    assert live_seams.auth_calls == []


def test_live_real_orders_without_required_wallet_is_a_config_error(
    tmp_path, capsys, live_seams, monkeypatch
):
    # §6 rule 7: allow_real_orders: true + require_agent_wallet: false is a
    # construction-time contradiction — rejected at config load, before any
    # env-var lookup, so arming can never depend on environment presence.
    monkeypatch.delenv(_LIVE_ENV, raising=False)
    cfg = _live_yaml(
        tmp_path,
        live_lines=(
            "  mode: testnet_live\n  network: testnet\n"
            "  allow_real_orders: true\n  require_agent_wallet: false\n"
        ),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "require_agent_wallet" in err
    assert live_seams.auth_calls == []
    assert live_seams.health_calls == []


def test_live_missing_key_with_real_orders_exits_1(tmp_path, capsys, live_seams, monkeypatch):
    # §6 rule 6 (PR 1 revision): real orders asked for with no key is an
    # operator contradiction — named hard fail, never a silent downgrade into
    # an order-less run. The invariant routes it through the
    # require_agent_wallet check; the message must still name rule 6.
    monkeypatch.delenv(_LIVE_ENV, raising=False)
    cfg = _live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert _LIVE_ENV in err
    assert "allow_real_orders" in err
    assert "rule 6" in err
    assert live_seams.auth_calls == []
    assert live_seams.health_calls == []


def test_live_keyless_gate_check_with_orders_off_passes(tmp_path, capsys, live_seams, monkeypatch):
    # The legitimate keyless lane: orders off + not required -> gate check
    # runs without authorization or a signed client.
    monkeypatch.delenv(_LIVE_ENV, raising=False)
    cfg = _live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  require_agent_wallet: false\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "allow_real_orders: false" in out
    # No authorization ran, so the machine-readable block omits its fields.
    assert "agent_address:" not in out
    assert "authorization_valid_until:" not in out
    assert live_seams.auth_calls == []  # nothing to verify without a key
    assert live_seams.health_calls == []  # no signed client without a key


def test_live_real_orders_positive_path(tmp_path, capsys, live_seams):
    # The §4.1 master gate's positive path: key present + allow_real_orders
    # true must reach exit 0 with the flag reported true and UNforced, with
    # authorization and the signed health check both proven.
    cfg = _live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "allow_real_orders: true" in out
    assert "forced" not in out
    assert live_seams.auth_calls == [_LIVE_WALLET]
    assert live_seams.health_calls == ["testnet"]
    # The signed client's bound §4.1 gate must be fresh-from-config: the
    # permissive config bits arrive, but every runtime condition is
    # fail-closed — even asked politely, this client cannot place an order.
    [bound_gate] = live_seams.signed_gates
    assert bound_gate.allow_real_orders is True
    assert bound_gate.mode is ExecutionMode.TESTNET_LIVE
    assert bound_gate.allowed_symbols == ("BTC",)
    assert bound_gate.check_order("BTC") is not None
    # Secret hygiene over the full assembled output: the raw key must never
    # reach stdout or stderr on any live lane.
    assert _LIVE_KEY not in out
    assert _LIVE_KEY not in err


def test_live_missing_risk_block_exits_1(tmp_path, capsys, live_seams):
    # §24: the risk↔live cross-check compares two blocks the operator WROTE;
    # no risk: block -> named exit 1, never a vacuous pass on defaults.
    cfg = _live_yaml(tmp_path, risk_lines=None)
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "no risk: block" in capsys.readouterr().err
    assert live_seams.auth_calls == []


def test_live_client_construction_failure_exits_1(tmp_path, capsys, live_seams):
    # The first network touch: a construction-time failure (DNS, bad SDK) must
    # be a named exit 1 with NO downstream gate having run.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.client_error = ExchangeRequestError("Hyperliquid request failed: boom")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "boom" in err
    assert live_seams.auth_calls == []
    assert live_seams.snapshot_requests == []
    assert live_seams.health_calls == []


def test_live_mainnet_tiny_happy_path_uses_mainnet_key_and_network(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The one mode that can trade real money end-to-end through _cmd_live:
    # the MAINNET env var must be the one consulted, and every seam (client,
    # signed client) must be pinned to mainnet.
    monkeypatch.delenv(_LIVE_ENV, raising=False)
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", _LIVE_KEY)
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "mode: mainnet_tiny" in out
    assert "network: mainnet" in out
    # Default caps at equity 1000: pct 600, min(600, 100) = 100.
    assert "effective_notional_cap: 100 USDC" in out
    assert live_seams.client_networks == ["mainnet"]
    assert live_seams.health_calls == ["mainnet"]
    assert live_seams.auth_calls == [_LIVE_WALLET]
    assert _LIVE_KEY not in out
    assert _LIVE_KEY not in err


def test_live_mainnet_tiny_missing_mainnet_key_names_mainnet_var(
    tmp_path, capsys, live_seams, monkeypatch
):
    # A testnet key alone must NOT satisfy a mainnet_tiny run — the error
    # names the mainnet variable specifically (the split-env-var rationale).
    monkeypatch.delenv("HYPERLIQUID_AGENT_KEY_MAINNET", raising=False)
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "HYPERLIQUID_AGENT_KEY_MAINNET" in capsys.readouterr().err
    assert live_seams.auth_calls == []


def test_live_mainnet_tiny_collects_all_gate_failures(tmp_path, capsys, live_seams, monkeypatch):
    # The collect-all contract holds on the real-money mode too.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    monkeypatch.delenv(_LIVE_ENV, raising=False)
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", _LIVE_KEY)
    live_seams.auth_error = AgentAuthorizationError("not approved")
    live_seams.equity = D(10)  # effective cap 6 < 10 USDC exchange minimum
    live_seams.signed_error = ExchangeRequestError("signed transport down")
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    assert "minimum order value" in err
    assert "signed client health check failed" in err
    assert live_seams.client_networks == ["mainnet"]


def test_live_risk_consistency_mismatch_exits_1(tmp_path, capsys, live_seams):
    # risk: and live.safety: must agree on the sizing regime — a live cap
    # looser than the AI gate's cap is a named config error at startup.
    path = _live_yaml(
        tmp_path,
        risk_lines="  leverage: 1\n  margin_mode: cross\n  max_target_margin_pct: 50\n",
    )
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "risk.max_target_margin_pct" in err
    assert live_seams.auth_calls == []  # rejected before any network work


def test_live_loop_bad_decision_config_exits_1_before_recovery(tmp_path, capsys, live_seams):
    # PR 5 (decided 2026-07-22): --loop consumes the risk:/decision: grid, so
    # a typo'd decision: block must be a named exit-1 at the front gate —
    # never a passing recovery whose loop is then silently skipped behind an
    # exit 0 a supervisor reads as a clean run.
    path = _live_yaml(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("decision:\n  bogus_knob: 1\n")
    rc = cli_main(["live", "--config", str(path), "--run-id", "r1", "--loop"])
    assert rc == 1
    assert "decision" in capsys.readouterr().err
    assert live_seams.auth_calls == []  # rejected before any network work


def test_live_partial_risk_block_exits_1(tmp_path, capsys, live_seams):
    # §24 field granularity: a partial risk: block would let from_dict fill
    # the cross-checked fields from defaults identical to live.safety's,
    # passing the cross-check vacuously — the operator must write them.
    path = _live_yaml(tmp_path, risk_lines="  leverage: 1\n")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "must explicitly write" in err
    assert "margin_mode" in err
    assert "max_target_margin_pct" in err
    assert live_seams.auth_calls == []  # rejected before any network work


def test_live_happy_path_prints_caps_and_exits_0(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 0
    out, err = capsys.readouterr()
    # §5 rule 3 with the default safety caps: equity 1000 -> pct 600, min(600, 100) = 100.
    assert "pct_cap_notional: 600 USDC" in out
    assert "effective_notional_cap: 100 USDC" in out
    assert "mode: testnet_live" in out
    assert "allow_real_orders: false" in out
    # The machine-readable contract includes the authorization result, so a
    # deploy preflight can capture the expiry without scraping stderr.
    assert f"agent_address: {'0x' + 'cc' * 20}" in out
    assert f"authorization_valid_until: {live_seams.auth_valid_until.isoformat()}" in out
    # Authorization ran against the configured wallet, on the live network.
    assert live_seams.auth_calls == [_LIVE_WALLET]
    assert live_seams.client_networks == ["testnet"]
    assert live_seams.snapshot_requests == [_LIVE_WALLET]
    # The signed transport was proven end-to-end (init + health check).
    assert live_seams.health_calls == ["testnet"]
    assert "gates OK" in err
    # Secret hygiene: the raw agent key must never reach the assembled output.
    assert _LIVE_KEY not in out
    assert _LIVE_KEY not in err


def test_live_auth_failure_exits_1(tmp_path, capsys, live_seams):
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    live_seams.auth_error = AgentAuthorizationError("agent 0xcc... is not in wallet list")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    # Collect-all: the independent gates still ran so the operator sees every
    # failure in one pass, then the run exits 1.
    assert live_seams.snapshot_requests == [_LIVE_WALLET]
    assert live_seams.health_calls == ["testnet"]


def test_live_auth_network_failure_exits_1(tmp_path, capsys, live_seams):
    # A network/SDK failure during the §6.1 check must ride the SAME named
    # exit-1 lane as a rejection, not fall into the generic exit-2 bucket.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.auth_error = ExchangeRequestError("Hyperliquid request failed: boom")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    assert "agent authorization failed" in capsys.readouterr().err


def test_live_signed_health_check_failure_exits_1(tmp_path, capsys, live_seams):
    # The one place PR 1 proves the signed transport: a health-check failure
    # must be a named exit 1, never "gates OK".
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.signed_error = ExchangeRequestError("signed transport down")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    out, err = capsys.readouterr()
    assert "signed client health check failed" in err
    assert "gates OK" not in err
    assert "mode:" not in out  # no machine-readable success block on failure


def test_live_key_present_without_require_still_verifies(tmp_path, capsys, live_seams):
    # §6.1 runs whenever a key exists — require_agent_wallet: false must not
    # skip verification of a present (possibly wrong-network) key.
    cfg = _live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  require_agent_wallet: false\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    assert live_seams.auth_calls == [_LIVE_WALLET]
    assert live_seams.health_calls == ["testnet"]


def test_live_collects_all_gate_failures_in_one_pass(tmp_path, capsys, live_seams):
    # An operator with a bad approval AND an underfunded account AND a broken
    # signed transport sees all three named failures on one run.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    live_seams.auth_error = AgentAuthorizationError("not approved")
    live_seams.equity = D(10)  # effective cap 6 < 10 USDC exchange minimum
    live_seams.signed_error = ExchangeRequestError("signed transport down")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    assert "minimum order value" in err
    assert "signed client health check failed" in err


def test_live_near_expiry_authorization_warns_but_passes(tmp_path, capsys, live_seams):
    live_seams.auth_valid_until = datetime.now(timezone.utc) + timedelta(days=2)
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 0
    err = capsys.readouterr().err
    assert "warning: agent authorization expires" in err


def test_live_cap_below_exchange_minimum_exits_1(tmp_path, capsys, live_seams):
    # §5 rule 4: equity 10 -> effective cap 6 USDC < the 10 USDC exchange
    # minimum — the run could never trade, so startup fails.
    live_seams.equity = D(10)
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "minimum order value" in err
    # Collect-all: the signed transport was still proven in the same pass.
    assert live_seams.health_calls == ["testnet"]


def test_live_account_read_failure_exits_1(tmp_path, capsys, live_seams):
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.account_error = ExchangeRequestError("boom")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    assert "account read failed" in capsys.readouterr().err


def test_live_unusable_account_snapshot_exits_1(tmp_path, capsys, live_seams):
    # AccountSnapshot.__post_init__ raises a bare ValueError when
    # account_value <= 0 (margin-called / empty account) — that must stay a
    # named exit-1 startup failure with an actionable message, never fall
    # through to the generic exit-2 crash handler.
    live_seams.account_error = ValueError("AccountSnapshot.account_value must be > 0, got 0")
    rc = cli_main(["live", "--config", str(_live_yaml(tmp_path))])
    assert rc == 1
    assert "account snapshot unusable" in capsys.readouterr().err


def test_live_top_level_network_mismatch_warns(tmp_path, capsys, live_seams):
    # A top-level network: that disagrees with live.network is legal (paper
    # reads mainnet data while live drills on testnet) but must be said aloud.
    path = _live_yaml(tmp_path, top_level_network="mainnet")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ignores the top-level network" in err
    assert live_seams.client_networks == ["testnet"]  # live.network won


def test_live_matching_top_level_network_does_not_warn(tmp_path, capsys, live_seams):
    # Mixed case: load_config stores the top-level key raw, so the comparison
    # must normalise or an equal pair would warn spuriously.
    path = _live_yaml(tmp_path, top_level_network="TestNet")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 0
    assert "ignores the top-level network" not in capsys.readouterr().err


# --------------------------------------------------------------------------
# live-smoke + validate (live) — PR 6
# --------------------------------------------------------------------------


def _make_live_run(
    tmp_path,
    *,
    mode="testnet_live",
    run_id="live-BTC",
    db_name="live_trading.db",
    coin="BTC",
    config_json=None,
):
    from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

    if config_json is None:
        # The identity fields a real `live --create` genesis always records —
        # the live-smoke drift check (2026-07-28) reads coin + live.network.
        config_json = json.dumps(
            {
                "coin": coin,
                "live": {
                    "mode": mode,
                    "network": "testnet" if mode == "testnet_live" else "mainnet",
                },
            }
        )
    db = Database(tmp_path / db_name)
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=Decimal(200),
        schema_version=SCHEMA_VERSION,
        config_json=config_json,
    )
    db.close()
    return tmp_path / db_name


def test_validate_dispatches_live_run(tmp_path, capsys):
    dbp = _make_live_run(tmp_path)
    rc = cli_main(["validate", "--run-id", "live-BTC", "--db", str(dbp)])
    out = capsys.readouterr().out
    assert "execution_mode: testnet_live" in out
    # A freshly-initialized live run is internally consistent (clean replay) but
    # short of the gate (0 cycles, no smoke) → exit 4 "keep running", not 5.
    assert rc == 4
    assert "shortfall:" in out


def test_validate_live_missing_run_exits_1(tmp_path, capsys):
    dbp = _make_live_run(tmp_path)
    rc = cli_main(["validate", "--run-id", "nope", "--db", str(dbp)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_live_smoke_gate_status_reports_not_passed(tmp_path, capsys):
    dbp = _make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    out = capsys.readouterr().out
    assert "smoke_gate_passed: no" in out
    assert rc == 4


def test_live_smoke_gate_status_nonexistent_run_exits_1(tmp_path, capsys):
    dbp = _make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "nope", "--db", str(dbp), "--gate-status"])
    assert rc == 1


def test_live_smoke_bad_only_key_exits_1(tmp_path, capsys):
    dbp = _make_live_run(tmp_path)
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--only", "bogus"])
    assert rc == 1
    assert "unknown smoke test key" in capsys.readouterr().err


def test_live_smoke_dry_run_records_skipped(tmp_path, capsys):
    # Dry-run needs a valid live config + the run to exist; it touches no network.
    # Genesis matches the config (the drift identity check runs before the
    # dry-run fork, 2026-07-28).
    cfg = _live_yaml(tmp_path)
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="live-BTC")
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
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    dbp = _make_live_run(tmp_path, mode="mainnet_tiny")
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "only against a testnet_live run" in err
    assert "mainnet_tiny" in err


def test_live_smoke_refuses_mainnet_even_with_dry_run(tmp_path, capsys):
    # The guard sits before the dry-run branch: a mainnet config is refused even
    # for the offline wiring check, so a green dry-run can never lull an operator.
    cfg = _live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    dbp = _make_live_run(tmp_path, mode="mainnet_tiny")
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
    cfg = _live_yaml(tmp_path)  # testnet_live / testnet
    dbp = _make_live_run(tmp_path, mode="mainnet_tiny")  # genesis network=mainnet
    rc = cli_main(
        ["live-smoke", "--config", str(cfg), "--run-id", "live-BTC", "--db", str(dbp), "--dry-run"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "live.network" in err
    assert "new --run-id" in err


def test_live_smoke_refuses_run_created_for_another_coin(tmp_path, capsys):
    cfg = _live_yaml(tmp_path)  # allowed_symbols → BTC
    dbp = _make_live_run(tmp_path, coin="ETH")
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
    dbp = _make_live_run(tmp_path, mode="mainnet_tiny")
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "§21.3" in captured.err
    assert "smoke_gate_passed" not in captured.out


def test_live_smoke_gate_status_refuses_unknown_genesis_mode(tmp_path, capsys):
    # A hand-built run whose genesis names no live.mode reads as "unknown" —
    # fail-safe refusal, same as any non-testnet mode.
    dbp = _make_live_run(tmp_path, config_json="{}")
    rc = cli_main(["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--gate-status"])
    assert rc == 1
    assert "'unknown'" in capsys.readouterr().err


def test_live_smoke_gate_status_refuses_only_and_dry_run(tmp_path, capsys):
    # --gate-status is a pure store read: combining it with an action flag is
    # ambiguous operator intent (read the gate, or run/skip tests?) — refused
    # by name before anything touches the store.
    dbp = _make_live_run(tmp_path)
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
    dbp = _make_live_run(tmp_path)
    rc = cli_main(
        ["live-smoke", "--run-id", "live-BTC", "--db", str(dbp), "--only", "slice_order_status"]
    )
    assert rc == 1
    assert "select both" in capsys.readouterr().err


# -- live --loop §20.2 gate consumer + live-smoke lease (2026-07-27) --------


def _seed_live_run_with_genesis_subset(
    tmp_path, cfg_path, *, run_id="r1", db_name="live_trading.db"
):
    """A live run whose genesis config_json matches what --create would record,
    so a later ``live --run-id`` restart passes the drift check offline."""
    from contrib.hyperliquid_perp.config import load_config
    from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

    conf = load_config(str(cfg_path))
    subset = _run_config_subset(conf, "BTC")
    subset["live"] = conf["live"]
    dbp = tmp_path / db_name
    db = Database(dbp)
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=Decimal(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps(subset, ensure_ascii=False, default=str),
    )
    db.close()
    return dbp


def test_live_loop_refuses_testnet_run_until_smoke_passes(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The gate CONSUMER itself (review 2026-07-27): a testnet_live restart with
    # no passing smoke rows must be refused --loop by name, before the run lock
    # or the kill switch is touched.
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    # Exit 4, not 1 (decision 2026-07-29): "the gate is not open" is the same
    # not-yet-at-the-gate fact live-smoke itself reports as 4 — a supervisor
    # must be able to tell it from a config/auth failure's exit 1.
    assert rc == 4
    err = capsys.readouterr().err
    assert "§20.2 smoke suite" in err
    assert "not yet run" in err


def test_live_loop_open_smoke_gate_proceeds_past_the_gate(
    tmp_path, capsys, live_seams, monkeypatch
):
    # With all 18 smoke rows passed the gate opens: --loop prints the
    # oldest-pass age line and moves on to the run lock (pre-held here, so the
    # command stops at the lease refusal — proof it got PAST the gate).
    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    db = Database(dbp)
    with db.transaction() as conn:
        for test in smoke_mod.SMOKE_TESTS:
            repo.insert_smoke_test_result(
                conn,
                run_id="r1",
                test_number=test.number,
                test_key=test.key,
                test_name=test.name,
                status="passed",
                network="testnet",
                executed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
    acquire_run_lock(db, "r1", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "smoke gate open" in err  # the age line printed
    assert "§20.2 smoke suite" not in err  # NOT the gate refusal
    assert "2026-07-20" in err  # names the oldest pass


def _open_smoke_gate(dbp, run_id="r1") -> None:
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    db = Database(dbp)
    with db.transaction() as conn:
        for test in smoke_mod.SMOKE_TESTS:
            repo.insert_smoke_test_result(
                conn,
                run_id=run_id,
                test_number=test.number,
                test_key=test.key,
                test_name=test.name,
                status="passed",
                network="testnet",
                executed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
    db.close()


def test_live_create_refused_by_a_sibling_leaves_no_half_created_run(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The refusal has to land BEFORE --create writes the run row. Refusing after
    # left a half-created run behind, and the operator's corrected re-run was
    # then rejected with "already exists — drop --create to resume it" — a
    # second, unrelated error for a run they never got to start.
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock
    from contrib.hyperliquid_perp.persistence import repository as repo_mod

    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="sibling")
    db = Database(dbp)
    acquire_run_lock(db, "sibling", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(
        ["live", "--config", str(cfg), "--run-id", "brand-new", "--db", str(dbp), "--create"]
    )
    assert rc == 1
    assert "ACCOUNT-wide" in capsys.readouterr().err
    # The run was never created, so the corrected re-run is a clean --create.
    with Database(dbp) as db:
        assert repo_mod.get_run(db.conn, "brand-new") is None


def test_live_loop_refuses_a_same_wallet_sibling_run(tmp_path, capsys, live_seams, monkeypatch):
    # The guard `live-smoke` has had since 2026-07-30, now on the path that runs
    # with REAL money. The run lease is per-run_id and both runs hold their own
    # quite happily, but this command arms and clears the ACCOUNT-wide
    # scheduleCancel and runs the §19.3 sweep, whose bot-ownership lookup carries
    # no run_id — so the two runs cancel each other's resting orders and
    # whichever shuts down first strips the other's dead-man cover.
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="sibling")
    _open_smoke_gate(dbp)
    db = Database(dbp)
    acquire_run_lock(db, "sibling", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ACCOUNT-wide" in err
    assert "sibling" in err
    # And it must not offer the remedy that does not work: the hazard is
    # per-wallet, so a separate store only hides the two runs from this check.
    assert "does NOT help" in err


def test_live_loop_smoke_gate_does_not_apply_to_a_mainnet_run(
    tmp_path, capsys, live_seams, monkeypatch
):
    """The gate's testnet_live SCOPING, which had no test (review 2026-07-30).

    §21.3 proves smoke on the separate testnet run, so a mainnet_tiny run's
    live_smoke_tests table is empty BY DESIGN. Drop the `mode is TESTNET_LIVE`
    clause from the gate and this run would find all 18 missing and exit 4
    forever — permanently unstartable, which is exactly why the scoping exists.
    The negative control is the testnet test above: same empty table, exit 4.
    """
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: mainnet_tiny\n  network: mainnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    db = Database(dbp)
    # Lease pre-held so the command stops at the lease refusal — proof it got
    # PAST the gate rather than being refused by it.
    acquire_run_lock(db, "r1", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1  # the lease, not the gate's exit 4
    err = capsys.readouterr().err
    assert "§20.2 smoke suite" not in err
    assert "not yet run" not in err


def test_live_loop_without_an_api_key_is_refused_up_front(
    tmp_path, capsys, live_seams, monkeypatch
):
    """Without a key every 4h cycle records api_failed, which never counts toward
    the §20.3 >=30-cycle gate — a real-money run could burn days producing nothing
    gateable. _cmd_paper always checked this; the live path did not (2026-07-30).
    """
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_live_without_loop_still_runs_keyless(tmp_path, capsys, live_seams, monkeypatch):
    """Control for the guard above: `live` without --loop never polls the AI.

    It arms, sweeps and exits, so it must stay keyless — the guard belongs to
    --loop alone. Stopped at the pre-held lease, well past the key check.
    """
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg)
    db = Database(dbp)
    acquire_run_lock(db, "r1", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1  # the lease
    assert "OPENROUTER_API_KEY" not in capsys.readouterr().err


def test_live_refuses_a_nonexistent_db_without_create(tmp_path, capsys, live_seams, monkeypatch):
    """Database() creates AND migrates, so a typo'd --db must not be opened.

    Without this guard the command left an empty migrated live store behind
    before failing on "run does not exist" — and with --create it would silently
    open a SECOND live ledger over the same real wallet (review 2026-07-30).
    """
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    missing = tmp_path / "typo.db"
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(missing)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()  # nothing was created


def test_the_daemon_writes_unmarked_rows(tmp_path, live_seams, monkeypatch):
    """The inverse of the smoke marking, pinned where the wiring actually is.

    ``suite_authored=True`` on the DAEMON's manager is one kwarg away, in the
    sibling constructor 1500 lines from the smoke one, and it left the whole
    suite green: a unit test on the manager's default cannot see what the CLI
    passes. In production it would make every real refresh suite-authored, so
    ``refreshed`` stays 0, the §20.3 floor is never reached, ``live_ready`` can
    never be true — and the operator is told the run's refreshes "were written
    during live-smoke" (2026-08-01 round-17 mutation probe).
    """
    import contextlib

    from contrib.hyperliquid_perp.live import kill_switch as ks_mod

    seen: list[dict] = []
    real = ks_mod.KillSwitchManager

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            seen.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(ks_mod, "KillSwitchManager", _Recording)
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    # The recovery is NOT driven to completion, and that is deliberate rather than
    # papered over: ``live_seams``' signed double stops short of the fill-backfill
    # surface a full §19.1 pass needs. The fact under test is settled before then
    # — what the CLI hands the constructor — and ``assert seen`` fails loudly if
    # construction ever stops being reached.
    with contextlib.suppress(Exception):
        cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert seen, "no KillSwitchManager was constructed — the pin proves nothing"
    assert all(kwargs.get("suite_authored", False) is False for kwargs in seen), seen
    # The other term this call site carries, and the reason round 17 had to make
    # ``_FakeSigned.timeout`` faithful in the first place: forcing it to None
    # here left the whole suite green, so the daemon's copy of the §18.2 timing
    # budget could lose its failed-attempt cost undetected. The smoke sibling
    # pins the same value; this one asserted only the marker
    # (2026-08-01 round-18 mutation probe).
    assert all(kwargs.get("network_timeout_s") == 8 for kwargs in seen), seen


def test_the_daemon_hands_the_fill_processor_the_signed_wallet(tmp_path, live_seams, monkeypatch):
    """The envelope-identity check is armed by wiring, pinned at the DAEMON site.

    ``LiveFillProcessor.wallet_address`` is optional and skips the check when
    left at None, so the cli call sites are the load-bearing part. The sibling
    pin in test_live_smoke_cli.py drives ``live-smoke`` and therefore reaches
    only the smoke constructor; deleting the kwarg from the DAEMON constructor
    — the processor handed to the live loop and to FillBackfiller, the one that
    ingests real userFills for weeks — left the whole suite green
    (2026-08-17 identity-echo mutation probe). Same shape, and same reason, as
    the suite_authored pin above.
    """
    import contextlib

    # cli.py imports the class lazily inside the command function, so the seam
    # is the SOURCE module, not a cli attribute.
    from contrib.hyperliquid_perp.live import fills as fills_mod

    seen: list[object] = []
    real = fills_mod.LiveFillProcessor

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            seen.append(kwargs.get("wallet_address"))
            super().__init__(**kwargs)

    monkeypatch.setattr(fills_mod, "LiveFillProcessor", _Recording)
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    # Recovery is not driven to completion, for the reason the sibling pin above
    # states: the fact under test is settled at construction, and ``assert seen``
    # fails loudly if construction stops being reached.
    with contextlib.suppress(Exception):
        cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert seen, "no LiveFillProcessor was constructed - the pin proves nothing"
    assert all(value == _LIVE_WALLET for value in seen), seen


def _drive_the_daemon_recovery(tmp_path, monkeypatch):
    """Run ``live --run-id`` far enough to build the recovery components.

    Stops inside ``run_startup_recovery``: arming the switch calls
    ``schedule_cancel``, which the ``live_seams`` double does not have, and
    ``_live_startup_recovery`` reports that as the exit-1 "startup recovery
    failed" path. Everything these pins assert is settled before then — and the
    exit code is asserted rather than suppressed so that "the drive still gets
    there" stays observable rather than assumed.
    """
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = _seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1  # the un-armable switch, well past the constructions under test
    return dbp


def test_the_daemon_wires_the_reconciliation_sweeps_switch_refresh(
    tmp_path, live_seams, monkeypatch
):
    """§18.2: the daemon's two sweep components refresh the dead man's switch.

    ``FillBackfiller.refresh_kill_switch`` and ``LiveReconciler.refresh_kill_switch``
    both default to None, and None makes the reconciler's ``_refresh_deadline()``
    and the backfiller's per-page refresh no-ops — so a paged backfill or a
    per-order orderStatus sweep holds the single-threaded tick for its whole
    length with no refresh, the scheduled cancel lapses, and every resting SL/TP
    on the wallet is cancelled with the process still alive and mid-reconcile.

    Both are passed HERE and were pinned nowhere: the constructors' own comments
    ("both real construction sites pass this") were the only thing asserting it,
    and a comment of that exact shape was already wrong once (2026-07-31). Same
    wiring-pin shape as the two siblings above (issue #45).
    """
    record = record_reconciliation_sweep_wiring(monkeypatch)
    _drive_the_daemon_recovery(tmp_path, monkeypatch)

    assert record.switches, "no recovery ran — the pin proves nothing"
    _assert_paired_sweep_refreshes(record, owner="daemon")
    # The double has no REST behaviour; reaching it would mean the drive ran
    # past the constructions under test into work these pins do not model.
    assert live_seams.rest_calls == []


def test_the_daemon_gives_the_reconciler_a_payload_dir(tmp_path, live_seams, monkeypatch):
    """``LiveReconciler.payload_dir`` defaults to None, which drops the evidence.

    Not a §18.2 safety check but the §19.1 audit trail: without it the raw
    clearinghouse payload behind every reconciliation verdict is never written,
    silently, so an operator reconstructing a disputed sweep has the verdict and
    nothing under it. Separate from the refresh pin above because it is a
    separate fact about the same call site — a failure should name the evidence,
    not the dead man's switch.
    """
    record = record_reconciliation_sweep_wiring(monkeypatch)
    dbp = _drive_the_daemon_recovery(tmp_path, monkeypatch)

    assert record.reconcilers, "no LiveReconciler was constructed — the pin proves nothing"
    for reconciler in record.reconcilers:
        _assert_payload_dir(reconciler, dbp, run_id="r1")


class _StopBeforeTheLoop(Exception):
    """Sentinel: every kwarg the pins below assert is already decided."""


def _drive_live_loop_construction(tmp_path, monkeypatch, *, fetch_clearinghouse):
    """Build ``_run_live_loop``'s components and stop; return what it built with.

    The loop BODY needs a whole live session — a real §19.1 pass the offline
    doubles cannot produce — which is why its sibling invariant in
    test_kill_switch.py is checked against the SOURCE. The construction block is
    a different matter: it is straight-line, it is where the three safety kwargs
    below are decided, and it can simply be driven. The drive stops at
    ``_EngineDecisionProvider``, the last of the three; the engine, worker and
    driver built around it are the loop's own machinery and are not modelled
    here.

    The recorders subclass the real classes and construct THROUGH them wherever
    the real constructor runs offline, so a call site that drifts from a
    signature dies here instead of being recorded as fine. The exception is
    ``_EngineDecisionProvider``, whose ``__init__`` builds a whole engine
    config: its arguments are bound against the real signature instead.
    """
    import inspect

    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
    from contrib.hyperliquid_perp.exchanges.hyperliquid import market_data as md_mod
    from contrib.hyperliquid_perp.live import loss_guards as lg_mod, protection as prot_mod
    from contrib.hyperliquid_perp.live.config import LiveConfig
    from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    class _FakeMarket:
        def __init__(self, _client):
            pass

        def get_asset_meta(self, coin):
            return 3, MarginSchedule(coin=coin, tiers=(MarginTier(D(0), D(50)),))

    class _FakeSwitch:
        """The refresh surface ``refresh_across_blocking_work`` reaches for."""

        def __init__(self):
            self.ticks = 0

        def tick(self):
            self.ticks += 1

    built = SimpleNamespace(protection=None, guards=None, provider=None, kill_switch=_FakeSwitch())

    def _recorder(module, name, field):
        real = getattr(module, name)

        class _Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                setattr(built, field, kwargs)

        monkeypatch.setattr(module, name, _Recording)

    real_provider = cli_mod._EngineDecisionProvider

    class _RecordingProvider:
        def __init__(self, *args, **kwargs):
            # Bound, not constructed: the real __init__ builds a whole engine
            # config. Binding still fails a call site that drifts from the
            # signature, which is the failure the other recorders get from
            # constructing through the real class.
            inspect.signature(real_provider).bind(*args, **kwargs)
            built.provider = kwargs
            raise _StopBeforeTheLoop

    monkeypatch.setattr(md_mod, "HyperliquidMarketData", _FakeMarket)
    monkeypatch.setattr(cli_mod, "_EngineDecisionProvider", _RecordingProvider)
    _recorder(prot_mod, "ProtectionManager", "protection")
    _recorder(lg_mod, "LossGuards", "guards")

    dbp = tmp_path / "live.db"
    db = Database(dbp)
    accounting.initialize_run(
        db, run_id="r1", mode="live", initial_balance_usdc=D(200), schema_version=SCHEMA_VERSION
    )
    live_cfg = LiveConfig.from_dict(
        {
            "mode": "testnet_live",
            "network": "testnet",
            "safety": {
                "allowed_symbols": ["BTC"],
                "leverage": 1,
                "max_target_margin_pct": 60,
                "max_notional_usdc": "500",
                "absolute_notional_ceiling": "1000",
            },
        }
    )
    gate = RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
        state_reconciled=True,
    )
    try:
        with pytest.raises(_StopBeforeTheLoop):
            cli_mod._run_live_loop(
                cfgs=(RiskConfig(leverage=D(1), max_target_margin_pct=60), DecisionConfig()),
                db=db,
                run_id="r1",
                coin="BTC",
                config={},
                live_cfg=live_cfg,
                client=SimpleNamespace(),
                signed=SimpleNamespace(open_orders=lambda: []),
                gate=gate,
                kill_switch=built.kill_switch,
                safe_mode=SafeModeManager(db=db, run_id="r1", gate=gate),
                reconciler=SimpleNamespace(),
                processor=SimpleNamespace(),
                payload_dir=tmp_path / "payloads",
                fetch_clearinghouse=fetch_clearinghouse,
            )
    finally:
        db.close()
    for field in ("protection", "guards", "provider"):
        assert getattr(built, field) is not None, f"no {field} built — the pin proves nothing"
    return built


def test_the_live_loop_hands_protection_the_kill_switch(tmp_path, monkeypatch):
    """§18.2: SL/TP repair refreshes the switch across its own retry delays.

    ``ProtectionManager.kill_switch`` defaults to None, and None turns all six
    ``refresh_across_blocking_work`` calls in the manager into no-ops — the four
    on the repair ladder plus the orderStatus confirmation read and the
    protection cancel — and disables the firing latch with them. That leaves one
    of the longest blocking episodes on the tick (a repair with synchronous
    delays between attempts) as the one place the dead man's switch can self-trip
    and cancel the very SL being repaired. Only this call site wires it, and
    nothing observed this call site (2026-08-17 issue #45).
    """
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: _clearinghouse()
    )
    assert built.protection.get("kill_switch") is built.kill_switch


def test_the_live_loop_takes_the_day_baseline_from_the_clearinghouse(tmp_path, monkeypatch):
    """§10.3 rule 1: the UTC-day baseline is READ, not taken from the ledger.

    ``LossGuards.day_baseline_source`` defaults to None, which silently swaps the
    baseline for the LOCAL ledger's equity — no error, no log — and every
    drawdown and daily-loss judgement for that whole day is anchored to the wrong
    number. The comment at the call site records the last time this path died
    quietly (an arity mismatch its own except-Exception ate, 2026-08-01); what it
    could not record is that the wiring was still unpinned.

    Asserted by CALLING it, for the reason that history gives: a source-level or
    not-None check passes on a source that returns the ledger. The pin covers the
    hop it can see — the source is bound to the ``fetch_clearinghouse`` this
    function was handed; that THAT callable reads the exchange is a fact about
    the caller, pinned where the caller is.
    """
    reads: list[str] = []

    def _fetch():
        reads.append("clearinghouse")
        return _clearinghouse(account_value="4242")

    built = _drive_live_loop_construction(tmp_path, monkeypatch, fetch_clearinghouse=_fetch)
    source = built.guards.get("day_baseline_source")
    assert source is not None, "the day baseline silently fell back to the local ledger"
    # 4242 is what the handed-in read returns; the seeded ledger is 200, so a
    # source bound to the local ledger cannot produce it.
    assert source() == D("4242")
    assert reads == ["clearinghouse"]
    # The same call is a bare full-timeout REST read on the single-threaded tick,
    # so it refreshes across itself. Counted loosely: refreshing MORE than once
    # would be a safer loop, not a regression.
    assert built.kill_switch.ticks >= 1


def test_the_live_loop_refreshes_across_the_decision_cycles_market_reads(tmp_path, monkeypatch):
    """§18.2: the longest REST chain in the system refreshes between its reads.

    ``_EngineDecisionProvider.on_blocking_read`` defaults to None, and
    ``_build_context``'s ``_between_reads`` simply returns when it is — so the
    four back-to-back full-timeout reads of a decision cycle run entirely
    unrefreshed. That chain is what ``_MAX_UNREFRESHED_REST_CALLS`` is reasoned
    about against; unwired, the switch's real exposure is the chain's length
    while the operator advisory is still computed from the submit chain's 3.

    Same shape as the four kwargs issue #45 lists, and found by scanning for that
    shape — the issue does not name this site. ``_build_context``'s own refresh
    behaviour is driven in test_kill_switch.py; pinned here is that the live loop
    hands it a hook at all, and that the hook drives THIS run's switch.
    """
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: _clearinghouse()
    )
    hook = built.provider.get("on_blocking_read")
    assert hook is not None, "a whole decision cycle of market reads refreshes nothing (§18.2)"
    before = built.kill_switch.ticks
    hook()
    assert built.kill_switch.ticks == before + 1


def test_live_refuses_a_timeout_that_cannot_fit_the_kill_switch_budget(
    tmp_path, capsys, live_seams, monkeypatch
):
    """The §18.2 timing preflight's exit-1 path, at the CLI, over a real config.

    Only the pure function was tested, so nothing observed that the CLI reads the
    timeout off the client at all — and the fake client under-reported it as
    None, which silently dropped a term and let these tests reach assertions the
    same config cannot reach in production. This is the end-to-end pin: the
    top-level 30s DEFAULT is deliberately illegal in live
    (30 + 30 + 30 + 15 + 30 = 135 >= 120) and must be refused by name before the
    run lock is taken (2026-08-01 round-16 review).
    """
    monkeypatch.setenv(_LIVE_ENV, _LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = _live_yaml(
        tmp_path,
        network_timeout_s=None,  # omitted -> the client resolves the 30s default
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(tmp_path / "l.db")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot be refreshed in time" in err
    # The message names the knob the operator can actually change.
    assert "network_timeout_s" in err


def test_live_smoke_real_run_requires_the_run_lease(tmp_path, capsys, monkeypatch):
    # live-smoke places real orders and runs recoveries — the same actions the
    # run lease keeps single-owner. A held lease must refuse the suite.
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    dbp = _make_live_run(tmp_path)
    db = Database(dbp)
    acquire_run_lock(db, "live-BTC", pid=999999, now=datetime.now(timezone.utc))
    db.close()
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    assert rc == 1
    assert "places real orders" in capsys.readouterr().err


def test_live_smoke_preflight_failure_exits_4_and_releases_the_lease(tmp_path, capsys, monkeypatch):
    # A pre-flight recovery failure aborts the suite: exit 4, the error named on
    # stderr, and the lease released so the operator can immediately retry.
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.paper.run_lock import acquire_run_lock

    dbp = _make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
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
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.live import smoke as smoke_mod
    from contrib.hyperliquid_perp.paper.run_lock import RunLockError

    dbp = _make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
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
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = _make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
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
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = _make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
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
    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.live import smoke as smoke_mod

    dbp = _make_live_run(tmp_path)
    monkeypatch.setattr(
        cli_mod, "_build_smoke_session", lambda args, db: SimpleNamespace(dry_run=False)
    )
    monkeypatch.setattr(smoke_mod.SmokeTestRunner, "run", lambda self, *, only=None: [])
    rc = cli_main(
        ["live-smoke", "--config", "unused.yaml", "--run-id", "live-BTC", "--db", str(dbp)]
    )
    err = capsys.readouterr().err
    assert rc == 4
    assert "staging position may still be OPEN" not in err


def test_the_prompt_version_is_pinned_to_the_block_it_versions():
    """The version stamp and the text it versions must move together.

    RUNBOOK §4's A/B exception deliberately lets one run straddle a
    prompt-only deploy and segments the before/after populations on
    ``ai_inputs.prompt_version`` — which makes this stamp the ONLY thing
    separating them. It lives in ``cli.py`` while the text it versions lives in
    ``domains/perp/target_decision.py``; ``cli.py`` does import that function,
    but an import is not a coupling — nothing makes the constant track the
    text, no assertion relates them, and nothing else in the suite references
    the constant. So a prompt edit that forgot the bump would merge the two
    populations into one bucket and the merge would be invisible in the data:
    the query still returns a clean two-value split.

    The digest covers the block as rendered from ``DecisionConfig()``, so a
    changed config DEFAULT trips it too. That is the intended reading rather
    than a false positive: the deployed prompt text really did change, and the
    RUNBOOK already requires a code-default change to ship with a fresh run-id.

    If this fails because you changed the prompt on purpose: bump
    ``PROMPT_VERSION`` to a value that has never been used before (rollbacks
    included — see the RUNBOOK), then update the digest here.
    """
    import hashlib

    from contrib.hyperliquid_perp import cli as _cli
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        DecisionConfig,
        decision_format_instructions,
    )

    block = decision_format_instructions(DecisionConfig())
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]
    # Compared as one tuple so a mismatch shows both halves at once — which one
    # drifted is the whole diagnosis.
    assert (_cli.PROMPT_VERSION, digest) == ("phase2-target-v3", "97aa0feaa4496d6f")
