"""The PR 5 live trading loop (§9/§11.4) and its per-tick helpers."""

from __future__ import annotations

import logging
import os
import sys
import time
from decimal import Decimal
from functools import partial
from pathlib import Path

from . import _provider

logger = logging.getLogger(__name__)


# The live loop's tick period. Kept well inside the kill-switch tick budget
# (max_tick_gap 30s) so the §18.2 refresh never lands late even when a tick does
# real work (reconciliation network reads, an SL repair). engine.tick() refreshes
# the switch every call and across its own blocking work; the AI's LLM call runs
# off-thread, but the decision cycle's MARKET-DATA reads (_build_context) run on
# this thread inside driver.pump(), which is why the loop refreshes between the
# two (2026-08-01 lifecycle review).
_LIVE_TICK_SECONDS = 10.0


def _day_baseline_from_exchange(fetch_clearinghouse, kill_switch) -> Decimal:
    """§10.3 rule 1's UTC-day baseline: the EXCHANGE's reconciled accountValue.

    Module level, not a closure inside the loop, so it can be called directly by
    a test — the first version was nested and its only "coverage" was a string
    search for its name, which passed happily while the call site had the wrong
    arity and the whole path was dead.

    The refresh is in ``finally`` for the same reason protection's orderStatus
    read is: this is a bare full-timeout REST call on the single-threaded tick,
    landing between the protection sync and the slice submits, and the call that
    TIMES OUT is both the expensive one and the one that leaves by exception.
    """
    from ..exchanges.hyperliquid.mapper import map_account_snapshot
    from ..live.kill_switch import refresh_across_blocking_work

    try:
        return map_account_snapshot(fetch_clearinghouse()).account_value
    finally:
        refresh_across_blocking_work(kill_switch, what="day-roll baseline read")


def _contain_as_recoverable_safe_mode(safe_mode, *, log_message: str, detail: str) -> None:
    """The live loop's ONE containment idiom: log, then best-effort safe mode.

    Used by every except-branch that must keep the loop alive (tick/pump
    errors, heartbeat blips): the failure is logged with its traceback, the run
    drops into recoverable safe mode so new risk pauses until a clean reconcile
    releases it, and a safe-mode write that ITSELF fails is swallowed too —
    nothing here may end the loop, because the caller's teardown would sweep
    the resting SL/TP off a live position.
    """
    from ..live.safe_mode import REASON_LIVE_TICK_ERROR

    logger.exception(log_message)
    try:
        safe_mode.enter("recoverable", REASON_LIVE_TICK_ERROR, detail=detail)
    except Exception:  # noqa: BLE001 — a safe-mode write miss must not itself end the loop
        logger.exception("failed to enter safe mode after containment (%s)", detail)


def _live_heartbeat(db, run_id: str, *, pid: int, now, safe_mode) -> None:
    """§18.2 lease heartbeat, contained like a tick error.

    ``RunLockError`` (this pid was superseded by a newer process) stays FATAL —
    two writers must never flip-flop the lease. Any OTHER failure here is a
    transient store error (an operator's export/validate holding the SQLite
    lock, say): letting it tear the loop down would run the §18.2 shutdown
    sweep, cancelling the resting SL/TP — a naked position bought with a
    heartbeat blip. Contain it exactly like a tick error instead: log, enter
    recoverable safe mode, retry on the next tick's heartbeat.
    """
    from ..paper import run_lock

    try:
        run_lock.heartbeat_run_lock(db, run_id, pid=pid, now=now)
    except run_lock.RunLockError:
        raise
    except Exception:  # noqa: BLE001 — a transient store error must not strip SL/TP
        _contain_as_recoverable_safe_mode(
            safe_mode,
            log_message=(
                "run-lock heartbeat failed transiently — entering recoverable "
                "safe mode and continuing"
            ),
            detail="run-lock heartbeat write failed (see log)",
        )


def _still_owns_run(db, run_id: str, *, pid: int, now) -> bool:
    """Positively re-verify the lease before the §18.2 shutdown sweep.

    The ``superseded`` flag is absence-of-evidence: it is set only when the
    loop's heartbeat actually RAISED. A Ctrl-C / SIGTERM exits the loop through
    ``except KeyboardInterrupt`` with no heartbeat at all, so after a tick that
    blocked past ``LOCK_STALE_SECONDS`` a successor can already own the run
    while this process still reads ``superseded is False`` — and the sweep then
    cancels the successor's live SL/TP and clears the wallet's dead-man switch.
    Stopping a hung process with SIGTERM is precisely how an operator reaches
    that lane, so this asks the store instead of trusting the flag
    (2026-07-30 concurrency review).

    A transient store failure is not proof of supersession: the lease is most
    likely still ours and skipping the sweep would leave resting orders behind,
    so it is logged and treated as owned. Only ``RunLockError`` gives the run
    away.
    """
    from ..paper import run_lock

    try:
        run_lock.heartbeat_run_lock(db, run_id, pid=pid, now=now)
    except run_lock.RunLockError:
        return False
    except Exception as exc:  # noqa: BLE001 — see docstring: not proof of supersession
        logger.warning(
            "could not re-verify the run lease before the §18.2 sweep (%s: %s) — "
            "proceeding as the owner",
            type(exc).__name__,
            exc,
        )
    return True


def _run_live_loop(
    *,
    cfgs,
    db,
    run_id: str,
    coin: str,
    config: dict,
    live_cfg,
    client,
    signed,
    gate,
    kill_switch,
    safe_mode,
    reconciler,
    processor,
    payload_dir: Path,
    fetch_clearinghouse,
) -> None:
    """The PR 5 live trading loop (§9/§11.4): tick the engine + pump the 4h cycle.

    Builds the execution engine, §17 protection manager, §10 loss guards and the
    off-thread decision worker/driver over the recovery components, then loops
    every ~10s (well inside the kill-switch tick budget). Returns on Ctrl-C /
    SIGTERM (SIGTERM is already mapped to KeyboardInterrupt by the caller); the
    caller's §18.2 shutdown sweep disarms the switch afterwards.

    v1 scope note: this wires a :class:`LiveWsStream` WITHOUT a live socket
    connection — fills are ingested by the reconciler's REST backfill at the
    implemented §12.2 timings (post-fill / 5-minute heartbeat /
    protection-change / pre-shutdown) rather than in real time. The live WS
    connection wiring lands in a later pass (§11 / PR 6).

    A ``RunLockError`` from the lease heartbeat propagates OUT of this function
    by design: the caller exits without the §18.2 sweep (the successor process
    owns the run's orders — see the caller's ``except RunLockError``).
    """

    from ..exchanges.hyperliquid.market_data import HyperliquidMarketData
    from ..live.decision import LiveDecisionDriver, LiveDecisionWorker
    from ..live.engine import LiveExecutionEngine
    from ..live.kill_switch import refresh_across_blocking_work
    from ..live.loss_guards import LossGuards
    from ..live.orders import LiveOrderSubmitter
    from ..live.protection import ProtectionManager
    from ..live.ws_stream import LiveWsStream
    from ..paper.clock import WallClock
    from ..paper.engine import AssetSpec
    from ..paper.market_feed import PortSnapshotProvider
    from ..paper.stops import StopConfig
    from ..persistence import repository as repo

    # ``cfgs`` was validated by _cmd_live's front gate (decided 2026-07-22): a
    # bad risk:/decision:/paper_trading: block is an exit-1 up front, so this
    # function can no longer be reached with an unusable grid and silently
    # skip the loop behind a passing recovery's exit 0.
    risk_cfg, decision_cfg = cfgs
    clock = WallClock()
    market = HyperliquidMarketData(client)
    sz_decimals, schedule = market.get_asset_meta(coin)
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)
    provider = PortSnapshotProvider(market, clock)
    submitter = LiveOrderSubmitter(
        client=signed, gate=gate, db=db, run_id=run_id, payload_dir=payload_dir, clock=clock
    )
    protection = ProtectionManager(
        db=db,
        run_id=run_id,
        coin=coin,
        client=signed,
        gate=gate,
        tick_size=asset.tick_size,
        qty_step=asset.qty_step,
        stop_config=StopConfig(),
        max_slippage_pct=live_cfg.execution.max_slippage_pct,
        protection_config=live_cfg.protection,
        owner_prefix=live_cfg.order_owner_prefix,
        clock=clock,
        kill_switch=kill_switch,
    )

    loss_guards = LossGuards(
        db=db,
        run_id=run_id,
        safety=live_cfg.safety,
        safe_mode=safe_mode,
        # §10.3 rule 1: the UTC-day baseline is the EXCHANGE's reconciled
        # accountValue, fetched once at each day roll (per-tick drawdown
        # evaluation stays on the local ledger — zero extra REST per tick).
        #
        # Rare, but it is a bare full-timeout REST call landing between the
        # protection sync and the slice submits, so it refreshes across itself
        # like every other blocking read on this thread (§18.2).
        #
        # Bound with partial rather than a lambda ON PURPOSE: the first version
        # wrapped a zero-arg closure in ``lambda: helper(kill_switch)``, so every
        # day roll raised TypeError, LossGuards' except-Exception swallowed it,
        # and the baseline silently fell back to the local ledger — defeating the
        # whole point of §10.3 rule 1, with nothing on any surface to say so.
        # partial binds the arguments where they are declared, so an arity
        # mismatch cannot be written here (2026-08-01 incremental review).
        day_baseline_source=partial(_day_baseline_from_exchange, fetch_clearinghouse, kill_switch),
    )
    ledger = repo.get_current_account_state(db.conn, run_id)
    if ledger is not None:
        loss_guards.ensure_settlement_anchor(ledger.wallet_balance, now=clock.now())
    ws_stream = LiveWsStream()
    engine = LiveExecutionEngine(
        db=db,
        run_id=run_id,
        asset=asset,
        live_config=live_cfg,
        risk_config=risk_cfg,
        decision_config=decision_cfg,
        provider=provider,
        submitter=submitter,
        gate=gate,
        kill_switch=kill_switch,
        safe_mode=safe_mode,
        reconciler=reconciler,
        protection=protection,
        loss_guards=loss_guards,
        fill_processor=processor,
        ws_stream=ws_stream,
        fetch_open_orders=signed.open_orders,
        clock=clock,
    )
    # §10.4: a flat reached while the process was down (an SL filled offline,
    # backfilled by startup recovery) never crosses _detect_settlement — score
    # that segment now, before the first tick, so the loss counter cannot merge
    # it into the next one.
    engine.settle_offline_flat()
    decision_provider = _provider._EngineDecisionProvider(
        config,
        risk_cfg=risk_cfg,
        decision_cfg=decision_cfg,
        payload_dir=payload_dir,
        # build_input runs on THIS thread inside driver.pump(); its five market
        # reads are the longest back-to-back REST chain in the system. Refreshing
        # between them keeps the unrefreshed run at the submit chain's 3 instead
        # of 5, which is what lets the operator advisory stay at a ~10s timeout
        # rather than demanding 7.5s from a cycle that cannot retry.
        on_blocking_read=partial(
            refresh_across_blocking_work, kill_switch, what="decision market data"
        ),
    )
    worker = LiveDecisionWorker(provider=decision_provider)
    driver = LiveDecisionDriver(
        db=db,
        run_id=run_id,
        coin=coin,
        asset=asset,
        risk_config=risk_cfg,
        decision_config=decision_cfg,
        engine=engine,
        worker=worker,
        provider=decision_provider,
        clock=clock,
    )
    # §3.1: adopt a prior process's stranded in-progress decision (resume from
    # its stored response, or fail it closed) — without this the deterministic
    # attempt id collides every tick and the driver never decides again.
    adopted = driver.resume_startup()
    if adopted is not None:
        logger.info("decision driver startup adoption: %s", adopted)
    pid = os.getpid()
    print(
        f"live loop started for {run_id!r} ({live_cfg.mode.value}) — Ctrl-C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            tick_started = time.monotonic()
            now = clock.now()
            _live_heartbeat(db, run_id, pid=pid, now=now, safe_mode=safe_mode)
            try:
                tick = engine.tick()
                # The seam between the two blocking halves of one iteration.
                # engine.tick() refreshes at its top and across its own blocking
                # work, but driver.pump() then runs _build_context ON THIS THREAD:
                # constructing the SDK client fetches perp meta, then snapshot,
                # candles, the exchange clock and funding — five back-to-back
                # REST calls on the same
                # network_timeout_s, with no refresh of their own. Without this
                # line the two chains are consecutive, so the run of unrefreshed
                # calls is their SUM, not the max the budget constant assumes
                # (2026-08-01 lifecycle review).
                refresh_across_blocking_work(kill_switch, what="decision pump")
                cycle = driver.pump()
                # Per-tick operator visibility: the live loop is otherwise silent
                # between the startup banner and whatever individual components
                # warn about (the paper loop logs its cycle events likewise). Only
                # a tick that DID something is logged, so an idle 10s cadence stays
                # quiet; the decision-cycle tag is logged whenever it advances.
                if tick.events or tick.slices_submitted or tick.fills_ingested:
                    logger.info(
                        "live tick %s: fills=%d slices=%d protection=%s events=%s",
                        tick.status.value,
                        tick.fills_ingested,
                        tick.slices_submitted,
                        None if tick.protection is None else tick.protection.value,
                        list(tick.events),
                    )
                if cycle is not None:
                    logger.info("live decision cycle: %s", cycle)
            except Exception:  # noqa: BLE001 — a tick error must not tear down the loop or strip SL/TP
                # A single transient tick failure (DB lock, a reconciler read, an
                # unexpected raise) must not propagate out to the caller's §18.2
                # shutdown sweep, which would cancel the resting SL/TP and leave the
                # position naked. Log, enter recoverable safe mode (new orders pause
                # until the next clean reconcile auto-releases), and keep ticking —
                # protection re-attempts next tick. KeyboardInterrupt is a
                # BaseException, so Ctrl-C / SIGTERM still reaches the handler below.
                _contain_as_recoverable_safe_mode(
                    safe_mode,
                    log_message="live tick raised — entering recoverable safe mode and continuing",
                    detail="live tick raised (see log)",
                )
            # The sleep DEDUCTS the tick's own wall time (decided 2026-07-22):
            # ``max_tick_gap_seconds`` is a promise about the wall clock BETWEEN
            # tick() calls, and a fixed sleep would stack on top of a slow tick —
            # a degraded (slow, not dead) network could then push the §18.2
            # refresh past the exchange-side deadline and cancel the resting
            # SL/TP. Deducting keeps the cadence near-constant; the residual
            # risk of a single call outlasting the gap is warned about at
            # startup and owned by PR 6's network-layer rework.
            time.sleep(max(0.0, _LIVE_TICK_SECONDS - (time.monotonic() - tick_started)))
    except KeyboardInterrupt:
        print("\nlive loop stopping — running the §18.2 shutdown sweep...", file=sys.stderr)
        # Let the off-thread AI decision settle so no worker thread writes to the
        # store after teardown begins (§11.4 single writer).
        worker.join(timeout=5.0)
        # A decision that finished during the shutdown window is a paid-for
        # answer only the next pump would have persisted — store its raw
        # response so resume_startup resumes it after restart (§3.1) instead
        # of failing the cycle closed and idling up to 4h. Fully contained:
        # shutdown proceeds on any failure.
        driver.salvage_shutdown()
