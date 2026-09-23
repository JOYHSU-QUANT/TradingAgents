"""The live lane's production wiring, built in one place.

``live --run-id`` and ``live-smoke`` each construct the same components over
one signed client — a runtime-armed gate, the §13.5 venue-identity monitor,
the §18 kill switch, the §13 safe-mode machine, the fill processor and the
§19.1 sweep pair — and each did it by hand, as two copies of the same
constructor block: the sweep pair until issue #224, the rest until refactor
plan v2's PR 4. A copy is where the two drift: issue #169 found the
backfiller's ``fetch`` unguarded while the reconciler's ``fetch_fills`` — the
SAME ``user_fills_by_time`` seam — was refused at boot, exactly because each
site named it twice; and wiring only the daemon's copy to the §18.2 hook would
have left the smoke restart tests (15–17) running the sweep unrefreshed
(2026-07-31 deadline review). Each factory here binds its seam once and hands
it to both CLIs, so the two cannot disagree about what a recovery reads, and
the §18.2 refresh closure exists once rather than as a per-site definition
that one site could forget.

The components are looked up on their modules at call time rather than bound
at import: the wiring pins (``tests/conftest.py``
``record_reconciliation_sweep_wiring``, and the constructor recorders in
``tests/cli/``) record what each CLI builds by patching the SOURCE modules'
names, and those pins are what proves the two sites still pass the §18.2 hook
and the §19.1 evidence directory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..exchanges.hyperliquid import signed_client as signed_client_mod
from . import (
    fill_backfill as fill_backfill_mod,
    fills as fills_mod,
    kill_switch as kill_switch_mod,
    order_gate as order_gate_mod,
    reconcile as reconcile_mod,
    safe_mode as safe_mode_mod,
    startup as startup_mod,
    venue_identity as venue_identity_mod,
)

if TYPE_CHECKING:
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from ..persistence.db import Database
    from ..ports import Clock
    from .config import LiveConfig
    from .fill_backfill import FillBackfiller
    from .fills import LiveFillProcessor
    from .kill_switch import KillSwitchManager
    from .order_gate import RealOrderGate
    from .reconcile import LiveReconciler
    from .safe_mode import SafeModeManager
    from .startup import StartupResult
    from .venue_identity import VenueIdentityMonitor

__all__ = ["LiveSession", "build_live_session", "build_reconciliation", "build_signed_client"]


def build_signed_client(
    live_cfg: LiveConfig,
    *,
    agent_key: str,
    wallet_address: str,
    timeout: float | None,
    agent_authorized: bool,
) -> tuple[RealOrderGate, HyperliquidSignedClient]:
    """A fresh-from-config §4.1 gate and the signed client bound to it.

    ``agent_authorized`` is the only gate flag set at construction — True
    only once :func:`~.authorization.verify_agent_authorization` passed for
    this key and wallet (§6.1); every other flag starts fail-closed and is
    proven at runtime (§19.1). ``timeout`` is the read client's.
    """
    gate = order_gate_mod.RealOrderGate.from_config(live_cfg)
    gate.agent_authorized = agent_authorized
    signed = signed_client_mod.HyperliquidSignedClient(
        live_cfg.network,
        agent_key,
        wallet_address=wallet_address,
        gate=gate,
        timeout=timeout,
    )
    return gate, signed


@dataclass(frozen=True)
class LiveSession:
    """The components one live-mode process runs its §19.1 recovery over.

    Built by :func:`build_live_session`. The daemon reads the components back
    off its session for the ``--loop`` hand-off and the §18.2 shutdown sweep;
    the smoke suite builds one per recovery it runs (the pre-flight and
    restart tests 15–17).
    """

    signed: HyperliquidSignedClient
    gate: RealOrderGate
    db: Database
    run_id: str
    payload_dir: Path
    fetch_clearinghouse: Callable[[], Any]
    identity: VenueIdentityMonitor
    kill_switch: KillSwitchManager
    safe_mode: SafeModeManager
    processor: LiveFillProcessor
    backfiller: FillBackfiller
    reconciler: LiveReconciler

    def run_startup_recovery(self) -> StartupResult:
        """Steps 5–16 of §19.1 over this session's components (arms the switch)."""
        return startup_mod.run_startup_recovery(
            db=self.db,
            run_id=self.run_id,
            client=self.signed,
            fetch_clearinghouse=self.fetch_clearinghouse,
            gate=self.gate,
            kill_switch=self.kill_switch,
            reconciler=self.reconciler,
            safe_mode=self.safe_mode,
            payload_dir=self.payload_dir,
        )


def build_live_session(
    *,
    signed: HyperliquidSignedClient,
    gate: RealOrderGate,
    db: Database,
    run_id: str,
    coin: str,
    live_cfg: LiveConfig,
    fetch_clearinghouse: Callable[[], Any],
    payload_dir: Path,
    max_tick_gap_seconds: float,
    suite_authored: bool = False,
) -> LiveSession:
    """The recovery components, wired the one way both CLIs wire them.

    ``signed`` and ``gate`` come from :func:`build_signed_client` with the
    §6.1 flag set; ``fetch_clearinghouse`` is the site's account read.
    ``max_tick_gap_seconds`` is the number the caller's timing preflight
    proved, and ``signed.timeout`` the failed-attempt term: both reach the
    switch explicitly, never probed off the client. ``suite_authored`` marks
    the switch's rows as the smoke suite's (see
    :class:`~.kill_switch.KillSwitchManager`). One venue-identity monitor per
    session (§13.5), shared by the switch and the reconciler (the daemon hands
    the same one to the loop's protection manager); the processor carries the
    signed wallet so its envelope-identity check is armed.
    """
    identity = venue_identity_mod.VenueIdentityMonitor(
        query_order_by_cloid=signed.query_order_by_cloid,
        db=db,
        run_id=run_id,
        symbol=coin,
        payload_dir=payload_dir,
    )
    kill_switch = kill_switch_mod.KillSwitchManager(
        client=signed,
        gate=gate,
        db=db,
        run_id=run_id,
        config=live_cfg.kill_switch,
        max_tick_gap_seconds=max_tick_gap_seconds,
        network_timeout_s=signed.timeout,
        payload_dir=payload_dir,
        suite_authored=suite_authored,
        identity=identity,
    )
    safe_mode = safe_mode_mod.SafeModeManager(db=db, run_id=run_id, gate=gate)
    processor = fills_mod.LiveFillProcessor(
        db=db,
        run_id=run_id,
        payload_dir=payload_dir,
        wallet_address=signed.wallet_address,
    )
    backfiller, reconciler = build_reconciliation(
        signed=signed,
        db=db,
        run_id=run_id,
        coin=coin,
        fetch_clearinghouse=fetch_clearinghouse,
        identity=identity,
        processor=processor,
        kill_switch=kill_switch,
        payload_dir=payload_dir,
    )
    return LiveSession(
        signed=signed,
        gate=gate,
        db=db,
        run_id=run_id,
        payload_dir=payload_dir,
        fetch_clearinghouse=fetch_clearinghouse,
        identity=identity,
        kill_switch=kill_switch,
        safe_mode=safe_mode,
        processor=processor,
        backfiller=backfiller,
        reconciler=reconciler,
    )


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
    # which an in-review draft had left unwired on the mistaken belief that
    # it had no switch.
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
