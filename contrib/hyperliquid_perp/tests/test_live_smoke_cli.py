"""Tests for the REAL (non-dry-run) live-smoke session and validate's live exits.

Every existing live-smoke CLI test either passes ``--dry-run`` or monkeypatches
``_build_smoke_session`` away, so ``_build_real_smoke_session`` — the §6.1/§4.1
guard ladder, the signed-client + market-data wiring, and the ``run_recovery``
seam that drives one real §19.1 startup recovery — had no coverage at all.
The ``smoke_seams`` fixture here mirrors test_cli.py's ``live_seams``: it fakes
the module-level network seams (the CLI's function-local imports bind at call
time) with a consistent, clean exchange — no open orders, no fills, a flat
account whose value matches the seeded ledger — so the CLI drives the REAL
SmokeTestRunner, RealOrderGate, KillSwitchManager, LiveReconciler and
run_startup_recovery machinery offline, end to end through ``cli_main``.

Also covered: the ``validate`` live exit mapping (0 when ``live_ready``, 5 on
an integrity failure) at the CLI layer, over test_live_validation's ``_healthy``
store builder.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main
from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import CancelAck, OrderAck
from contrib.hyperliquid_perp.live.authorization import (
    AgentAuthorization,
    AgentAuthorizationError,
)
from contrib.hyperliquid_perp.live.smoke import SMOKE_TEST_KEYS
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from .test_cli import _seed_live_run_with_genesis_subset as _seed_genesis_run
from .test_live_startup import _clearinghouse
from .test_live_validation import _healthy

_D = Decimal
_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)

_SMOKE_WALLET = "0x" + "aa" * 20
_SMOKE_KEY = "0x" + "11" * 32
_SMOKE_ENV = "HYPERLIQUID_AGENT_KEY_TESTNET"
_AGENT_ADDR = "0x" + "cc" * 20


def _smoke_yaml(tmp_path, *, wallet: str | None = _SMOKE_WALLET, allow_real_orders: bool = True):
    """A minimal live config that passes every live-smoke config gate."""
    path = tmp_path / "smoke-cfg.yaml"
    text = ""
    if wallet is not None:
        text += f'wallet_address: "{wallet}"\n'
    text += "risk:\n  leverage: 1\n  margin_mode: cross\n  max_target_margin_pct: 60\n"
    text += "live:\n  mode: testnet_live\n  network: testnet\n"
    if allow_real_orders:
        text += "  allow_real_orders: true\n"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def smoke_seams(monkeypatch):
    """Fake every network seam ``_build_real_smoke_session`` touches; return knobs.

    Same discipline as test_cli.py's ``live_seams``: the read-only client, the
    §6.1 authorization check, the signed client, and the market-data reads are
    patched at their module seams, so the CLI drives the real smoke runner AND
    the real §19.1 recovery (pre-flight + restart tests 15-17) fully offline.
    The fake exchange is clean and self-consistent: no open orders, no fills,
    a flat account matching the ledger — every recovery verdict passes. IOC
    acks echo the FULL requested size back as ``filled`` (tests 7/18 require a
    full close; a ``resting`` IOC would fail fail-safe), trigger acks rest.
    """
    from contrib.hyperliquid_perp.exchanges.hyperliquid import (
        market_data as market_data_mod,
        sdk_client as sdk_mod,
        signed_client as signed_mod,
    )
    from contrib.hyperliquid_perp.live import authorization as auth_mod

    state = SimpleNamespace(
        # The flat 200-USDC clearinghouse matches the seeded ledger, so every
        # recovery's equity leg reconciles.
        clearinghouse=_clearinghouse(account_value="200"),
        mark=_D(50000),
        auth_error=None,
        auth_calls=[],
        schedule_calls=[],
        clear_calls=0,
        # After this many successful clears, clear_scheduled_cancel raises —
        # 1 lets test 14's own in-test clear pass while the END-OF-SUITE
        # disarm (the second call in a full run) fails.
        clear_fail_after=None,
        oids=itertools.count(1),
    )

    class _FakeClient:
        def __init__(self, network="mainnet", *, timeout=None):
            self.network = network
            self.timeout = timeout
            self.info = SimpleNamespace(user_state=lambda addr: state.clearinghouse)

        @classmethod
        def from_config(cls, config, *, timeout=None, network=None):
            return cls(network=network or config.get("network", "mainnet"), timeout=timeout)

    class _FakeMarketData:
        def __init__(self, client):
            pass

        def get_asset_meta(self, coin):
            return 3, MarginSchedule(coin=coin, tiers=(MarginTier(_D(0), _D(50)),))

        def get_market_snapshot(self, coin):
            return SimpleNamespace(mark_price=state.mark)

    def _fake_verify(info, *, wallet_address, agent_key, now=None):
        state.auth_calls.append(wallet_address)
        if state.auth_error is not None:
            raise state.auth_error
        return AgentAuthorization(
            agent_address=_AGENT_ADDR,
            valid_until=datetime.now(timezone.utc) + timedelta(days=90),
        )

    class _FakeSigned:
        def __init__(self, network, agent_key, *, wallet_address, gate, timeout=None):
            self.network = network
            self.wallet_address = wallet_address
            self.agent_address = _AGENT_ADDR

        def health_check(self):
            return None

        def update_leverage(self, *, coin, leverage, is_cross=True):
            return None

        def place_ioc_limit(
            self,
            *,
            coin,
            is_buy,
            size,
            limit_price,
            cloid_hex,
            reduce_only=False,
            protective=False,
        ):
            # An IOC is terminal at the ack; echoing the FULL size keeps the
            # exchange flat after every open+close pair, matching the flat
            # clearinghouse the recoveries reconcile against.
            return OrderAck(
                status="filled",
                exchange_order_id=str(next(state.oids)),
                filled_size=size,
                average_price=limit_price,
            )

        def place_trigger_order(
            self,
            *,
            coin,
            is_buy,
            size,
            limit_price,
            trigger_price,
            tpsl,
            cloid_hex,
            reduce_only=True,
        ):
            return OrderAck(status="resting", exchange_order_id=str(next(state.oids)))

        def modify_trigger_order(
            self,
            *,
            target,
            coin,
            is_buy,
            size,
            limit_price,
            trigger_price,
            tpsl,
            cloid_hex,
            reduce_only=True,
        ):
            return OrderAck(status="resting", exchange_order_id=str(next(state.oids)))

        def cancel_by_cloid(self, *, coin, cloid_hex):
            return CancelAck(success=True)

        def cancel_by_oid(self, *, coin, exchange_order_id):
            return CancelAck(success=True)

        def query_order_by_cloid(self, cloid_hex):
            return {"status": "unknownOid"}

        def user_fills_by_time(self, start_time_ms, end_time_ms=None):
            return []

        def open_orders(self):
            return []

        def exchange_time(self):
            return None  # the kill switch skips the skew check with a warning

        def schedule_cancel(self, *, cancel_at):
            state.schedule_calls.append(cancel_at)

        def clear_scheduled_cancel(self):
            state.clear_calls += 1
            if state.clear_fail_after is not None and state.clear_calls > state.clear_fail_after:
                raise ExchangeRequestError("scheduleCancel clear refused")

    monkeypatch.setattr(sdk_mod, "HyperliquidClient", _FakeClient)
    monkeypatch.setattr(market_data_mod, "HyperliquidMarketData", _FakeMarketData)
    monkeypatch.setattr(auth_mod, "verify_agent_authorization", _fake_verify)
    monkeypatch.setattr(signed_mod, "HyperliquidSignedClient", _FakeSigned)
    monkeypatch.setenv(_SMOKE_ENV, _SMOKE_KEY)
    return state


# -- the real-run guard ladder (order matters: each earlier guard satisfied) --


def test_live_smoke_real_run_refuses_allow_real_orders_false(tmp_path, capsys, smoke_seams):
    cfg = _smoke_yaml(tmp_path, allow_real_orders=False)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    assert "live.allow_real_orders is false" in capsys.readouterr().err


def test_live_smoke_real_run_missing_wallet_exits_1(tmp_path, capsys, smoke_seams):
    cfg = _smoke_yaml(tmp_path, wallet=None)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    assert "wallet_address is not configured" in capsys.readouterr().err


def test_live_smoke_real_run_missing_agent_key_names_the_env_var(
    tmp_path, capsys, smoke_seams, monkeypatch
):
    monkeypatch.delenv(_SMOKE_ENV, raising=False)
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    assert _SMOKE_ENV in capsys.readouterr().err
    assert smoke_seams.auth_calls == []  # the key guard fired before authorization


def test_live_smoke_real_run_auth_failure_exits_1(tmp_path, capsys, smoke_seams):
    smoke_seams.auth_error = AgentAuthorizationError("agent not in extra_agents")
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "agent authorization failed" in err
    assert smoke_seams.auth_calls == [_SMOKE_WALLET]


# -- the full real suite through the CLI --------------------------------------


def test_live_smoke_full_real_suite_passes_gate_and_releases_lock(tmp_path, capsys, smoke_seams):
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "smoke_gate_passed: yes" in out
    with Database(dbp) as db:
        latest = repo.latest_smoke_test_results(db.conn, "r1")
        assert set(latest) == set(SMOKE_TEST_KEYS)  # all 18 ran for real
        assert all(row["status"] == "passed" for row in latest.values())
        assert all(row["dry_run"] == 0 for row in latest.values())
    # The pre-flight + tests 15-17 each armed the switch; test 14 cleared once
    # in-test, and the end-of-suite disarm cleared exactly once more.
    assert smoke_seams.clear_calls == 2
    assert len(smoke_seams.schedule_calls) >= 4  # 4 recoveries armed + test 14
    # The run lock was released: a second full invocation succeeds too.
    rc2 = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc2 == 0
    assert "smoke_gate_passed: yes" in capsys.readouterr().out


def test_live_smoke_disarm_failure_warns_but_exit_stays_the_gates(tmp_path, capsys, smoke_seams):
    # Test 14's own clear succeeds (call 1); the end-of-suite disarm (call 2)
    # raises — the gate still passed, so rc stays 0 with the loud warning.
    smoke_seams.clear_fail_after = 1
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "smoke_gate_passed: yes" in captured.out
    assert "kill-switch disarm FAILED" in captured.err


# -- validate (live): the CLI's 0 / 5 exit mapping -----------------------------


def test_validate_live_ready_run_exits_0(tmp_path, capsys):
    db = _healthy(tmp_path)
    db.close()
    rc = cli_main(["validate", "--run-id", "r", "--db", str(tmp_path / "live.db")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "live_ready: yes" in out


def test_validate_live_integrity_failure_exits_5(tmp_path, capsys):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="startup",
            case_type="orphan_exchange_order",
            symbol="BTC",
            exchange_value="oid-9",
            timestamp=_T0,
        )
    db.close()
    rc = cli_main(["validate", "--run-id", "r", "--db", str(tmp_path / "live.db")])
    out = capsys.readouterr().out
    assert rc == 5
    assert "live_ready: no" in out
    assert any("orphan" in line for line in out.splitlines() if line.startswith("failure:"))
