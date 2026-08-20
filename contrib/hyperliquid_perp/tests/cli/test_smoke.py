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
an integrity failure) at the CLI layer, over live/test_validation.py's ``_healthy``
store builder.
"""

from __future__ import annotations

import itertools
import os
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

from ..conftest import (
    assert_paired_sweep_refreshes,
    assert_payload_dir,
    echo_order_status_cloid,
    record_reconciliation_sweep_wiring,
)
from ..live.test_startup import _clearinghouse
from ..live.test_validation import _healthy
from .test_cli import _seed_live_run_with_genesis_subset as _seed_genesis_run

_D = Decimal
_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)

_SMOKE_WALLET = "0x" + "aa" * 20
_SMOKE_KEY = "0x" + "11" * 32
_SMOKE_ENV = "HYPERLIQUID_AGENT_KEY_TESTNET"
_AGENT_ADDR = "0x" + "cc" * 20
# One real suite runs this many §19.1 recoveries: the per-invocation
# pre-flight (smoke.py's order-placing selection) plus restart tests 15-17.
_SMOKE_RECOVERIES = 4


def _smoke_yaml(
    tmp_path,
    *,
    wallet: str | None = _SMOKE_WALLET,
    allow_real_orders: bool = True,
    schedule_cancel_seconds: int | None = None,
    refresh_interval_seconds: int | None = None,
    # RUNBOOK §1.5's value, and the DEFAULT here on purpose: the 30s top-level
    # default is deliberately illegal in live (30+30+30+15+30 = 135 >= 120), so a
    # helper that omitted it produced configs no operator could actually run. It
    # went unnoticed only while the fake client under-reported its timeout as None
    # (2026-08-01 round-15 review).
    network_timeout_s: float | None = 8,
):
    """A minimal live config that passes every live-smoke config gate."""
    path = tmp_path / "smoke-cfg.yaml"
    text = ""
    if wallet is not None:
        text += f'wallet_address: "{wallet}"\n'
    if network_timeout_s is not None:
        text += f"network_timeout_s: {network_timeout_s}\n"
    text += "risk:\n  leverage: 1\n  margin_mode: cross\n  max_target_margin_pct: 60\n"
    text += "live:\n  mode: testnet_live\n  network: testnet\n"
    if allow_real_orders:
        text += "  allow_real_orders: true\n"
    if schedule_cancel_seconds is not None or refresh_interval_seconds is not None:
        text += "  kill_switch:\n"
        if schedule_cancel_seconds is not None:
            text += f"    schedule_cancel_seconds: {schedule_cancel_seconds}\n"
        if refresh_interval_seconds is not None:
            text += f"    refresh_interval_seconds: {refresh_interval_seconds}\n"
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
            # Resolves the timeout the way the real client does — from the
            # explicit kwarg, else ``network_timeout_s``, else the bounded
            # default. Returning None here made the double claim "no timeout"
            # for a config that states one, which is exactly the value the kill
            # switch's timing invariant counts.
            if timeout is None:
                raw = config.get("network_timeout_s")
                timeout = float(raw) if raw is not None else 30.0
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
            # Mirrors the real signed client, which keeps its resolved timeout:
            # the CLI hands it to KillSwitchManager as the failed-attempt term of
            # the refresh-timing invariant.
            self.timeout = timeout

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
            # The fake books every IOC (full-fill echo), so test 4's cloid
            # resolves — the real Info hit shape from live/orders.py.
            return echo_order_status_cloid(
                {"status": "order", "order": {"order": {"oid": 1}, "status": "filled"}},
                cloid_hex,
            )

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


def test_the_cli_hands_the_manager_the_clients_own_timeout(tmp_path, monkeypatch, smoke_seams):
    """The wiring that ENDS the two-halves-disagreeing bug, pinned at the CLI.

    ``network_timeout_s`` became an explicit constructor argument so the degraded
    four-term check is a caller's stated choice rather than a getattr accident.
    That made ``None`` a legal argument — so the regression shape is now a CALL
    SITE passing None, and forcing every construction to ``network_timeout_s=None``
    left 1965 of 1966 tests green, the one failure being the constructor's own
    unit test. Neither production construction site was exercised by anything
    (2026-08-01 round-15 mutation probe).
    """
    from contrib.hyperliquid_perp.live import kill_switch as ks_mod

    seen: list[object] = []
    real = ks_mod.KillSwitchManager

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            seen.append(kwargs.get("network_timeout_s"))
            super().__init__(**kwargs)

    monkeypatch.setattr(ks_mod, "KillSwitchManager", _Recording)
    # An explicit, RUNBOOK §1.5-shaped timeout: relying on the default would let
    # the assertion pass against None, which is the very value being guarded.
    cfg = _smoke_yaml(tmp_path, network_timeout_s=8)
    dbp = _seed_genesis_run(tmp_path, cfg)
    assert cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 0
    assert seen, "no KillSwitchManager was constructed — the pin proves nothing"
    # The number the failed attempt would actually burn, not None: the whole point
    # is that the constructor and the CLI preflight now read the SAME value.
    assert all(value == 8 for value in seen), seen


def test_the_cli_hands_the_fill_processor_the_signed_wallet(tmp_path, monkeypatch, smoke_seams):
    """The envelope-identity check is armed by wiring, so the wiring is pinned.

    ``LiveFillProcessor.wallet_address`` is optional (identity-agnostic tests
    construct bare processors), which makes the cli call sites the load-bearing
    part - exactly the ``network_timeout_s`` shape pinned above: a refactor
    that rebuilds the construction and drops the argument disarms the check
    with every other test still green.
    """
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
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    assert cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 0
    assert seen, "no LiveFillProcessor was constructed - the pin proves nothing"
    assert all(value == _SMOKE_WALLET for value in seen), seen


def _drive_the_smoke_recoveries(tmp_path):
    """Run the whole real live-smoke suite; return the store path it used.

    Four recoveries land in one run — the per-invocation pre-flight plus restart
    tests 15-17 — and the count is asserted so that a suite which quietly stops
    running three of them cannot leave the "paired per recovery" claim below
    standing on a single construction.
    """
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    assert cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 0
    return dbp


def test_the_smoke_recovery_wires_the_reconciliation_sweeps_switch_refresh(
    tmp_path, monkeypatch, smoke_seams
):
    """The daemon pin's sibling, at the OTHER construction site (issue #45).

    ``_smoke_startup_recovery`` builds the same sweep components ``live`` does,
    against a switch it ARMS under the same recovery tick budget — so the same
    two optional kwargs decide whether a paged backfill or an orderStatus sweep
    can let the scheduled cancel lapse and wipe the resting probe mid-suite.
    Wiring only the live lane was the actual 2026-07-31 finding, and nothing
    since then observed either site.
    """
    record = record_reconciliation_sweep_wiring(monkeypatch)
    _drive_the_smoke_recoveries(tmp_path)

    assert len(record.switches) == _SMOKE_RECOVERIES, record.switches
    assert_paired_sweep_refreshes(record, owner="smoke recovery")


def test_the_smoke_recovery_gives_the_reconciler_a_payload_dir(tmp_path, monkeypatch, smoke_seams):
    """The evidence term at the smoke site, split out for the reason its daemon
    sibling is: a failure here is about the §19.1 audit trail, not §18.2."""
    record = record_reconciliation_sweep_wiring(monkeypatch)
    dbp = _drive_the_smoke_recoveries(tmp_path)

    assert len(record.reconcilers) == _SMOKE_RECOVERIES, record.reconcilers
    for reconciler in record.reconcilers:
        assert_payload_dir(reconciler, dbp, run_id="r1")


def test_every_row_a_smoke_run_writes_is_marked_including_the_managers(tmp_path, smoke_seams):
    """The finding that made round 15's exclusion a no-op on real runs.

    The suite does not only write rows through its own runner: it builds a REAL
    KillSwitchManager for the pre-flight recovery and restart tests 15-17, and on
    a real testnet suite — minutes of wall clock per test against a 30s refresh
    interval — that manager's ``tick()`` emits ``kill_switch_refreshed`` rows.
    Those were unmarked, so they bought §20.3 sample credit exactly as before and
    the user's "smoke must not satisfy the floor" decision was never in force.
    Only an offline clock that never elapses hid it, which is why this asserts
    over EVERY row the run produced rather than over the runner's own three
    (2026-08-01 round-16 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import is_suite_authored

    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    assert cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)]) == 0
    with Database(dbp) as db:
        rows = [(r["event_type"], r["detail"]) for r in repo.iter_kill_switch_events(db.conn, "r1")]
    assert rows, "the suite wrote no kill-switch rows at all"
    unmarked = [(event, detail) for event, detail in rows if not is_suite_authored(detail)]
    assert not unmarked, f"rows written during live-smoke but not marked: {unmarked}"


def test_the_runbook_quotes_the_literals_the_code_prints(tmp_path):
    # RUNBOOK §20.3 shows operators the literal token so they can read the event
    # log by hand. Nothing tied the doc to the constant, so renaming the constant
    # left the suite green and the runbook quietly wrong (round-16 probe).
    from pathlib import Path as _Path

    from contrib.hyperliquid_perp.live.kill_switch import _SUITE_AUTHORED_TOKEN
    from contrib.hyperliquid_perp.live.validation import _NO_DAEMON_ROWS_RENDER

    # Resolved from __file__, not the cwd: this is the only test in the suite
    # that reads a doc, and a cwd-relative path fails whenever pytest is
    # invoked from anywhere but the repo root (2026-08-01 round-17 probe).
    # parents[2] = the package root (this file lives in tests/cli/).
    docs = _Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK-live.md"
    runbook = docs.read_text(encoding="utf-8")
    assert _SUITE_AUTHORED_TOKEN in runbook
    # Same tie for the other literal §20.3 quotes: the summary's clean-shutdown
    # value for a run with no daemon rows. It could drift in either place with
    # the suite green (2026-08-01 round-18 mutation probe).
    assert _NO_DAEMON_ROWS_RENDER in runbook


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


# -- the sibling-run refusal: the lease is per-run, the damage is per-wallet ---


def _write_lease(db_path, run_id: str, *, pid: int, heartbeat_at: datetime) -> None:
    """Plant a run lease directly, the way a live/paper process would hold one."""
    with Database(db_path) as db, db.transaction() as conn:
        repo.upsert_scheduler_state(conn, run_id, lock_pid=pid, lock_heartbeat_at=heartbeat_at)


def test_conflicting_run_lease_reports_a_fresh_sibling(tmp_path):
    # acquire_run_lock would let this suite straight through: it only asks about
    # THIS run_id. But the kill-switch arm/clear, updateLeverage and the §19.3
    # sweep are per-WALLET, and a sibling run in the same store shares the
    # wallet — so the sibling has to be found by a separate read.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease

    now = datetime.now(timezone.utc)
    dbp = tmp_path / "sib.db"
    _write_lease(dbp, "sibling-run", pid=4321, heartbeat_at=now - timedelta(seconds=5))
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") == ("sibling-run", 4321)


def test_conflicting_run_lease_ignores_a_stale_sibling_at_the_boundary(tmp_path):
    # The boundary is the whole point of the freshness test: a lease exactly
    # LOCK_STALE_SECONDS old is the one acquire_run_lock itself treats as
    # takeable (the holder is presumed dead), so refusing on it would ground the
    # smoke suite on the corpse of a crashed run forever.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease
    from contrib.hyperliquid_perp.paper.run_lock import LOCK_STALE_SECONDS

    now = datetime.now(timezone.utc)
    dbp = tmp_path / "sib.db"
    _write_lease(
        dbp, "sibling-run", pid=4321, heartbeat_at=now - timedelta(seconds=LOCK_STALE_SECONDS)
    )
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") is None


def _set_genesis_network(db_path, run_id: str, network: str) -> None:
    """Point a run's genesis ``live.network`` at a given exchange."""
    import json

    with Database(db_path) as db, db.transaction() as conn:
        row = repo.get_run(conn, run_id)
        genesis = json.loads(row["config_json"]) if row["config_json"] else {}
        genesis.setdefault("live", {})["network"] = network
        conn.execute(
            "UPDATE runs SET config_json = ? WHERE run_id = ?",
            (json.dumps(genesis, ensure_ascii=False), run_id),
        )


def test_a_sibling_on_the_other_network_is_not_a_conflict(tmp_path):
    # RUNBOOK-live §7.3 puts the mainnet_tiny run in the SAME live_trading.db as
    # the testnet run. Those are different exchanges with different accounts — a
    # testnet scheduleCancel cannot reach a mainnet order. Keying the refusal on
    # "same store" told the operator to stop a REAL-MONEY run in order to smoke
    # a testnet one (exit check 2026-07-31), so the network decides.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease

    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)  # r1, testnet (the suite's own run)
    _seed_genesis_run(tmp_path, cfg, run_id="mainnet-BTC")
    _set_genesis_network(dbp, "mainnet-BTC", "mainnet")
    _write_lease(dbp, "mainnet-BTC", pid=4321, heartbeat_at=datetime.now(timezone.utc))
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") is None

    # Negative control: the SAME sibling on this network is still refused, so
    # the pass above is the network test doing its job, not the lookup failing.
    _set_genesis_network(dbp, "mainnet-BTC", "testnet")
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") == ("mainnet-BTC", 4321)


def test_a_sibling_whose_network_differs_only_in_case_is_still_a_conflict(tmp_path):
    # LiveConfig reads network case-insensitively, so "Testnet" and "testnet"
    # are the same exchange and the same wallet. Comparing the raw genesis
    # strings made them look like different networks and sent this guard
    # fail-OPEN — the one direction its docstring forbids, since the cost of a
    # false pass is a stripped dead-man switch on a live wallet.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease

    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)  # r1, "testnet"
    _seed_genesis_run(tmp_path, cfg, run_id="sibling-run")
    _set_genesis_network(dbp, "sibling-run", "  TestNet  ")
    _write_lease(dbp, "sibling-run", pid=4321, heartbeat_at=datetime.now(timezone.utc))
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") == ("sibling-run", 4321)


def test_a_paper_sibling_is_not_a_conflict(tmp_path):
    # A paper run signs nothing and holds no wallet: it can neither be harmed by
    # an account-wide action nor take one. It also has no live: block at all, so
    # before this it read as an unreadable network and was refused fail-closed —
    # telling the operator to stop a run to protect a dead-man cover it never had.
    import json

    from contrib.hyperliquid_perp.cli import _conflicting_run_lease
    from contrib.hyperliquid_perp.paper import accounting
    from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    with Database(dbp) as db:
        accounting.initialize_run(
            db,
            run_id="paper-BTC",
            mode="paper",
            initial_balance_usdc=Decimal(1000),
            schema_version=SCHEMA_VERSION,
            config_json=json.dumps({"coin": "BTC"}),
        )
    _write_lease(dbp, "paper-BTC", pid=4321, heartbeat_at=datetime.now(timezone.utc))
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") is None


def test_a_sibling_whose_genesis_network_is_unreadable_is_treated_as_a_conflict(tmp_path):
    # Fail-closed on a corrupt genesis: an unreadable network is not evidence of
    # safety, and the cost of a false refusal is one operator message against a
    # stripped dead-man switch on a live wallet.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease

    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    _seed_genesis_run(tmp_path, cfg, run_id="sibling-run")
    with Database(dbp) as db, db.transaction() as conn:
        conn.execute("UPDATE runs SET config_json = ? WHERE run_id = ?", ("[]", "sibling-run"))
    _write_lease(dbp, "sibling-run", pid=4321, heartbeat_at=datetime.now(timezone.utc))
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") == ("sibling-run", 4321)


def test_conflicting_run_lease_never_reports_the_runs_own_lease(tmp_path):
    # Negative control, and the one that matters most: an ordinary `live-smoke`
    # re-run against a store where this run's own lease row is still populated
    # must not refuse itself — that would break every invocation.
    from contrib.hyperliquid_perp.cli import _conflicting_run_lease

    now = datetime.now(timezone.utc)
    dbp = tmp_path / "sib.db"
    _write_lease(dbp, "r1", pid=4321, heartbeat_at=now)
    with Database(dbp) as db:
        assert _conflicting_run_lease(db, "r1") is None


def test_live_smoke_real_run_refuses_while_a_sibling_run_is_live(tmp_path, capsys, smoke_seams):
    # End to end through the CLI: the suite must stop BEFORE it arms the
    # account-wide kill switch or sweeps the wallet's resting orders, and the
    # message has to name the run the operator must go and stop.
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    _write_lease(dbp, "sibling-run", pid=4321, heartbeat_at=datetime.now(timezone.utc))
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "'sibling-run'" in err
    assert "4321" in err
    assert "ACCOUNT-wide" in err
    # Nothing on the wire: no leverage write, no kill-switch arm, no sweep.
    assert smoke_seams.schedule_calls == []
    assert smoke_seams.clear_calls == 0
    with Database(dbp) as db:
        assert repo.latest_smoke_test_results(db.conn, "r1") == {}


def test_live_smoke_real_run_not_refused_by_own_lease_or_a_stale_sibling(
    tmp_path, capsys, smoke_seams
):
    # The negative control for the guard above. Two rows that MUST NOT refuse:
    # this run's own lease (a resume re-taking a lease it already holds — every
    # ordinary invocation looks like this once the row exists) and a sibling
    # whose holder is long dead. Refusing on either would make the guard a
    # permanent outage instead of a concurrency check.
    from contrib.hyperliquid_perp.paper.run_lock import LOCK_STALE_SECONDS

    now = datetime.now(timezone.utc)
    cfg = _smoke_yaml(tmp_path)
    dbp = _seed_genesis_run(tmp_path, cfg)
    _write_lease(dbp, "r1", pid=os.getpid(), heartbeat_at=now)
    _write_lease(
        dbp, "dead-sibling", pid=4321, heartbeat_at=now - timedelta(seconds=LOCK_STALE_SECONDS)
    )
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 0
    assert "smoke_gate_passed: yes" in capsys.readouterr().out


# -- the §18 kill-switch deadline comes from the run's own config -------------


def test_real_smoke_session_takes_the_kill_switch_deadline_from_config(tmp_path, smoke_seams):
    # The suite refreshes the switch before each test to ctx.now() +
    # kill_switch_deadline. Inheriting SmokeContext's 120s dataclass default
    # silently NARROWED a longer configured cover to 120s for the whole suite,
    # so a test making a few round-trips on a slow network could let the switch
    # fire and cancel the resting probe — the exact failure the refresh exists
    # to prevent. 600 is deliberately off-default: a probe asserting 120 would
    # have passed against the bug.
    from contrib.hyperliquid_perp.cli import _build_smoke_session
    from contrib.hyperliquid_perp.live.smoke import SmokeContext

    assert SmokeContext.__dataclass_fields__["kill_switch_deadline"].default == timedelta(
        seconds=120
    )
    cfg = _smoke_yaml(tmp_path, schedule_cancel_seconds=600)
    dbp = _seed_genesis_run(tmp_path, cfg)
    args = SimpleNamespace(config=str(cfg), db=str(dbp), run_id="r1", dry_run=False)
    with Database(dbp) as db:
        session = _build_smoke_session(args, db)
        assert not isinstance(session, int)  # not an exit code: the build succeeded
        assert session.kill_switch_deadline == timedelta(seconds=600)


def test_a_short_configured_cover_is_floored_not_inherited(tmp_path, smoke_seams):
    # The other direction of the same wiring. The config invariant only demands
    # schedule_cancel > 5s and >= 2x refresh_interval, both judged against
    # `live`'s 30s tick model — but the suite refreshes once per TEST, and a
    # test is an unbounded place/poll/cancel round-trip. So 40s/5s is legal for
    # the daemon while handing the suite a cover NARROWER than the 120s it had
    # before the value was wired from config at all: the switch fires mid-test
    # and cancels the resting probe, recorded as "the exchange refused" — which
    # sends the operator to check config and market state, not the clock.
    from contrib.hyperliquid_perp.cli import _build_smoke_session

    # 80/5, not 40/5: since the timing invariant started counting the failed
    # attempt's own timeout and the retry's own tick wait, 40/5 is no longer legal
    # for the daemon (5 + 30 + 8 + 2.5 + 30 = 75.5 > 40) and the preflight refuses
    # it before this floor is ever reached. It was briefly 70, which only passed
    # while the fake client under-reported its timeout as None and the sum lost
    # its 8s term. 80 is the nearest cover that keeps the ORIGINAL point: legal
    # for the daemon, still narrower than the 120s the suite guarantees itself
    # (2026-08-01 round-15 review).
    cfg = _smoke_yaml(tmp_path, schedule_cancel_seconds=80, refresh_interval_seconds=5)
    dbp = _seed_genesis_run(tmp_path, cfg)
    args = SimpleNamespace(config=str(cfg), db=str(dbp), run_id="r1", dry_run=False)
    with Database(dbp) as db:
        session = _build_smoke_session(args, db)
        assert not isinstance(session, int)
        assert session.kill_switch_deadline == timedelta(seconds=120)


def test_smoke_refuses_a_violating_kill_switch_timing_with_exit_1(tmp_path, capsys, smoke_seams):
    # Exit-code parity with `live`. The same bad config used to reach
    # KillSwitchManager's constructor, raise, be contained as a
    # SmokePreflightError and surface as exit 4 — which RUNBOOK §5 answers with
    # "the run state is unclean, check safe-mode --status" (the wrong
    # investigation) and §8 tells a supervisor to branch its restart policy on.
    # Legal to LiveConfig (60 >= 2x30) but violating once the caller's 30s
    # worst-case tick gap is added: 30 + 30 is not strictly inside 60. This is
    # exactly the band only the preflight catches — a config-level violation
    # would already exit 1 from load_config and prove nothing.
    cfg = _smoke_yaml(tmp_path, schedule_cancel_seconds=60, refresh_interval_seconds=30)
    dbp = _seed_genesis_run(tmp_path, cfg)
    rc = cli_main(["live-smoke", "--config", str(cfg), "--run-id", "r1", "--db", str(dbp)])
    assert rc == 1
    err = capsys.readouterr().err
    # And it names the two knobs, exactly as `live` does.
    assert "schedule_cancel_seconds" in err
    assert "refresh_interval_seconds" in err


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


def test_validate_failed_smoke_row_exits_4_not_5(tmp_path, capsys):
    # Decision 2026-07-29: a FAILED/errored smoke row is curable by one
    # `live-smoke --only <key>` re-run, so validate maps it to exit 4 ("keep
    # running / re-run smoke"), keeping exit 5 for the integrity conditions
    # (the test above pins that side of the split).
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_smoke_test_result(
            conn,
            run_id="r",
            test_number=8,
            test_key="stop_loss_create",
            test_name="SL create",
            status="failed",
            executed_at=_T0,
        )
    db.close()
    rc = cli_main(["validate", "--run-id", "r", "--db", str(tmp_path / "live.db")])
    out = capsys.readouterr().out
    assert rc == 4
    assert "live_ready: no" in out
    assert any(
        "FAILED (exchange refused)" in line
        for line in out.splitlines()
        if line.startswith("shortfall:")
    )
