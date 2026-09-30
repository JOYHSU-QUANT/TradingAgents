"""Fixtures and store/config builders the ``tests/cli`` modules share."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli._drift import _run_config_subset
from contrib.hyperliquid_perp.persistence.db import Database, connect, stored_schema_version
from contrib.hyperliquid_perp.persistence.schema import LEASE_READABLE_SINCE, MIGRATIONS
from contrib.hyperliquid_perp.runtime import accounting

from ..conftest import migrations_up_to
from ..fakes.market import margin_schedule
from ..fakes.payloads import clearinghouse

D = Decimal


def seed_db(tmp_path):
    path = tmp_path / "cli.db"
    db = Database(path)
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return path, db


@pytest.fixture
def paper_seams(tmp_path, monkeypatch):
    """Mock the exchange seam so ``paper``'s pre-lease guards run keylessly.

    The create/resume identity checks fire only after the exchange metadata
    fetch, so the client factory and ``get_asset_meta`` are patched at the
    module seam (no network); everything the guards need stays real.
    """
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
    schedule = margin_schedule()
    monkeypatch.setattr(
        md_mod.HyperliquidMarketData,
        "get_asset_meta",
        lambda self, coin: (3, schedule),
    )
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("", encoding="utf-8")
    return cfg


def paper_argv(db_path, *, run_id, config, create=False):
    argv = ["paper", "--coin", "BTC", "--db", str(db_path), "--run-id", run_id]
    if create:
        argv.append("--create")
    return argv + ["--config", str(config)]


# The env knob family the bridge gates at startup, one bad value each: the
# cap (#177), the retry budget (#266) and the temperature (#269).
BAD_ENGINE_ENV_KNOBS = [
    ("max_tokens", "8k", "TRADINGAGENTS_MAX_TOKENS"),
    ("llm_max_retries", "abc", "TRADINGAGENTS_LLM_MAX_RETRIES"),
    ("temperature", "abc", "TRADINGAGENTS_TEMPERATURE"),
]


def build_behind(build, behind: int):
    """Run ``build()`` with every migration past ``behind`` hidden, so the
    store it makes is that many schema versions behind this build."""
    with migrations_up_to(behind):
        result = build()
    return behind, result


def build_one_behind(build):
    """The deploy-box shape a new binary meets when an older daemon still owns
    the store: exactly one schema version behind."""
    from contrib.hyperliquid_perp.persistence.schema import MIGRATIONS

    return build_behind(build, sorted(MIGRATIONS)[-2])


# The two shapes an owning command's pre-lease refusal must survive: the lease
# floor itself (the oldest store whose lease it can read, issue #147) and the
# routine one-behind deploy-box store.
BEHIND_VERSIONS = [LEASE_READABLE_SINCE, sorted(MIGRATIONS)[-2]]


def stored_version(path) -> int:
    probe = connect(path)
    try:
        return stored_schema_version(probe)
    finally:
        probe.close()


LIVE_WALLET = "0x" + "aa" * 20


LIVE_KEY = "0x" + "11" * 32


LIVE_ENV = "HYPERLIQUID_AGENT_KEY_TESTNET"


def live_yaml(
    tmp_path,
    *,
    wallet: str | None = LIVE_WALLET,
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
        # Opt-in REST behaviour (issue #80 review): False keeps the historical
        # contract — the double has NO REST behaviour, the daemon drive dies at
        # arm(), and the no-REST pins stay meaningful. A test that needs the
        # recovery to run through the sweep flips this and scripts the seams.
        rest_enabled=False,
        order_status={},
        open_orders_result=[],
        # What Info's user_state answers when a rest_enabled test reaches the
        # clearinghouse read: a flat, healthy account.
        clearinghouse=clearinghouse(account_value="200"),
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
            self.info = SimpleNamespace(user_state=lambda address: state.clearinghouse)

        @classmethod
        def from_config(cls, config, *, timeout=None, network=None):
            # Mirrors the real from_config's network-override contract AND its
            # timeout resolution. Returning None for a config that states one made
            # this double claim "no timeout", which silently dropped a term of the
            # kill switch's timing invariant: `_cmd_live`'s preflight then passed
            # configs that exit 1 in production, so the exit-4 smoke-gate contract
            # these tests assert was never reachable there. The sibling double in
            # cli/test_smoke.py was fixed one round earlier; this one was
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

    def _fake_signed_refuse(name):
        state.rest_calls.append(name)
        raise NotImplementedError(f"the signed double has no {name}")

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

        # The recovery components BIND these at construction. The real client
        # has all of them; this double has no REST behaviour UNLESS a test opts
        # in via
        # ``state.rest_enabled`` — reaching one while opted out is a broken
        # test rather than a scenario — recorded, so that claim is checkable
        # instead of being a comment (2026-08-19 review; opt-in 2026-08-27).
        # ``_fake_signed_refuse`` lives in the FIXTURE scope: a class-body name
        # is invisible inside methods, only the enclosing function's names are.
        def user_fills_by_time(self, start_ms, end_ms):
            if not state.rest_enabled:
                _fake_signed_refuse("user_fills_by_time")
            return []

        def open_orders(self):
            if not state.rest_enabled:
                _fake_signed_refuse("open_orders")
            return list(state.open_orders_result)

        def query_order_by_cloid(self, cloid_hex):
            if not state.rest_enabled:
                _fake_signed_refuse("query_order_by_cloid")
            result = state.order_status.get(cloid_hex, {"status": "unknownOid"})
            if isinstance(result, Exception):
                raise result
            return result

        def schedule_cancel(self, *, cancel_at):
            if not state.rest_enabled:
                _fake_signed_refuse("schedule_cancel")

        def clear_scheduled_cancel(self):
            if not state.rest_enabled:
                _fake_signed_refuse("clear_scheduled_cancel")

        def exchange_time(self):
            # Zero skew against the wall clock, so arm()'s guard passes.
            return datetime.now(timezone.utc)

        def __repr__(self):
            return f"FakeSigned(network={self.network!r})"

    monkeypatch.setattr(sdk_mod, "HyperliquidClient", _FakeClient)
    monkeypatch.setattr(account_mod, "HyperliquidAccount", _FakeAccount)
    monkeypatch.setattr(auth_mod, "verify_agent_authorization", _fake_verify)
    monkeypatch.setattr(signed_mod, "HyperliquidSignedClient", _FakeSigned)
    monkeypatch.setenv(LIVE_ENV, LIVE_KEY)
    return state


def make_live_run(
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


def seed_live_run_with_genesis_subset(
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


class StopBeforeTheLoop(Exception):
    """Sentinel: every kwarg the pins assert is already decided."""


def assert_position_source_binds(source, *, run_id: str, coin: str) -> None:
    """``source`` is ``read_books`` bound over THIS run's store, in order."""
    from contrib.hyperliquid_perp.persistence.db import Database
    from contrib.hyperliquid_perp.runtime.position_facts import read_books

    assert source.func is read_books
    db, bound_run, bound_coin = source.args
    assert isinstance(db, Database)
    assert (bound_run, bound_coin) == (run_id, coin)
