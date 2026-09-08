"""The §19.1 reconciliation sweep's production wiring, built in one place.

``live --run-id`` and the ``live-smoke`` restart recovery each construct the
same pair — a :class:`~.fill_backfill.FillBackfiller` and a
:class:`~.reconcile.LiveReconciler` over one signed client, one fill
processor and one kill-switch refresh closure — and until issue #224 each did
it by hand, as two copies of the same two-constructor block. A copy is where the
two drift: issue #169 found the backfiller's ``fetch`` unguarded while the
reconciler's ``fetch_fills`` — the SAME ``user_fills_by_time`` object — was
refused at boot, exactly because each site named it twice.
:func:`build_reconciliation` binds that seam once and hands it to both, so
the two CLIs cannot disagree about what the sweep reads, and the refresh
closure (§18.2) exists once rather than as a per-site definition that one
site could forget.

The components are looked up on their modules at call time rather than bound
at import: the wiring pins (``tests/conftest.py``
``record_reconciliation_sweep_wiring``) record what each CLI builds by
patching the SOURCE modules' names, and those pins are what proves the two
sites still pass the §18.2 hook and the §19.1 evidence directory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    fill_backfill as fill_backfill_mod,
    kill_switch as kill_switch_mod,
    reconcile as reconcile_mod,
)

if TYPE_CHECKING:
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from ..paper.clock import Clock
    from ..persistence.db import Database
    from .fills import LiveFillProcessor
    from .kill_switch import KillSwitchManager
    from .venue_identity import VenueIdentityMonitor

__all__ = ["build_reconciliation"]


def build_reconciliation(
    *,
    signed: HyperliquidSignedClient,
    db: Database,
    run_id: str,
    coin: str,
    fetch_clearinghouse: Callable[[], Any],
    identity: VenueIdentityMonitor,
    processor: LiveFillProcessor,
    kill_switch: KillSwitchManager,
    payload_dir: Path,
    clock: Clock | None = None,
) -> tuple[fill_backfill_mod.FillBackfiller, reconcile_mod.LiveReconciler]:
    """The backfiller and the reconciler a recovery site sweeps with, as one pair.

    ``signed`` supplies the exchange seams — ``open_orders`` and
    ``user_fills_by_time``, the latter bound ONCE and given to both
    components. ``fetch_clearinghouse`` is the site's account read (the
    daemon's and the smoke suite's differ only in which client they close
    over). ``identity`` is the site's one venue-identity monitor (§13.5), and
    ``processor`` the fill processor the WS drain will share, so REST and WS
    converge on one ledger. ``kill_switch`` is the manager both sweeps refresh
    across their blocking work (§18.2); ``payload_dir`` is where the
    reconciler writes its raw-payload evidence. ``clock`` is for tests.
    """

    # §18.2: both components block the single-threaded tick for far longer
    # than one round-trip (a paged backfill, a per-order orderStatus sweep),
    # and the tick's own refresh happens before either runs — so they refresh
    # across their own work (2026-07-31 deadline review). Both production
    # sites arm the switch this refreshes: the daemon's loop and the smoke
    # restart recovery, which runs under the same recovery tick budget and
    # was once left unwired on the mistaken belief that it had no switch.
    def _refresh_across_sweep() -> None:
        kill_switch_mod.refresh_across_blocking_work(kill_switch, what="reconciliation")

    fetch_fills = signed.user_fills_by_time
    backfiller = fill_backfill_mod.FillBackfiller(
        fetch=fetch_fills,
        processor=processor,
        clock=clock,
        refresh_kill_switch=_refresh_across_sweep,
    )
    reconciler = reconcile_mod.LiveReconciler(
        db=db,
        run_id=run_id,
        coin=coin,
        fetch_open_orders=signed.open_orders,
        fetch_clearinghouse=fetch_clearinghouse,
        fetch_fills=fetch_fills,
        backfiller=backfiller,
        payload_dir=payload_dir,
        clock=clock,
        refresh_kill_switch=_refresh_across_sweep,
        identity=identity,  # owns the orderStatus seam (§13.5)
    )
    return backfiller, reconciler
