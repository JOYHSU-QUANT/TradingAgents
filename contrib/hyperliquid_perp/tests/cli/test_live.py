"""Tests for the ``live`` subcommand: its gate check, startup recovery and exit."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import _common as common_mod, live as live_mod, main as cli_main
from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database, connect
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting

from ..conftest import (
    assert_paired_sweep_refreshes,
    assert_payload_dir,
    doc_text,
    identity_latch_rows,
    misrouted_order_status,
    record_constructor_kwargs,
    record_reconciliation_sweep_wiring,
)
from ..fakes.payloads import btc_position, clearinghouse
from .conftest import (
    BEHIND_VERSIONS,
    LIVE_ENV,
    LIVE_KEY,
    LIVE_WALLET,
    build_behind,
    build_one_behind,
    live_yaml,
    seed_live_run_with_genesis_subset,
    stored_version,
)

D = Decimal


def test_the_runbook_quotes_the_migration_window_literals():
    # Issue #147: the §8 rows for the lease floor and pid recycling restate
    # three code facts as bare literals — the floor version, the lease
    # staleness window, and the suffix `live` appends to its lease refusal.
    # Same criterion as test_smoke's RUNBOOK pins: a value the code enforces,
    # restated by the doc as a literal, with nothing tying the two.
    from contrib.hyperliquid_perp.persistence.schema import LEASE_READABLE_SINCE
    from contrib.hyperliquid_perp.runtime.run_lock import LOCK_STALE_SECONDS

    runbook = doc_text("RUNBOOK-live.md")
    assert f"`LOCK_STALE_SECONDS`＝{LOCK_STALE_SECONDS} 秒" in runbook
    assert f"before upgrading a store arrived in v{LEASE_READABLE_SINCE}`" in runbook
    # The suffix the pid-recycling row keys on, as the live lease-conflict test
    # further down asserts the CLI prints it.
    assert "`(this process is pid N)`" in runbook


def test_live_missing_live_block_exits_1(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path, live_lines=None))])
    assert rc == 1
    assert "no live: block" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--create", "--adopt-positions"])
def test_live_create_flags_without_run_id_are_rejected_by_name(tmp_path, capsys, live_seams, flag):
    # Without --run-id the command is config-check mode: it creates and seeds
    # nothing, so these flags had no effect and were silently ignored — an
    # operator would read the "gates OK" exit 0 as "run created". Named
    # rejection, the same discipline the resume/safe-mode flag guards use.
    cfg = live_yaml(tmp_path)
    rc = cli_main(["live", "--config", str(cfg), flag])
    assert rc == 1
    assert "require --run-id" in capsys.readouterr().err


def test_live_paper_mode_exits_1(tmp_path, capsys, live_seams):
    cfg = live_yaml(tmp_path, live_lines="  mode: paper\n  network: testnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "paper subcommand" in capsys.readouterr().err


def test_live_missing_network_exits_1(tmp_path, capsys, live_seams):
    # live.network is required (no guessed default to blame the operator for).
    cfg = live_yaml(tmp_path, live_lines="  mode: testnet_live\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "live.network is required" in capsys.readouterr().err


def test_live_mainnet_live_mode_exits_1(tmp_path, capsys, live_seams):
    # The §22 hard rejection must surface as the named config exit, not a crash.
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_live\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid live: config" in err
    assert "mainnet_live" in err


def test_live_missing_wallet_exits_1(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path, wallet=None))])
    assert rc == 1
    assert "wallet_address" in capsys.readouterr().err


def test_live_missing_key_with_require_exits_1(tmp_path, capsys, live_seams, monkeypatch):
    # require_agent_wallet defaults true: no key -> named startup refusal that
    # names the exact env var for this network.
    monkeypatch.delenv(LIVE_ENV, raising=False)
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert LIVE_ENV in err
    assert "require_agent_wallet" in err
    assert live_seams.auth_calls == []


def test_live_real_orders_without_required_wallet_is_a_config_error(
    tmp_path, capsys, live_seams, monkeypatch
):
    # §6 rule 7: allow_real_orders: true + require_agent_wallet: false is a
    # construction-time contradiction — rejected at config load, before any
    # env-var lookup, so arming can never depend on environment presence.
    monkeypatch.delenv(LIVE_ENV, raising=False)
    cfg = live_yaml(
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
    monkeypatch.delenv(LIVE_ENV, raising=False)
    cfg = live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    err = capsys.readouterr().err
    assert LIVE_ENV in err
    assert "allow_real_orders" in err
    assert "rule 6" in err
    assert live_seams.auth_calls == []
    assert live_seams.health_calls == []


def test_live_keyless_gate_check_with_orders_off_passes(tmp_path, capsys, live_seams, monkeypatch):
    # The legitimate keyless lane: orders off + not required -> gate check
    # runs without authorization or a signed client.
    monkeypatch.delenv(LIVE_ENV, raising=False)
    cfg = live_yaml(
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
    cfg = live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "allow_real_orders: true" in out
    assert "forced" not in out
    assert live_seams.auth_calls == [LIVE_WALLET]
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
    assert LIVE_KEY not in out
    assert LIVE_KEY not in err


def test_live_missing_risk_block_exits_1(tmp_path, capsys, live_seams):
    # §24: the risk↔live cross-check compares two blocks the operator WROTE;
    # no risk: block -> named exit 1, never a vacuous pass on defaults.
    cfg = live_yaml(tmp_path, risk_lines=None)
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "no risk: block" in capsys.readouterr().err
    assert live_seams.auth_calls == []


def test_live_client_construction_failure_exits_1(tmp_path, capsys, live_seams):
    # The first network touch: a construction-time failure (DNS, bad SDK) must
    # be a named exit 1 with NO downstream gate having run.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.client_error = ExchangeRequestError("Hyperliquid request failed: boom")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
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
    monkeypatch.delenv(LIVE_ENV, raising=False)
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", LIVE_KEY)
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "mode: mainnet_tiny" in out
    assert "network: mainnet" in out
    # Default caps at equity 1000: pct 600, min(600, 100) = 100.
    assert "effective_notional_cap: 100 USDC" in out
    assert live_seams.client_networks == ["mainnet"]
    assert live_seams.health_calls == ["mainnet"]
    assert live_seams.auth_calls == [LIVE_WALLET]
    assert LIVE_KEY not in out
    assert LIVE_KEY not in err


def test_live_mainnet_tiny_missing_mainnet_key_names_mainnet_var(
    tmp_path, capsys, live_seams, monkeypatch
):
    # A testnet key alone must NOT satisfy a mainnet_tiny run — the error
    # names the mainnet variable specifically (the split-env-var rationale).
    monkeypatch.delenv("HYPERLIQUID_AGENT_KEY_MAINNET", raising=False)
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 1
    assert "HYPERLIQUID_AGENT_KEY_MAINNET" in capsys.readouterr().err
    assert live_seams.auth_calls == []


def test_live_agent_key_error_diagnoses_the_networks_own_env_var(
    tmp_path, capsys, live_seams, monkeypatch
):
    """The ``dotenv_diagnosis`` interpolation on the live agent-key refusal.

    Two sibling paths already pin this with a sentinel (``_require_api_key`` and
    the paper protection-only message); the live one did not, and its plain
    substring assertions cannot see the gap: the message's own
    ``f"error: {env_var} is not set"`` prefix already satisfies "the env var
    appears in stderr", so dropping the diagnosis call — or diagnosing the WRONG
    variable — stayed invisible (issue #76).

    Run on MAINNET_TINY deliberately: the diagnosis argument is a variable, so
    the failure worth catching is it being computed for the other network. A
    testnet run cannot distinguish that from correct behaviour.
    """
    mainnet_env = "HYPERLIQUID_AGENT_KEY_MAINNET"
    monkeypatch.delenv(mainnet_env, raising=False)
    # The message is printed by _common._require_agent_key (issue #126), so
    # patch THAT module's binding (each importer holds its own module-global,
    # per the from-import style).
    monkeypatch.setattr(common_mod, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")

    rc = cli_main(["live", "--config", str(cfg)])

    assert rc == 1
    err = capsys.readouterr().err
    assert mainnet_env in err
    # The interpolation exists AND was handed the right variable. LIVE_ENV is
    # the testnet one, still exported by the fixture — so a diagnosis computed
    # off the wrong network would show up as DIAG[...TESTNET] here.
    assert f"DIAG[{mainnet_env}]" in err
    assert f"DIAG[{LIVE_ENV}]" not in err
    assert live_seams.auth_calls == []  # refused before any network work


def test_live_mainnet_tiny_collects_all_gate_failures(tmp_path, capsys, live_seams, monkeypatch):
    # The collect-all contract holds on the real-money mode too.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    monkeypatch.delenv(LIVE_ENV, raising=False)
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", LIVE_KEY)
    live_seams.auth_error = AgentAuthorizationError("not approved")
    live_seams.equity = D(10)  # effective cap 6 < 10 USDC exchange minimum
    live_seams.signed_error = ExchangeRequestError("signed transport down")
    cfg = live_yaml(tmp_path, live_lines="  mode: mainnet_tiny\n  network: mainnet\n")
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
    path = live_yaml(
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
    path = live_yaml(tmp_path)
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
    path = live_yaml(tmp_path, risk_lines="  leverage: 1\n")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "must explicitly write" in err
    assert "margin_mode" in err
    assert "max_target_margin_pct" in err
    assert live_seams.auth_calls == []  # rejected before any network work


def test_live_happy_path_prints_caps_and_exits_0(tmp_path, capsys, live_seams):
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
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
    assert live_seams.auth_calls == [LIVE_WALLET]
    assert live_seams.client_networks == ["testnet"]
    assert live_seams.snapshot_requests == [LIVE_WALLET]
    # The signed transport was proven end-to-end (init + health check).
    assert live_seams.health_calls == ["testnet"]
    assert "gates OK" in err
    # Secret hygiene: the raw agent key must never reach the assembled output.
    assert LIVE_KEY not in out
    assert LIVE_KEY not in err


def test_live_auth_failure_exits_1(tmp_path, capsys, live_seams):
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    live_seams.auth_error = AgentAuthorizationError("agent 0xcc... is not in wallet list")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    # Collect-all: the independent gates still ran so the operator sees every
    # failure in one pass, then the run exits 1.
    assert live_seams.snapshot_requests == [LIVE_WALLET]
    assert live_seams.health_calls == ["testnet"]


def test_live_auth_network_failure_exits_1(tmp_path, capsys, live_seams):
    # A network/SDK failure during the §6.1 check must ride the SAME named
    # exit-1 lane as a rejection, not fall into the generic exit-2 bucket.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.auth_error = ExchangeRequestError("Hyperliquid request failed: boom")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    assert "agent authorization failed" in capsys.readouterr().err


def test_live_signed_health_check_failure_exits_1(tmp_path, capsys, live_seams):
    # The one place PR 1 proves the signed transport: a health-check failure
    # must be a named exit 1, never "gates OK".
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.signed_error = ExchangeRequestError("signed transport down")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    out, err = capsys.readouterr()
    assert "signed client health check failed" in err
    assert "gates OK" not in err
    assert "mode:" not in out  # no machine-readable success block on failure


def test_live_key_present_without_require_still_verifies(tmp_path, capsys, live_seams):
    # §6.1 runs whenever a key exists — require_agent_wallet: false must not
    # skip verification of a present (possibly wrong-network) key.
    cfg = live_yaml(
        tmp_path,
        live_lines=("  mode: testnet_live\n  network: testnet\n  require_agent_wallet: false\n"),
    )
    rc = cli_main(["live", "--config", str(cfg)])
    assert rc == 0
    assert live_seams.auth_calls == [LIVE_WALLET]
    assert live_seams.health_calls == ["testnet"]


def test_live_collects_all_gate_failures_in_one_pass(tmp_path, capsys, live_seams):
    # An operator with a bad approval AND an underfunded account AND a broken
    # signed transport sees all three named failures on one run.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
    from contrib.hyperliquid_perp.live.authorization import AgentAuthorizationError

    live_seams.auth_error = AgentAuthorizationError("not approved")
    live_seams.equity = D(10)  # effective cap 6 < 10 USDC exchange minimum
    live_seams.signed_error = ExchangeRequestError("signed transport down")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    assert "minimum order value" in err
    assert "signed client health check failed" in err


def test_live_near_expiry_authorization_warns_but_passes(tmp_path, capsys, live_seams):
    live_seams.auth_valid_until = datetime.now(timezone.utc) + timedelta(days=2)
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 0
    err = capsys.readouterr().err
    assert "warning: agent authorization expires" in err


def test_live_cap_below_exchange_minimum_exits_1(tmp_path, capsys, live_seams):
    # §5 rule 4: equity 10 -> effective cap 6 USDC < the 10 USDC exchange
    # minimum — the run could never trade, so startup fails.
    live_seams.equity = D(10)
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    err = capsys.readouterr().err
    assert "minimum order value" in err
    # Collect-all: the signed transport was still proven in the same pass.
    assert live_seams.health_calls == ["testnet"]


def test_live_account_read_failure_exits_1(tmp_path, capsys, live_seams):
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    live_seams.account_error = ExchangeRequestError("boom")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    assert "account read failed" in capsys.readouterr().err


def test_live_unusable_account_snapshot_exits_1(tmp_path, capsys, live_seams):
    # AccountSnapshot.__post_init__ raises a bare ValueError when
    # account_value <= 0 (margin-called / empty account) — that must stay a
    # named exit-1 startup failure with an actionable message, never fall
    # through to the generic exit-2 crash handler.
    live_seams.account_error = ValueError("AccountSnapshot.account_value must be > 0, got 0")
    rc = cli_main(["live", "--config", str(live_yaml(tmp_path))])
    assert rc == 1
    assert "account snapshot unusable" in capsys.readouterr().err


def test_live_top_level_network_mismatch_warns(tmp_path, capsys, live_seams):
    # A top-level network: that disagrees with live.network is legal (paper
    # reads mainnet data while live drills on testnet) but must be said aloud.
    path = live_yaml(tmp_path, top_level_network="mainnet")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ignores the top-level network" in err
    assert live_seams.client_networks == ["testnet"]  # live.network won


def test_live_matching_top_level_network_does_not_warn(tmp_path, capsys, live_seams):
    # Mixed case: load_config stores the top-level key raw, so the comparison
    # must normalise or an equal pair would warn spuriously.
    path = live_yaml(tmp_path, top_level_network="TestNet")
    rc = cli_main(["live", "--config", str(path)])
    assert rc == 0
    assert "ignores the top-level network" not in capsys.readouterr().err


def test_live_loop_refuses_testnet_run_until_smoke_passes(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The gate CONSUMER itself (review 2026-07-27): a testnet_live restart with
    # no passing smoke rows must be refused --loop by name, before the run lock
    # or the kill switch is touched.
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
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
    # oldest-pass age line and moves on to the run lock (made to refuse here,
    # so the command stops at the lease refusal — proof it got PAST the gate).
    # The refusal is scripted rather than a pre-held lease: since issue #129
    # a held lease is caught by the read-only peek at open, before the gate.
    from contrib.hyperliquid_perp.live import smoke_catalog
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    acquires: list[tuple[str, int]] = []

    def refuse(db, run_id, *, pid, now):
        acquires.append((run_id, pid))
        raise run_lock_mod.RunLockError("scripted lease refusal")

    monkeypatch.setattr(run_lock_mod, "acquire_run_lock", refuse)
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    db = Database(dbp)
    with db.transaction() as conn:
        for test in smoke_catalog.SMOKE_TESTS:
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
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "scripted lease refusal" in err  # stopped at the lock, not earlier
    assert acquires == [("r1", os.getpid())]  # the REAL acquire, for this run
    assert "smoke gate open" in err  # the age line printed
    assert "§20.2 smoke suite" not in err  # NOT the gate refusal
    assert "2026-07-20" in err  # names the oldest pass


def _latch_recoverable(db) -> None:
    """Latch recoverable safe mode on run ``r1``, as a tick error inside the loop would."""
    from contrib.hyperliquid_perp.live.safe_mode import (
        REASON_LIVE_TICK_ERROR,
        SAFE_MODE_RECOVERABLE,
        SafeModeManager,
    )

    SafeModeManager(db=db, run_id="r1", gate=None).enter(
        SAFE_MODE_RECOVERABLE, REASON_LIVE_TICK_ERROR, detail="scripted"
    )


def _drive_cmd_live_loop_to_its_exit(
    tmp_path,
    monkeypatch,
    *,
    loop,
    reconcile=lambda self, *a, **kw: None,
    one_shot=False,
    on_recovery=lambda **_: None,
):
    """``live --loop`` offline, up to and past ``_run_live_loop``'s call site.

    With ``one_shot`` the command runs without ``--loop``: ``loop`` is never
    called, the scripted recovery goes straight to the ``finally``, and that
    lane runs no §12.2 pre-shutdown reconcile. ``on_recovery`` runs inside
    the scripted recovery, after the session is built, with the recovery's
    kwargs.

    The smoke gate is seeded open, the §19.1 recovery is scripted as a pass
    (its real arming needs a live exchange), and the loop itself is replaced
    by ``loop`` — so what runs for real is ``_cmd_live``'s handling of what
    the loop raises or returns: the exit line and the exit code (issue #268).
    Unless the test sets a position, breaks the position read or arms the
    switch, the ``finally``
    sweep runs over the seams' flat account with the switch
    never armed, which is the shape of a flat-book exit; on ``--loop`` its §12.2
    pre-shutdown reconcile is scripted clean too (``reconcile``), since the
    signed double has no REST and an unclean pass would latch safe mode — the
    exit-4 lane this drive must be able to tell apart from protection-only's.
    """
    from contrib.hyperliquid_perp.live import reconcile as reconcile_mod, startup as startup_mod
    from contrib.hyperliquid_perp.live.startup import StartupResult

    monkeypatch.setattr(reconcile_mod.LiveReconciler, "reconcile_and_apply", reconcile)

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    _open_smoke_gate(dbp)
    passed = StartupResult(report=SimpleNamespace(clean=True), safe_mode_active=False)

    def _recovery(**kwargs):
        on_recovery(**kwargs)
        return passed

    monkeypatch.setattr(startup_mod, "run_startup_recovery", _recovery)
    monkeypatch.setattr(live_mod, "_run_live_loop", loop)
    argv = ["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]
    return cli_main(argv if one_shot else [*argv, "--loop"])


def test_cmd_live_hands_the_loop_the_session_its_recovery_ran_over(
    tmp_path, live_seams, monkeypatch
):
    # The loop's ``session=`` is the one object ``_live_startup_recovery``
    # built and ran the §19.1 recovery over — the same switch, safe mode
    # and reconciler, not a second session wired the same way (the inputs a
    # second one would be built from are shared, so those prove nothing).
    recovered = {}
    handed = {}

    def _loop(**kwargs):
        handed.update(kwargs)

    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path, monkeypatch, loop=_loop, on_recovery=lambda **kw: recovered.update(kw)
    )
    assert rc == 0
    session = handed["session"]
    for component in ("kill_switch", "safe_mode", "reconciler"):
        assert getattr(session, component) is recovered[component], component


def test_cmd_live_names_the_engine_refusal_over_a_flat_book_as_exit_1(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The loop re-raises the bridge's refusal when the book is flat (issue
    # #268); ``_cmd_live`` must print it by name and exit 1 — the paper
    # lane's code — not file it under the generic "startup recovery failed".
    from contrib.hyperliquid_perp.engine_bridge import EngineConfigError

    def _refuse(**kwargs):
        raise EngineConfigError(
            "config key 'temperature' (TRADINGAGENTS_TEMPERATURE) must be a finite, "
            "non-negative number, got 'abc'"
        )

    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_refuse)
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: config key 'temperature' (TRADINGAGENTS_TEMPERATURE)" in err
    assert "startup recovery failed" not in err


def test_cmd_live_flat_refusal_names_itself_when_the_shutdown_reread_fails(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The flat-book refusal raises out of the loop, so ``loop_raised`` stays
    # True; when the shutdown position re-read ALSO fails (unknown ≠ flat →
    # keep), the sweep's note must point at the error already printed, not
    # file it as an unaccounted-for raise (exit-check review).
    from contrib.hyperliquid_perp.engine_bridge import EngineConfigError

    def _refuse(**kwargs):
        raise EngineConfigError("config key 'temperature' (TRADINGAGENTS_TEMPERATURE) ...")

    live_seams.clearinghouse = None  # map_account_snapshot(None) raises → positions unreadable
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_refuse)
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: config key 'temperature' (TRADINGAGENTS_TEMPERATURE)" in err
    assert "could NOT be re-read at shutdown" in err
    assert "the engine could not be built (see the error above)" in err
    assert "raised instead of returning" not in err


def test_cmd_live_exits_1_when_protection_only_has_nothing_left_to_protect(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The loop's settle-exit: the position closed under protection-only. Exit
    # 1 like the paper loop's settle-exit, naming the cause the operator has
    # to fix — a supervisor's restart then meets the flat-book refusal above.
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit

    settled = ProtectionOnlyExit(cause="config key 'max_tokens' (TRADINGAGENTS_MAX_TOKENS) ...", settled=True)
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=lambda **kwargs: settled)
    err = capsys.readouterr().err
    assert rc == 1
    assert "nothing left to protect" in err
    assert "TRADINGAGENTS_MAX_TOKENS" in err
    assert "live loop exited — §18.2 shutdown sweep done" not in err  # not the all-quiet line
    # Flat: the protection-only "unclean" term keeps nothing (the AND with
    # the position re-read), so no STANDING line and no unclean sweep.
    assert "STANDING" not in err
    assert "§18.2 shutdown unclean" not in err


def test_cmd_live_exits_4_when_stopped_in_protection_only(tmp_path, capsys, live_seams, monkeypatch):
    # Ctrl-C / SIGTERM while protection-only: "executed, not clean" — the same
    # 4 a stop in safe mode returns, never 0 ("all quiet") for a run whose
    # decision cycles never ran. The cause rides in the exit line.
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit

    stopped = ProtectionOnlyExit(
        cause="config key 'llm_max_retries' (TRADINGAGENTS_LLM_MAX_RETRIES) ...", settled=False
    )
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=lambda **kwargs: stopped)
    err = capsys.readouterr().err
    assert rc == 4
    assert "exited from protection-only mode" in err
    assert "TRADINGAGENTS_LLM_MAX_RETRIES" in err


def test_cmd_live_keeps_sl_tp_standing_when_the_loop_raises_over_a_live_position(
    tmp_path, capsys, live_seams, monkeypatch
):
    """The class behind #268, not just its provider-construction instance: the
    §18.2 sweep keyed "clean" off the BOOT verdict, so ANY raise out of the
    loop after a pass (a REST read in construction, a store read, an import)
    stripped the resting SL/TP over a live position. The sweep now also asks
    whether the loop RETURNED; a raise keeps the SL/TP standing and says so.
    The control below — the same position, a loop that returns — shows the
    flag is what flips the outcome.
    """
    live_seams.clearinghouse = clearinghouse(positions=[btc_position()])

    def _boom(**kwargs):
        raise RuntimeError("a REST read in loop construction timed out")

    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_boom)
    err = capsys.readouterr().err
    assert rc == 1
    assert "startup recovery failed" in err  # the generic lane, as before
    assert "leaves the bot's resting SL/TP STANDING" in err
    assert "the live loop raised instead of returning" in err
    assert "UNPROTECTED after exit" not in err

    control = tmp_path / "control"
    control.mkdir()
    rc = _drive_cmd_live_loop_to_its_exit(control, monkeypatch, loop=lambda **kwargs: None)
    err = capsys.readouterr().err
    assert rc == 0
    assert "UNPROTECTED after exit" in err  # a clean loop exit: §18.2 semantics stand
    assert "STANDING" not in err
    # And the all-quiet exit line, untouched by the protection-only branches.
    assert "live loop exited — §18.2 shutdown sweep done" in err
    assert "protection-only" not in err


def test_cmd_live_protection_only_stop_keeps_sl_tp_standing(
    tmp_path, capsys, live_seams, monkeypatch
):
    # A deliberate stop of a protection-only loop is an unclean end (#270
    # review): the environment is wrong and the next start meets the same
    # refusal, so the sweep leaves the reduce-only SL/TP standing for the
    # fixed restart to adopt — stripping them on the operator's way to fixing
    # .env would be the front-gate hole the mode exists to close.
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit

    live_seams.clearinghouse = clearinghouse(positions=[btc_position()])
    stopped = ProtectionOnlyExit(cause="config key 'temperature' (TRADINGAGENTS_TEMPERATURE) ...", settled=False)
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=lambda **kwargs: stopped)
    err = capsys.readouterr().err
    assert rc == 4
    assert "leaves the bot's resting SL/TP STANDING" in err
    assert "the loop ran in protection-only mode" in err
    assert "exited from protection-only mode" in err
    # The cause line leads; the sweep's WARNING follows it.
    assert err.index("exited from protection-only mode") < err.index("SL/TP STANDING")


def test_cmd_live_unclean_sweep_outranks_the_code_but_not_the_cause(
    tmp_path, capsys, live_seams, monkeypatch
):
    # An unclean §18.2 sweep (the switch left armed) is always exit 4 — the
    # wallet-wide trigger may still fire — but the protection-only cause the
    # operator has to fix must still reach the output (#270 review).
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit
    from contrib.hyperliquid_perp.live import kill_switch as ks_mod

    monkeypatch.setattr(ks_mod.KillSwitchManager, "armed", property(lambda self: True))
    monkeypatch.setattr(ks_mod.KillSwitchManager, "shutdown", lambda self, *, keep_protective: None)
    settled = ProtectionOnlyExit(cause="config key 'max_tokens' (TRADINGAGENTS_MAX_TOKENS) ...", settled=True)
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=lambda **kwargs: settled)
    err = capsys.readouterr().err
    assert rc == 4
    assert "§18.2 shutdown unclean" in err
    assert "nothing left to protect" in err
    assert "TRADINGAGENTS_MAX_TOKENS" in err
    # ORDER, not just membership: the cause line comes first, ahead of the
    # unclean-sweep line the ``finally`` prints (round 2: the earlier
    # placement after the try/finally printed it last).
    assert err.index("nothing left to protect") < err.index("§18.2 shutdown unclean")


def test_cmd_live_settled_protection_only_outranks_a_safe_mode_latch(
    tmp_path, capsys, live_seams, monkeypatch
):
    # A tick error can latch recoverable safe mode while protection-only
    # runs; when the position then closes, the exit must still name the
    # cause the operator has to fix and exit 1 (the ``safe_mode:`` line
    # printed above reports the latch), not vanish behind the safe-mode 4.
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit

    def _latch_then_settle(**kwargs):
        _latch_recoverable(kwargs["session"].db)
        return ProtectionOnlyExit(cause="config key 'temperature' (TRADINGAGENTS_TEMPERATURE) ...", settled=True)

    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_latch_then_settle)
    captured = capsys.readouterr()
    assert rc == 1
    assert "nothing left to protect" in captured.err
    assert "TRADINGAGENTS_TEMPERATURE" in captured.err
    assert "safe_mode: recoverable" in captured.out  # the latch is still reported


def test_cmd_live_loop_reconciles_once_more_before_the_shutdown_sweep(
    tmp_path, capsys, live_seams, monkeypatch
):
    # §12.2 rule 8: a --loop run has placed orders since the boot verdict, so
    # the sweep works from a fresh "shutdown" pass (the one-shot's verdict
    # pass is its last word, and the recovery here is scripted).
    triggers: list[str] = []
    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path,
        monkeypatch,
        loop=lambda **kwargs: None,
        reconcile=lambda self, trigger, **kw: triggers.append(trigger),
    )
    capsys.readouterr()
    assert rc == 0
    assert triggers == ["shutdown"]


def test_cmd_live_hands_the_keep_decision_to_the_sweep(tmp_path, capsys, live_seams, monkeypatch):
    # The keep decision must reach ``KillSwitchManager.shutdown``, not only
    # the WARNING line: a raise out of the loop over a live position keeps
    # the SL/TP, a clean loop exit over the same position does not.
    from contrib.hyperliquid_perp.live import kill_switch as ks_mod

    kept: list[bool] = []
    monkeypatch.setattr(ks_mod.KillSwitchManager, "armed", property(lambda self: True))
    monkeypatch.setattr(
        ks_mod.KillSwitchManager,
        "shutdown",
        lambda self, *, keep_protective: kept.append(keep_protective),
    )
    live_seams.clearinghouse = clearinghouse(positions=[btc_position()])

    def _boom(**kwargs):
        raise RuntimeError("a store read in the loop failed")

    _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_boom)
    control = tmp_path / "control"
    control.mkdir()
    _drive_cmd_live_loop_to_its_exit(control, monkeypatch, loop=lambda **kwargs: None)
    capsys.readouterr()
    assert kept == [True, False]


def test_cmd_live_loop_that_latched_safe_mode_exits_4_naming_it(
    tmp_path, capsys, live_seams, monkeypatch
):
    # A latch taken mid-loop must not hand exit 0 ("all quiet") to the
    # supervisor: the boot verdict is stale after a loop.
    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path, monkeypatch, loop=lambda **kwargs: _latch_recoverable(kwargs["session"].db)
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert "live loop exited IN SAFE MODE — see safe_mode above" in captured.err
    assert "safe_mode: recoverable" in captured.out


def test_cmd_live_loop_exits_4_when_sl_tp_were_kept_behind_a_failed_safe_mode_read(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The exit-time safe-mode read fails, so the sweep keeps the SL/TP over
    # the live position (unknown ≠ clean). A later read that finds no latch
    # must not talk the exit code back down to 0 over those kept orders.
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    def _unreadable(self):
        raise RuntimeError("database is locked")

    def _loop_then_break_the_read(**kwargs):
        monkeypatch.setattr(SafeModeManager, "active", property(_unreadable))

    live_seams.clearinghouse = clearinghouse(positions=[btc_position()])
    rc = _drive_cmd_live_loop_to_its_exit(tmp_path, monkeypatch, loop=_loop_then_break_the_read)
    captured = capsys.readouterr()
    assert rc == 4
    assert "the exit-time safe-mode state could NOT be read (unknown ≠ clean)" in captured.err
    assert (
        "live loop exited with protective orders kept behind a FAILED shutdown "
        "safe-mode read (unknown ≠ clean)"
    ) in captured.err
    assert "safe_mode: none" in captured.out
    assert "could NOT be read after the §18.2 shutdown sweep" not in captured.err


@pytest.mark.parametrize(
    ("positions", "code", "last_line"),
    [
        (
            [btc_position()],
            4,
            "startup recovery passed, but protective orders were kept behind a "
            "FAILED shutdown safe-mode read (unknown ≠ clean)",
        ),
        ([], 0, "startup recovery passed — a live loop can start from this state"),
    ],
    ids=["live", "flat"],
)
def test_cmd_live_one_shot_exits_4_when_sl_tp_were_kept_behind_a_failed_safe_mode_read(
    tmp_path, capsys, live_seams, monkeypatch, positions, code, last_line
):
    # Issue #303: the one-shot's exit-time safe-mode read fails. Over a live
    # position the keep decision holds the SL/TP and the exit is 4; over a
    # flat book nothing is kept and the exit stays 0. The switch is never
    # armed here, so no sweep runs: this pins the decision reaching the code.
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    def _unreadable(self):
        raise RuntimeError("database is locked")

    def _loop_must_not_run(**kwargs):
        raise AssertionError("the one-shot entered the live loop")

    monkeypatch.setattr(SafeModeManager, "active", property(_unreadable))
    live_seams.clearinghouse = clearinghouse(positions=positions)
    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path, monkeypatch, loop=_loop_must_not_run, one_shot=True
    )
    captured = capsys.readouterr()
    assert rc == code
    assert last_line in captured.err
    warned = "the exit-time safe-mode state could NOT be read (unknown ≠ clean)" in captured.err
    assert warned is (code == 4)
    assert "safe_mode: none" in captured.out


def _break_every_safe_mode_read(monkeypatch):
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    def _unreadable(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(SafeModeManager, "current", _unreadable)


@pytest.mark.parametrize(
    ("positions", "last_line"),
    [
        (
            [btc_position()],
            "live loop exited with protective orders kept behind a FAILED shutdown "
            "safe-mode read (unknown ≠ clean)",
        ),
        (
            [],
            "live loop exited, but the safe-mode state could NOT be read after the "
            "§18.2 shutdown sweep (unknown ≠ clean)",
        ),
    ],
    ids=["live", "flat"],
)
def test_cmd_live_loop_exits_4_by_name_when_safe_mode_stays_unreadable(
    tmp_path, capsys, live_seams, monkeypatch, positions, last_line
):
    # Issue #308: the store never answers the safe-mode question again, so
    # the read after the sweep fails like the one before it.
    live_seams.clearinghouse = clearinghouse(positions=positions)
    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path, monkeypatch, loop=lambda **kwargs: _break_every_safe_mode_read(monkeypatch)
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert last_line in captured.err
    assert "fatal: unexpected error" not in captured.err
    assert (
        "WARNING: the safe-mode state could NOT be read after the §18.2 shutdown "
        "sweep (RuntimeError: database is locked)"
    ) in captured.err
    assert "safe_mode: unknown" in captured.out
    assert "safe_mode: none" not in captured.out


@pytest.mark.parametrize(
    ("positions", "code", "last_line"),
    [
        (
            [btc_position()],
            4,
            "startup recovery passed, but protective orders were kept behind a "
            "FAILED shutdown safe-mode read (unknown ≠ clean)",
        ),
        ([], 0, "startup recovery passed — a live loop can start from this state"),
    ],
    ids=["live", "flat"],
)
def test_cmd_live_one_shot_keeps_its_exit_when_safe_mode_stays_unreadable(
    tmp_path, capsys, live_seams, monkeypatch, positions, code, last_line
):
    # Issue #308, the one-shot: it does not read the latch, so the read that
    # fails after the sweep changes the ``safe_mode:`` line and not the code.
    def _loop_must_not_run(**kwargs):
        raise AssertionError("the one-shot entered the live loop")

    live_seams.clearinghouse = clearinghouse(positions=positions)
    rc = _drive_cmd_live_loop_to_its_exit(
        tmp_path,
        monkeypatch,
        loop=_loop_must_not_run,
        one_shot=True,
        on_recovery=lambda **_: _break_every_safe_mode_read(monkeypatch),
    )
    captured = capsys.readouterr()
    assert rc == code
    assert last_line in captured.err
    assert "fatal: unexpected error" not in captured.err
    assert (
        "WARNING: the safe-mode state could NOT be read after the §18.2 shutdown "
        "sweep (RuntimeError: database is locked)"
    ) in captured.err
    assert "safe_mode: unknown" in captured.out


def test_every_live_exit_reason_has_a_last_line_entry():
    # A new ExitReason must get its wording (or an explicit None) here, not
    # fall silent. The three None ones print their line inside the finally.
    from contrib.hyperliquid_perp.cli.live import _exit_line
    from contrib.hyperliquid_perp.live.shutdown import ExitReason

    silent = {r for r in ExitReason if _exit_line(r) is None}
    assert silent == {
        ExitReason.SWEEP_UNCLEAN,
        ExitReason.PROTECTION_ONLY_SETTLED,
        ExitReason.PROTECTION_ONLY_STOPPED,
    }


def test_every_gate_stage_live_can_hit_has_its_wording():
    from contrib.hyperliquid_perp.cli.live import _gate_refusal_wording
    from contrib.hyperliquid_perp.live.config import LiveGateRefusal, LiveGateStage

    unworded = set()
    for stage in LiveGateStage:
        try:
            _gate_refusal_wording(LiveGateRefusal(stage))
        except KeyError:
            unworded.add(stage)
    # ``live`` passes no ``modes``, so this rung never fires for it.
    assert unworded == {LiveGateStage.MODE_NOT_ACCEPTED}


@pytest.mark.parametrize("behind_version", BEHIND_VERSIONS)
def test_live_lease_conflict_leaves_a_behind_store_unmigrated(
    tmp_path, capsys, live_seams, monkeypatch, behind_version
):
    # Issue #129, the live sibling of the paper test: ``live`` also opened
    # with migrate-on-open ahead of its lease check. A one-shot recovery run
    # (no --loop, so no smoke gate) against a run another pid holds must be
    # refused with the store still at the version that daemon owns — at the
    # lease floor too (issue #147), where none of the live tables exist yet.
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )

    def build():
        dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
        db = Database(dbp, migrate=False)
        acquire_run_lock(db, "r1", pid=999999, now=datetime.now(timezone.utc))
        db.close()
        return dbp

    behind, dbp = build_behind(build, behind_version)
    assert stored_version(dbp) == behind

    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already being driven by pid 999999" in err
    # The peek exempts no pid, so a recycled pid refuses itself; the message
    # names this process's pid so the RUNBOOK's pid-recycling row can be
    # matched after the process is gone (issue #147).
    assert f"(this process is pid {os.getpid()})" in err
    assert stored_version(dbp) == behind


def test_live_create_into_a_newer_store_is_refused_before_the_run_row(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The deferred open no longer refuses a store migrated by a NEWER build at
    # open, so the refusal must land before --create writes anything: an older
    # binary pointed at the upgraded store must exit 1 by name with no run row
    # left behind (or the corrected re-run is rejected as "already exists").
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    db = Database(dbp)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2099-01-01T00:00:00+00:00"),
        )
    db.close()

    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r2", "--db", str(dbp), "--create"])
    assert rc == 1
    assert "NEWER build" in capsys.readouterr().err
    probe = connect(dbp)
    assert probe.execute("SELECT COUNT(*) FROM runs WHERE run_id = 'r2'").fetchone()[0] == 0
    probe.close()


def test_live_resume_refuses_a_paper_mode_run(tmp_path, capsys, live_seams, monkeypatch):
    # The live mirror of test_paper_resume_refuses_a_live_mode_run: a typo'd
    # --run-id/--db pointing at a paper run is refused by name before the
    # lease, the kill switch or any reconciliation write touches it.
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = tmp_path / "paper_store.db"
    db = Database(dbp)
    accounting.initialize_run(
        db, run_id="r1", mode="paper", initial_balance_usdc=D(1000), schema_version=SCHEMA_VERSION
    )
    db.close()
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    assert "is a paper run — resuming it here would arm the kill switch" in capsys.readouterr().err
    probe = connect(dbp)
    assert repo.get_scheduler_state(probe, "r1") is None  # no lease stamped
    probe.close()


def test_live_will_not_migrate_under_a_paper_siblings_fresh_lease(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The store-wide half of #129 on the live path. A paper run in the same
    # file is exactly what the wallet-hazard check (_conflicting_run_lease)
    # deliberately ignores — it signs nothing — but a migration rewrites its
    # tables all the same. With an upgrade owed, its fresh lease refuses.
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )

    def build():
        dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
        db = Database(dbp, migrate=False)
        accounting.initialize_run(
            db, run_id="paper-ETH", mode="paper", initial_balance_usdc=D(1000), schema_version=1
        )
        acquire_run_lock(db, "paper-ETH", pid=999999, now=datetime.now(timezone.utc))
        db.close()
        return dbp

    behind, dbp = build_one_behind(build)
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "run 'paper-ETH'" in err and "needs to migrate the store" in err
    assert stored_version(dbp) == behind


def test_live_migrates_a_behind_store_once_nobody_owns_it(
    tmp_path, capsys, live_seams, monkeypatch
):
    # The other half for live: with no fresh lease on this run or its wallet
    # siblings, the upgrade runs before the identity/off-coin reads that need
    # the newer columns. The testnet --loop smoke-gate refusal fires after
    # that point, so exit 4 here proves the store is current.
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    behind, dbp = build_one_behind(lambda: seed_live_run_with_genesis_subset(tmp_path, cfg))
    assert stored_version(dbp) == behind

    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 4
    assert "§20.2 smoke suite" in capsys.readouterr().err
    assert stored_version(dbp) == SCHEMA_VERSION


def _open_smoke_gate(dbp, run_id="r1") -> None:
    from contrib.hyperliquid_perp.live import smoke_catalog

    db = Database(dbp)
    with db.transaction() as conn:
        for test in smoke_catalog.SMOKE_TESTS:
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
    from contrib.hyperliquid_perp.persistence import repository as repo_mod
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="sibling")
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
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="sibling")
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
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    # The lock is scripted to refuse so the command stops there — proof it got
    # PAST the gate rather than being refused by it. (A pre-held lease no
    # longer works as the stop: since issue #129 it is caught by the read-only
    # peek at open, before the gate is ever evaluated.)
    def refuse(db, run_id, *, pid, now):
        raise run_lock_mod.RunLockError("scripted lease refusal")

    monkeypatch.setattr(run_lock_mod, "acquire_run_lock", refuse)
    # The MAINNET key: this is a mainnet run, and the testnet key the fixture
    # exports does not satisfy it. Without this the test exited 1 at the
    # agent-key refusal and never reached the gate at all (found 2026-08-28,
    # when the stop was made explicit).
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: mainnet_tiny\n  network: mainnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1  # the lease, not the gate's exit 4
    err = capsys.readouterr().err
    assert "scripted lease refusal" in err
    assert "§20.2 smoke suite" not in err
    assert "not yet run" not in err


def test_live_loop_without_an_api_key_is_refused_up_front(
    tmp_path, capsys, live_seams, monkeypatch
):
    """Without a key every 4h cycle records api_failed, which never counts toward
    the §20.3 >=30-cycle gate — a real-money run could burn days producing nothing
    gateable. _cmd_paper always checked this; the live path did not (2026-07-30).
    """
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp), "--loop"])
    assert rc == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_live_without_loop_still_runs_keyless(tmp_path, capsys, live_seams, monkeypatch):
    """Control for the guard above: `live` without --loop never polls the AI.

    It arms, sweeps and exits, so it must stay keyless — the guard belongs to
    --loop alone. Stopped at the pre-held lease, well past the key check.
    """
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg)
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
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
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

    from contrib.hyperliquid_perp.live import kill_switch as ks_mod

    seen: list[dict] = []
    record_constructor_kwargs(monkeypatch, ks_mod, "KillSwitchManager", seen)
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    # The recovery is NOT driven to completion, and that is deliberate rather
    # than papered over: ``live_seams``' signed double has no ``schedule_cancel``,
    # so arming the switch fails and the command reports the exit-1 "startup
    # recovery failed" path. The fact under test is settled long before that —
    # what the CLI hands the constructor. Asserted rather than suppressed:
    # swallowing every exception here would also swallow a regression anywhere
    # in the recovery this drive now reaches (2026-08-19 exit check).
    assert cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 1
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
    pin in cli/test_smoke.py drives ``live-smoke`` and therefore reaches
    only the smoke constructor; deleting the kwarg from the DAEMON constructor
    — the processor handed to the live loop and to FillBackfiller, the one that
    ingests real userFills for weeks — left the whole suite green
    (2026-08-17 identity-echo mutation probe). Same shape, and same reason, as
    the suite_authored pin above.
    """

    # cli.py imports the class lazily inside the command function, so the seam
    # is the SOURCE module, not a cli attribute.
    from contrib.hyperliquid_perp.live import fills as fills_mod

    seen: list[dict] = []
    record_constructor_kwargs(monkeypatch, fills_mod, "LiveFillProcessor", seen)
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    # Recovery is not driven to completion, and the exit code is asserted, for
    # the reasons the sibling pin above states.
    assert cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 1
    assert seen, "no LiveFillProcessor was constructed - the pin proves nothing"
    assert all(kwargs.get("wallet_address") == LIVE_WALLET for kwargs in seen), seen


def _drive_the_daemon_recovery(tmp_path, monkeypatch):
    """Run ``live --run-id`` far enough to build the recovery components.

    Stops inside ``run_startup_recovery``: arming the switch calls
    ``schedule_cancel``, which the ``live_seams`` double refuses while
    ``rest_enabled`` is off (recording the refusal), and
    ``_live_startup_recovery`` reports that as the exit-1 "startup recovery
    failed" path. Everything these pins assert is settled before then — and the
    exit code is asserted rather than suppressed so that "the drive still gets
    there" stays observable rather than assumed.
    """
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
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

    # One boot, one recovery: the count is part of the shape, as it is at the
    # smoke site, so a second recovery appearing on this path has to be noticed
    # rather than silently halving what the pairing below covers.
    assert len(record.switches) == 1, record.switches
    assert_paired_sweep_refreshes(record, owner="daemon")
    # The double refuses REST unless a test opts in; the drive's one recorded
    # refusal is arm()'s schedule_cancel — the documented stopping point — and
    # nothing after it, so the drive never ran into work these pins do not
    # model.
    assert live_seams.rest_calls == ["schedule_cancel"]


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

    assert len(record.reconcilers) == 1, record.reconcilers
    for reconciler in record.reconcilers:
        assert_payload_dir(reconciler, dbp, run_id="r1")
    assert live_seams.rest_calls == ["schedule_cancel"]  # as in the sibling pin above


def test_the_daemon_gives_its_sweep_components_one_identity_monitor(
    tmp_path, live_seams, monkeypatch
):
    """§13.5 (issue #80): one venue-identity streak per process, by wiring.

    Both ``KillSwitchManager.identity`` and ``LiveReconciler.identity`` default
    to None and fall back to a PRIVATE monitor, so dropping either kwarg here
    leaves every unit test green while the daemon quietly runs two (or three,
    with protection's) separate streaks — and a misroute that alternates
    between the shutdown cross-check and the reconciler's settle probes never
    reaches any of their thresholds. Identity, not equality: the pin is that
    they are the SAME object. The monitor must also carry the payload_dir the
    reconciler pin above checks, or the refused-answer evidence goes nowhere.
    """
    record = record_reconciliation_sweep_wiring(monkeypatch)
    dbp = _drive_the_daemon_recovery(tmp_path, monkeypatch)

    assert len(record.switches) == 1 and len(record.reconcilers) == 1
    shared = record.reconcilers[0].get("identity")
    assert shared is not None, "the reconciler was left to build a private monitor"
    assert record.switches[0]._identity is shared
    assert_payload_dir({"payload_dir": shared._payload_dir}, dbp, run_id="r1")
    assert live_seams.rest_calls == ["schedule_cancel"]  # as in the sibling pins


def test_a_venue_identity_fault_latched_at_shutdown_persists_manual_for_the_next_boot(
    tmp_path, capsys, live_seams, monkeypatch
):
    """End-to-end through the CLI (issue #80): the §18.2 disarm cross-check's
    misrouted answer crosses the threshold, the escalation folds into
    ``shutdown_problem`` (so the run can never end "all quiet"), and the
    persisted manual state — with the identity reason — is what the next boot
    hydrates.

    The threshold is patched to 3 because the one-shot lane asks about a cloid
    exactly three times (two §19.1 reconciliation passes + the cross-check);
    what is under test is the WIRING, not the constant, whose value is pinned
    in test_venue_identity. Exit code measured at 4 — the executed-but-unclean
    contract — never 0: the same misrouted answers that build the streak also
    leave the verdict unclean.
    """
    import contrib.hyperliquid_perp.live.venue_identity as vi_mod
    from contrib.hyperliquid_perp.live.safe_mode import (
        REASON_IDENTITY_FAULT,
        SafeModeManager,
    )
    from contrib.hyperliquid_perp.persistence import repository as repo

    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(vi_mod, "UNREADABLE_PROBE_LATCH_THRESHOLD", 3)
    cfg = live_yaml(
        tmp_path,
        live_lines="  mode: testnet_live\n  network: testnet\n  allow_real_orders: true\n",
    )
    dbp = seed_live_run_with_genesis_subset(tmp_path, cfg, run_id="r1")
    hex_id = "0x" + "ab" * 16
    db = Database(dbp)
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical="log-e2e",
            cloid_hex=hex_id,
            run_id="r1",
            symbol="BTC",
            order_role="entry",
        )
        repo.insert_order(
            conn,
            order_id="o-e2e",
            mode="live",
            run_id="r1",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=D("0.001"),
            status="open",
            price=D(100),
            cloid_logical="log-e2e",
            cloid_hex=hex_id,
            exchange_order_id="900",
            is_bot_owned=True,
            timestamp=datetime.now(timezone.utc),
        )
    db.close()

    live_seams.rest_enabled = True
    # Every orderStatus answer about our order names a STRANGER's identity.
    live_seams.order_status[hex_id] = misrouted_order_status()

    rc = cli_main(["live", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    err = capsys.readouterr().err

    assert rc == 4  # executed-but-unclean; never 0
    assert "§18.2 shutdown unclean" in err
    assert "venue identity fault latched" in err
    db = Database(dbp)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        assert state is not None and state.is_manual
        assert state.reason == REASON_IDENTITY_FAULT
        latch_rows = identity_latch_rows(db, run_id="r1")
        assert len(latch_rows) == 1
        assert "kill-switch disarm cross-check" in latch_rows[0]["detail"]
    finally:
        db.close()


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
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = live_yaml(
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
