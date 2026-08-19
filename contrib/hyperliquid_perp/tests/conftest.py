"""Shared pytest helpers for the Hyperliquid perp tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id as derive_attempt_id

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Keep the whole suite immune to a developer's real repo-root ``.env``.

    Both CLI entry points call ``load_dotenv_files()`` as their first act; under
    pytest that would inject every variable of a real ``.env`` into the process,
    and ``load_dotenv``'s ``os.environ`` writes happen outside monkeypatch's
    snapshot, so they outlive the test that triggered them. Neutralize the
    entry-point bindings suite-wide; the loader itself stays testable through
    ``config.load_dotenv_files`` (untouched), and the entry-point ordering tests
    opt back in by re-binding the real function.
    """
    from contrib.hyperliquid_perp import cli, main

    monkeypatch.setattr(cli, "load_dotenv_files", lambda: None)
    monkeypatch.setattr(main, "load_dotenv_files", lambda: None)


def _load(name: str):
    with (_FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def meta_and_asset_ctxs():
    return _load("meta_and_asset_ctxs.json")


@pytest.fixture
def candle_snapshot():
    return _load("candle_snapshot.json")


@pytest.fixture
def funding_history():
    return _load("funding_history.json")


@pytest.fixture
def clearinghouse_state():
    return _load("clearinghouse_state.json")


def record_reconciliation_sweep_wiring(monkeypatch):
    """Record what a recovery site builds its §18.2 sweep components with.

    Both production sites — ``_live_startup_recovery`` (the daemon) and
    ``_smoke_startup_recovery`` — build a ``KillSwitchManager``, a
    ``FillBackfiller`` and a ``LiveReconciler`` over one refresh closure, and
    the kwargs that arm the refresh are optional on both components. The pins
    live in two modules because only one command reaches each site; the
    plumbing is identical, so it lives here and the assertions — which differ,
    one recovery against one per restart test — stay with the tests.

    Returns a namespace of ``switches`` / ``refreshes`` / ``backfillers`` /
    ``reconcilers``, in construction order.
    """
    from contrib.hyperliquid_perp.live import (
        fill_backfill as fb_mod,
        kill_switch as ks_mod,
        reconcile as rec_mod,
    )

    record = SimpleNamespace(switches=[], refreshes=[], backfillers=[], reconcilers=[])
    real_switch = ks_mod.KillSwitchManager
    real_refresh = ks_mod.refresh_across_blocking_work

    class _RecordingSwitch(real_switch):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            record.switches.append(self)
            super().__init__(**kwargs)

    def _recording_refresh(switch, *, what):
        record.refreshes.append((switch, what))
        # Delegate rather than replace: the smoke suite's own refreshes run
        # through this name too, and swallowing them would change what the
        # drive proves.
        real_refresh(switch, what=what)

    def _record_kwargs(module, name, sink):
        real = getattr(module, name)

        class _Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, **kwargs):
                sink.append(kwargs)
                super().__init__(**kwargs)

        monkeypatch.setattr(module, name, _Recording)

    # cli.py imports all of these inside the command functions, so the seam is
    # the SOURCE module — the closures the CLI builds pick the patched ones up.
    monkeypatch.setattr(ks_mod, "KillSwitchManager", _RecordingSwitch)
    monkeypatch.setattr(ks_mod, "refresh_across_blocking_work", _recording_refresh)
    _record_kwargs(fb_mod, "FillBackfiller", record.backfillers)
    _record_kwargs(rec_mod, "LiveReconciler", record.reconcilers)
    return record


def assert_sweep_refreshes(record, switch, kwargs, *, label):
    """The recorded hook must refresh THAT switch — not merely exist.

    ``lambda: None`` satisfies a not-None assertion while refreshing nothing,
    which is the same no-op the missing kwarg produces.
    """
    hook = kwargs.get("refresh_kill_switch")
    assert hook is not None, f"the {label} sweep refreshes nothing (§18.2)"
    record.refreshes.clear()
    hook()
    assert record.refreshes == [(switch, "reconciliation")], (label, record.refreshes)


def synthetic_bar(**over):
    """A minimal well-formed candleSnapshot bar: BTC/1m, t/T in the past."""
    base = {
        "t": 1000,
        "T": 1999,
        "s": "BTC",
        "i": "1m",
        "o": "1",
        "h": "2",
        "l": "1",
        "c": "1",
        "v": "1",
    }
    base.update(over)
    return base


def echo_order_status_cloid(payload, cloid_hex):
    """Echo the queried cloid into a canned orderStatus payload, like the venue.

    ``parse_order_status`` requires the inner order to echo the cloid it was
    queried by (identity check, 2026-08-17). The real Info endpoint always
    echoes it; the fakes' canned payloads predate the check and are often
    served against cloids the test never sees (a manager-derived hex), so each
    fake routes its answer through here to model the well-behaved venue. A
    payload that ALREADY carries a cloid is returned untouched — that is how a
    test scripts a mismatch. Copies rather than mutates: several canned
    payloads are shared module constants.
    """
    if (
        isinstance(payload, dict)
        and payload.get("status") == "order"
        and isinstance(payload.get("order"), dict)
        and isinstance(payload["order"].get("order"), dict)
        and "cloid" not in payload["order"]["order"]
    ):
        inner = {**payload["order"]["order"], "cloid": cloid_hex}
        return {**payload, "order": {**payload["order"], "order": inner}}
    return payload


def insert_decision_attempts(db, statuses, *, run_id="r", start: datetime, mode="paper"):
    """Insert one terminal decision_attempts row per status, on the 4h grid."""
    with db.transaction() as conn:
        for i, status in enumerate(statuses):
            scheduled = start + timedelta(hours=4 * i)
            repo.insert_decision_attempt(
                conn,
                decision_attempt_id=derive_attempt_id(run_id, scheduled),
                timestamp=scheduled,
                mode=mode,
                run_id=run_id,
                scheduled_at=scheduled,
                attempt_count=1,
                status=status,
            )
