"""``live.wiring`` — the one place both recovery sites build their sweep pair (issue #224).

The two CLI sites pin that they GO THROUGH the factory and hand it the right
inputs (``tests/cli/test_cli.py``, ``tests/cli/test_smoke.py``, over the
shared recorder in ``tests/conftest.py``); this file pins what the factory
itself binds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from contrib.hyperliquid_perp.live import fill_backfill as fill_backfill_mod, kill_switch as ks_mod
from contrib.hyperliquid_perp.live.wiring import build_reconciliation
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence.db import Database

_NOW = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)


class _Signed:
    """The two exchange reads the pair takes off the signed client, as METHODS.

    Methods, not attributes, on purpose: a bound method is a new object on
    every attribute access, which is exactly why "bind the seam once" is a
    fact the factory has to establish rather than one a caller gets for free.
    """

    def user_fills_by_time(self, start_ms, end_ms):
        return []

    def open_orders(self):
        return []


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
