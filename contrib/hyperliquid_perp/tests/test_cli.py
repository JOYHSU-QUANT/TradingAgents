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


def test_sigterm_shim_raises_keyboard_interrupt():
    # systemd/docker stop with SIGTERM; the handler must funnel it into the
    # KeyboardInterrupt shutdown-export path.
    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(signal.SIGTERM, None)
