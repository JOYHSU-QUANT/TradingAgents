"""The PR 5 live trading loop (§9/§11.4) and its per-tick helpers."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from pathlib import Path

from . import _provider
from ._common import (
    announce_engine_config_protection_only,
    holds_live_work,
    note_stranded_attempt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProtectionOnlyExit:
    """How a protection-only live loop ended (issue #268).

    Returned by :func:`_run_live_loop` instead of ``None`` when the decision
    provider could not be built over a live position and the loop ran
    tick-only. ``cause`` is the ``EngineConfigError`` text; ``settled`` says
    the loop ended ITSELF because the position closed and nothing was left
    to protect (the paper loop's settle-exit, exit 1 there), as opposed to
    Ctrl-C / SIGTERM. The caller prints the matching exit line and picks the
    exit code — the loop never chooses one.
    """

    cause: str
    settled: bool


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


def _contain_wedged_adoption_as_manual_safe_mode(safe_mode, exc) -> None:
    """Contain a §3.1 adoption wedge, latching MANUAL instead of recoverable.

    The severity is the whole point (issue #205). ``AdoptionWedgedError`` says
    the adoption raise will repeat identically on every re-read, so the driver
    has stopped retrying and will start no decision cycle again: the run is
    alive, watched and reconciling, but permanently unable to decide. A
    RECOVERABLE latch auto-releases on the next clean reconciliation pass
    (§13.4) — which this fault does not affect — so the run would keep flipping
    between "safe" and "stuck" while nothing durable recorded either. A MANUAL
    one survives restarts, blocks risk-adding orders until a human releases it
    (§13.6), and is the trace ``validate`` and ``safe-mode --status`` report.

    Contained, never propagated, for the same reason the recoverable idiom is:
    ending the loop runs the caller's §18.2 teardown, and the supervisor's
    restart meets the same deterministic raise until ``StartLimitBurst`` gives
    up — leaving the position with its resting SL/TP and no process refreshing
    the kill switch or repairing protection at all (issue #180).

    That buys THIS process, not every future one: a standing manual latch makes
    the §19.1 verdict fail, so the next ``live --loop`` never enters this
    function and exits 4 instead. Which is why the runbook's remedy is fix the
    rows, RELEASE, then restart — and why ``pump`` re-attempts adoption as soon
    as the latch stops standing, so a running daemon needs no restart at all.
    """
    from ..live.safe_mode import REASON_ADOPTION_WEDGED, SAFE_MODE_MANUAL

    logger.exception(
        "decision driver startup adoption cannot complete and retrying will not "
        "change that — latching MANUAL safe mode and continuing to watch the "
        "position; no decision cycle will start until a human clears the run's "
        "in-progress attempts and releases the latch (`safe-mode --release`)"
    )
    try:
        if not safe_mode.enter(SAFE_MODE_MANUAL, REASON_ADOPTION_WEDGED, detail=str(exc)):
            # A MANUAL latch for a DIFFERENT reason was already standing, so
            # the current-state trio keeps that first reason and this one lives
            # only in safe_mode_events. Say so: `safe-mode --status` and
            # validate will both name the OTHER reason, and releasing THAT one
            # clears safe mode while this wedge is still in force.
            logger.error(
                "a manual safe-mode latch was already standing, so the wedged adoption "
                "is recorded only as an added reason (%s) in safe_mode_events — "
                "`safe-mode --status` will name the earlier reason, and releasing it "
                "does NOT unwedge the decision driver",
                REASON_ADOPTION_WEDGED,
            )
    except Exception:  # noqa: BLE001 — a safe-mode write miss must not itself end the loop
        # Not the end of it: pump re-checks whether the latch is actually
        # standing and re-raises until one lands, so a missed write here cannot
        # leave the wedge with no durable record at all (issue #205).
        logger.exception("failed to latch manual safe mode after a wedged startup adoption")


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
    identity,
) -> ProtectionOnlyExit | None:
    """The PR 5 live trading loop (§9/§11.4): tick the engine + pump the 4h cycle.

    ``identity`` is the caller's shared :class:`VenueIdentityMonitor` — the
    same instance its kill switch and reconciler already probe through — so
    the §17 protection manager built here feeds the same §13.5 streak
    (issue #80).

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

    Returns ``None`` after an ordinary Ctrl-C / SIGTERM, or a
    :class:`ProtectionOnlyExit` when the loop ran in protection-only mode
    (issue #268): the decision provider's construction raised an
    ``EngineConfigError`` — a failed engine import, or a rejected env knob
    such as ``TRADINGAGENTS_MAX_TOKENS``, ``TRADINGAGENTS_LLM_MAX_RETRIES``
    or ``TRADINGAGENTS_TEMPERATURE`` — over a LIVE position. Exiting then
    would hand the caller's §18.2 sweep a passing verdict and a live
    position, and the sweep would cancel the resting SL/TP: the position
    naked, and under ``Restart=`` a crash-loop into the same refusal. So the
    loop starts anyway, tick-only (kill-switch refresh, reconciliation, SL/TP
    protection; NO decision pump, so no new cycle), and ends itself once the
    position is closed. Flat, the same ``EngineConfigError`` propagates OUT
    (nothing to guard): the caller's handler makes it a named exit 1, the
    paper lane's rule. The caller's §18.2 sweep treats a protection-only
    run as unclean, so a stop leaves the resting SL/TP standing for the
    fixed restart to adopt.
    """

    from ..engine_bridge import EngineConfigError
    from ..exchanges.hyperliquid.market_data import HyperliquidMarketData
    from ..live.cancel import cancel_bot_order_with_evidence
    from ..live.decision import AdoptionWedgedError, LiveDecisionDriver, LiveDecisionWorker
    from ..live.engine import LiveExecutionEngine
    from ..live.kill_switch import refresh_across_blocking_work
    from ..live.loss_guards import LossGuards
    from ..live.orders import LiveOrderSubmitter
    from ..live.protection import ProtectionManager
    from ..live.ws_stream import LiveWsStream
    from ..paper.clock import WallClock
    from ..paper.engine import AssetSpec
    from ..paper.market_feed import PortSnapshotProvider
    from ..paper.position_facts import read_books
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
        identity=identity,
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

    def cancel_bot_order(*, cloid_hex: str, cloid_logical: str, cancel_reason: str) -> None:
        # The maker slice's cancel seam (§9.2.1): the evidence protocol the
        # §18.2 / §19.3 sweeps run for a bot-owned cancel, bound to this run.
        cancel_bot_order_with_evidence(
            db=db,
            client=signed,
            run_id=run_id,
            payload_dir=payload_dir,
            clock=clock,
            coin=coin,
            cloid_hex=cloid_hex,
            cloid_logical=cloid_logical,
            cancel_reason=cancel_reason,
        )

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
        fetch_top_of_book=market.get_top_of_book,
        cancel_order=cancel_bot_order,
    )
    # §10.4: a flat reached while the process was down (an SL filled offline,
    # backfilled by startup recovery) never crosses _detect_settlement — score
    # that segment now, before the first tick, so the loss counter cannot merge
    # it into the next one.
    engine.settle_offline_flat()

    # The provider's construction is the process's first tradingagents import
    # and runs the bridge's startup gates, so this is where an
    # operator-fixable environment fault surfaces — as an ``EngineConfigError``
    # (the base: a bad env knob and a failed import are the same class of
    # mistake, and a new sibling cause needs no new handler). Over a live
    # position it must NOT propagate: see the docstring (issue #268). This
    # mirrors ``cli/paper.py``'s healthy-restart rule through the same
    # ``holds_live_work`` decision (the live engine's own answer: position,
    # active leg or pending flip; unreadable counts as live).
    protection_only: ProtectionOnlyExit | None = None
    try:
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
            # The live store keeps the same books the paper daemon reads (the
            # reconciler mirrors the exchange onto them), so the prompt's
            # ``Position:`` section comes from the same read on both lanes.
            position_source=partial(read_books, db, run_id, coin),
        )
    except EngineConfigError as exc:
        if not holds_live_work(engine):
            # Flat: nothing to guard, so the named refusal stands — the
            # caller's ``except EngineConfigError`` prints it and exits 1.
            raise
        protection_only = ProtectionOnlyExit(cause=str(exc), settled=False)
        announce_engine_config_protection_only(
            exc,
            where=f"for live run {run_id}",
            alive="SL/TP protection, the kill-switch refresh and reconciliation",
        )
        worker = driver = None
        # Only a healthy restart's driver may adopt a stranded attempt (§3.1);
        # protection-only never builds the driver, so it stays open until then.
        note_stranded_attempt(
            db, run_id, never="builds the decision driver", restart_will="adopts it"
        )
    else:
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
    try:
        adopted = None if driver is None else driver.resume_startup()
    except AdoptionWedgedError as exc:
        # The SAME containment (the loop must watch the position either way),
        # at the severity the fault deserves: this one cannot heal by being
        # retried, so a recoverable latch would auto-release into an unchanged
        # wedge. The driver has already latched its own retry off — pump idles
        # from here — and the stranded in_progress row is what `validate`
        # reports (issue #205).
        _contain_wedged_adoption_as_manual_safe_mode(safe_mode, exc)
    except Exception:  # noqa: BLE001 — adoption must not exit before the loop watches the position
        # Contained like a tick error rather than propagating (issue #180
        # review). The motivating failure is the fail-closed record's OWN
        # write meeting a locked store — an operator's export/validate — and
        # exiting here hands the supervisor a restart that may meet the same
        # lock, with the real position and its resting SL/TP unwatched in
        # between and systemd's StartLimitBurst counting down. Inside the
        # loop both branches heal on their own: a poisoned re-parse has
        # already armed the driver's pending_fail lane, which retries just
        # that write on each pump, and an unanswered attempt is re-adopted by
        # pump (nothing is armed there, so without that retry containment
        # would wedge the driver — issue #180 review round 2). Safe mode
        # pauses new risk meanwhile, and the position is watched throughout.
        _contain_as_recoverable_safe_mode(
            safe_mode,
            log_message=(
                "decision driver startup adoption raised — entering recoverable "
                "safe mode and starting the loop anyway"
            ),
            detail="startup adoption raised (see log)",
        )
    else:
        if adopted is not None:
            logger.info("decision driver startup adoption: %s", adopted)
    pid = os.getpid()
    print(
        f"live loop started for {run_id!r} ({live_cfg.mode.value})"
        + (" in protection-only mode" if driver is None else "")
        + " — Ctrl-C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            tick_started = time.monotonic()
            now = clock.now()
            _live_heartbeat(db, run_id, pid=pid, now=now, safe_mode=safe_mode)
            # Which half of the iteration a raise came from. The containment
            # below is shared by both, and its ``detail`` is DURABLE — safe mode
            # stores it, and `safe-mode --status` and validate read it back hours
            # later with no traceback beside it. Before this marker that record
            # said "live tick raised" whatever had failed, so a pump failure was
            # filed against the tick (issue #238 review).
            phase = "tick"
            try:
                # The tick logs its own per-tick activity summary from a finally,
                # so no raise below can eat the record (see _log_tick_activity).
                engine.tick()
                if driver is None:
                    # Protection-only (issue #268): no pump, ever. The mode
                    # exists for a live position; once it is closed no later
                    # iteration can do anything, and a zombie would hold the
                    # lease and refresh the switch for days. End the loop
                    # loud instead — the caller's §18.2 sweep then runs over a
                    # flat book, and the exit code tells the supervisor
                    # (the paper loop's settle-exit rule). A raw store read
                    # under the loop's containment, on purpose: a raise here
                    # (a locked store) is contained like any loop-body fault
                    # — logged, recoverable safe mode latched (auto-releases
                    # on the next clean reconcile; nothing to block, this
                    # mode pumps nothing) — so a lock that PERSISTS is
                    # visible to `safe-mode --status` and `validate`, not
                    # only as a ~10s ERROR storm in the log (#270 review).
                    # The startup read is the one that must never latch
                    # (holds_live_work, above): its answer decides the lane.
                    # Its own phase, so the record does not say "tick" (#238).
                    phase = "protection-only settle check"
                    # ``driver`` is None exactly when ``protection_only`` was
                    # set (the except/else above assign them together); the
                    # assert states that for the type checker.
                    assert protection_only is not None
                    if not engine.has_active_work():
                        logger.error(
                            "protection-only live run %s has nothing left to "
                            "protect — exiting",
                            run_id,
                        )
                        return replace(protection_only, settled=True)
                else:
                    phase = "decision pump"
                    # The seam between the two blocking halves of one iteration.
                    # engine.tick() refreshes at its top and across its own
                    # blocking work, but driver.pump() then runs _build_context
                    # ON THIS THREAD: constructing the SDK client fetches perp
                    # meta, then snapshot, candles, the exchange clock and
                    # funding — five back-to-back REST calls on the same
                    # network_timeout_s, with no refresh of their own. Without
                    # this line the two chains are consecutive, so the run of
                    # unrefreshed calls is their SUM, not the max the budget
                    # constant assumes (2026-08-01 lifecycle review).
                    refresh_across_blocking_work(kill_switch, what="decision pump")
                    cycle = driver.pump()
                    if cycle is not None:
                        logger.info("live decision cycle: %s", cycle)
            except AdoptionWedgedError as exc:
                # pump retries adoption when the BOOT call was contained, so a
                # wedge can surface here too: the boot failure was the locked
                # store, and the read that finally succeeded found a broken
                # state machine. Same manual latch, same reasoning as the boot
                # branch above.
                #
                # NOT once per process. pump re-attempts adoption whenever no
                # manual latch stands — a human released one, or this very
                # containment's write missed — so a still-broken row raises
                # again and lands here once per such episode. That is the
                # mechanism by which a missed latch write is eventually
                # written, and it is safe to re-enter: ``enter`` is idempotent
                # under repetition (issue #205).
                _contain_wedged_adoption_as_manual_safe_mode(safe_mode, exc)
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
                    log_message=(
                        f"live {phase} raised — entering recoverable safe mode and continuing"
                    ),
                    detail=f"live {phase} raised (see log)",
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
        if driver is not None:
            assert worker is not None  # built together with the driver
            # Let the off-thread AI decision settle so no worker thread writes
            # to the store after teardown begins (§11.4 single writer).
            worker.join(timeout=5.0)
            # A decision that finished during the shutdown window is a paid-for
            # answer only the next pump would have persisted — store its raw
            # response so resume_startup resumes it after restart (§3.1)
            # instead of failing the cycle closed and idling up to 4h. Fully
            # contained: shutdown proceeds on any failure.
            driver.salvage_shutdown()
    return protection_only
