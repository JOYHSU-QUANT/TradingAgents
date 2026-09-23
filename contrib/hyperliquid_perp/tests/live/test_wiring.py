"""``live.wiring`` — the one place both live-mode CLIs build their components (issue #224).

The CLI drives pin what each command builds and hands its components
(``tests/cli/test_cli.py``, ``tests/cli/test_smoke.py``, over the shared
recorder in ``tests/conftest.py``); this file pins what the factories
themselves bind.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid import signed_client as signed_client_mod
from contrib.hyperliquid_perp.live import (
    fill_backfill as fill_backfill_mod,
    fills as fills_mod,
    kill_switch as ks_mod,
    reconcile as reconcile_mod,
    safe_mode as safe_mode_mod,
    startup as startup_mod,
    venue_identity as venue_identity_mod,
)
from contrib.hyperliquid_perp.live.config import ExecutionMode, LiveConfig
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.wiring import (
    LiveSession,
    build_live_session,
    build_reconciliation,
    build_signed_client,
)
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.runtime.clock import ManualClock

from ..conftest import record_constructor_kwargs

_NOW = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)
_WALLET = "0x" + "aa" * 20
_KEY = "0x" + "11" * 32


class _Signed:
    """The exchange reads the components take off the signed client, as METHODS.

    Methods, not attributes, on purpose: a bound method is a new object on
    every attribute access, which is exactly why "bind the seam once" is a
    fact the factory has to establish rather than one a caller gets for free.
    """

    wallet_address = _WALLET
    timeout = 8.0

    def user_fills_by_time(self, start_ms, end_ms):
        return []

    def open_orders(self):
        return []

    def query_order_by_cloid(self, cloid_hex):
        return {"status": "unknownOid"}


def _live_cfg() -> LiveConfig:
    return LiveConfig.from_dict(
        {"mode": "testnet_live", "network": "testnet", "allow_real_orders": True}
    )


def _build(tmp_path, **over):
    kwargs = {
        "signed": _Signed(),
        "db": Database(":memory:"),
        "run_id": "r",
        "coin": "BTC",
        "fetch_clearinghouse": lambda: {},
        # Stored, never probed here; shaped like the monitor so the guard passes.
        "identity": SimpleNamespace(probe=lambda *a, **k: None, latched=False, latched_site=None),
        "processor": None,
        "kill_switch": SimpleNamespace(),
        "payload_dir": tmp_path / "payloads",
        "clock": ManualClock(_NOW),
        **over,
    }
    return kwargs, build_reconciliation(**kwargs)


def test_the_pair_shares_one_fetch_seam_one_refresh_hook_and_the_sites_inputs(
    monkeypatch, tmp_path
):
    # ``user_fills_by_time`` is read off the client ONCE and given to both —
    # identity, not equality: two accesses would be two bound methods, and a
    # guard on one says nothing about the other (issue #169's finding, which
    # this factory exists to make structurally impossible).
    kwargs, (backfiller, reconciler) = _build(tmp_path)
    assert backfiller._fetch is reconciler._fetch_fills
    assert reconciler._backfiller is backfiller
    # The site's inputs reach the component that reads them.
    assert reconciler._identity is kwargs["identity"]
    assert reconciler._payload_dir == kwargs["payload_dir"]
    assert reconciler._fetch_clearinghouse is kwargs["fetch_clearinghouse"]
    assert backfiller._processor is kwargs["processor"]
    # One §18.2 refresh closure for both, routed to the switch the site armed
    # through the helper the recorder in ``tests/conftest.py`` patches.
    seen: list[tuple[object, str]] = []
    monkeypatch.setattr(
        ks_mod, "refresh_across_blocking_work", lambda switch, *, what: seen.append((switch, what))
    )
    assert backfiller._refresh_kill_switch is reconciler._refresh_kill_switch
    backfiller._refresh_kill_switch()
    assert seen == [(kwargs["kill_switch"], "reconciliation")]


def test_the_factory_resolves_the_components_on_their_modules_at_call_time(monkeypatch, tmp_path):
    # The CLI wiring pins record what each site builds by patching the SOURCE
    # modules' names (``record_reconciliation_sweep_wiring``); a factory that
    # bound ``FillBackfiller`` at import would leave those pins recording
    # nothing while the drives still passed. Pinned so the module docstring's
    # claim stays a fact.
    built: list[object] = []
    real = fill_backfill_mod.FillBackfiller

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            built.append(self)

    monkeypatch.setattr(fill_backfill_mod, "FillBackfiller", _Recording)
    _, (backfiller, _reconciler) = _build(tmp_path)
    assert built == [backfiller]
    assert type(backfiller) is _Recording


# ---------------------------------------------------------------------------
# build_signed_client — the §4.1 gate and the signed client bound to it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("authorized", [True, False])
def test_the_signed_client_is_bound_to_a_fresh_gate_carrying_the_one_flag(monkeypatch, authorized):
    # The real client builds an SDK ``Exchange`` (network): patched on its
    # module, as the CLI drives patch it.
    seen: list[tuple[tuple, dict]] = []

    class _FakeSigned:
        def __init__(self, network, agent_key, *, wallet_address, gate, timeout):
            seen.append(
                (
                    (network, agent_key),
                    {"wallet_address": wallet_address, "gate": gate, "timeout": timeout},
                )
            )

    monkeypatch.setattr(signed_client_mod, "HyperliquidSignedClient", _FakeSigned)
    live_cfg = _live_cfg()

    gate, signed = build_signed_client(
        live_cfg, agent_key=_KEY, wallet_address=_WALLET, timeout=8.0, agent_authorized=authorized
    )

    assert type(signed) is _FakeSigned
    assert seen == [(("testnet", _KEY), {"wallet_address": _WALLET, "gate": gate, "timeout": 8.0})]
    # Fresh from config: the config's wire conditions, and every runtime flag
    # still fail-closed — only the §6.1 flag is the caller's to set.
    assert type(gate) is RealOrderGate
    assert gate.allow_real_orders is True
    assert gate.mode is ExecutionMode.TESTNET_LIVE
    assert gate.allowed_symbols == live_cfg.safety.allowed_symbols
    assert gate.agent_authorized is authorized
    assert gate.startup_reconciliation_passed is False
    assert gate.kill_switch_active is False
    assert gate.state_reconciled is False


# ---------------------------------------------------------------------------
# build_live_session — the recovery components, wired once for both CLIs
# ---------------------------------------------------------------------------

_COMPONENTS = (
    (venue_identity_mod, "VenueIdentityMonitor"),
    (ks_mod, "KillSwitchManager"),
    (safe_mode_mod, "SafeModeManager"),
    (fills_mod, "LiveFillProcessor"),
    (fill_backfill_mod, "FillBackfiller"),
    (reconcile_mod, "LiveReconciler"),
)


def _session(tmp_path, monkeypatch, **over) -> tuple[dict, LiveSession, dict[str, list]]:
    """Build a session over REAL components, recording each constructor's kwargs."""
    record: dict[str, list] = {name: [] for _, name in _COMPONENTS}
    for module, name in _COMPONENTS:
        record_constructor_kwargs(monkeypatch, module, name, record[name])

    gate = RealOrderGate.from_config(_live_cfg())
    gate.agent_authorized = True
    kwargs = {
        "signed": _Signed(),
        "gate": gate,
        "db": Database(":memory:"),
        "run_id": "r",
        "coin": "BTC",
        "live_cfg": _live_cfg(),
        "fetch_clearinghouse": lambda: {},
        "payload_dir": tmp_path / "payloads",
        # 30 (interval) + 20 (tick) + 8 (timeout) + 8 (backoff = min(timeout, 15))
        # + 20 (retry's tick) = 86 < 120 (``kill_switch_timing_violation``).
        "max_tick_gap_seconds": 20.0,
        **over,
    }
    return kwargs, build_live_session(**kwargs), record


def test_the_session_shares_one_identity_monitor_one_processor_and_one_switch(
    tmp_path, monkeypatch
):
    kwargs, session, record = _session(tmp_path, monkeypatch)
    # Every component is the one its recorder saw built (resolved on its
    # module at call time — the ``live.wiring`` docstring's claim).
    assert {name: len(sink) for name, sink in record.items()} == {
        name: 1 for _, name in _COMPONENTS
    }
    # §13.5: ONE monitor, reaching the switch's disarm cross-check and the
    # reconciler's per-order probes.
    assert session.kill_switch._identity is session.identity
    assert session.reconciler._identity is session.identity
    assert record["VenueIdentityMonitor"] == [
        {
            "query_order_by_cloid": kwargs["signed"].query_order_by_cloid,
            "db": kwargs["db"],
            "run_id": "r",
            "symbol": "BTC",
            "payload_dir": kwargs["payload_dir"],
        }
    ]
    # The sweep pair is the ``build_reconciliation`` pair over the session's
    # processor, and the daemon-vs-suite marker defaults to the daemon's.
    assert session.reconciler._backfiller is session.backfiller
    assert session.backfiller._processor is session.processor
    assert session.kill_switch._suite_authored is False
    # The two timing terms and the wallet arrive as the caller's numbers, not
    # as anything probed off the client.
    (switch_kwargs,) = record["KillSwitchManager"]
    assert switch_kwargs["max_tick_gap_seconds"] == 20.0
    assert switch_kwargs["network_timeout_s"] == 8.0
    assert switch_kwargs["config"] is kwargs["live_cfg"].kill_switch
    assert switch_kwargs["client"] is kwargs["signed"]
    assert switch_kwargs["gate"] is kwargs["gate"]
    assert record["LiveFillProcessor"] == [
        {
            "db": kwargs["db"],
            "run_id": "r",
            "payload_dir": kwargs["payload_dir"],
            "wallet_address": _WALLET,
        }
    ]
    assert record["SafeModeManager"] == [
        {"db": kwargs["db"], "run_id": "r", "gate": kwargs["gate"]}
    ]
    # The session carries the site's own inputs unchanged.
    assert session.signed is kwargs["signed"]
    assert session.gate is kwargs["gate"]
    assert session.db is kwargs["db"]
    assert session.fetch_clearinghouse is kwargs["fetch_clearinghouse"]
    assert session.payload_dir == kwargs["payload_dir"]
    assert session.run_id == "r"


def test_suite_authored_reaches_the_switch_and_nothing_else(tmp_path, monkeypatch):
    _, session, record = _session(tmp_path, monkeypatch, suite_authored=True)
    assert session.kill_switch._suite_authored is True
    (switch_kwargs,) = record["KillSwitchManager"]
    assert switch_kwargs["suite_authored"] is True
    for _, name in _COMPONENTS:
        if name == "KillSwitchManager":
            continue
        (other_kwargs,) = record[name]
        assert "suite_authored" not in other_kwargs, name


def test_run_startup_recovery_hands_the_sessions_own_components_to_startup(tmp_path, monkeypatch):
    # Patched on ``live.startup``, as the CLI drives patch it.
    seen: list[dict] = []
    monkeypatch.setattr(
        startup_mod, "run_startup_recovery", lambda **kwargs: seen.append(kwargs) or "verdict"
    )
    _, session, _ = _session(tmp_path, monkeypatch)

    assert session.run_startup_recovery() == "verdict"
    assert seen == [
        {
            "db": session.db,
            "run_id": "r",
            "client": session.signed,
            "fetch_clearinghouse": session.fetch_clearinghouse,
            "gate": session.gate,
            "kill_switch": session.kill_switch,
            "reconciler": session.reconciler,
            "safe_mode": session.safe_mode,
            "payload_dir": tmp_path / "payloads",
        }
    ]
