"""Dead man's switch + shutdown cancel (phase3-spec §18, PR 2 scope).

:class:`KillSwitchManager` owns the §18.2 lifecycle:

1. :meth:`arm` — immediately after exchange client init, ``scheduleCancel``
   at now + ``schedule_cancel_seconds``; the exchange cancels this wallet's
   open orders at that deadline if the process dies first.
2. :meth:`tick` / :meth:`refresh` — push the deadline back every
   ``refresh_interval_seconds``. A refresh failure records
   ``kill_switch_refresh_failed``, drops ``gate.kill_switch_active`` (no new
   §4.1 order can pass), and raises :attr:`stop_new_orders` — the flag PR 4's
   safe-mode state machine consumes (``on_refresh_failed: safe_mode``); the
   full state machine is NOT in this PR. The flag is sticky AND enforced at
   the gate: while it is up, a successful refresh re-arms the exchange-side
   switch but leaves ``gate.kill_switch_active`` down — §13.4 requires a full
   reconciliation pass (PR 4) before trading resumes, not one lucky refresh.
   :meth:`release_safe_mode` is that one door.

   Every refresh (and :meth:`shutdown`) first asks whether the deadline has
   already lapsed — i.e. whether the exchange ALREADY fired the switch and
   swept the wallet while nobody was ticking us. That records
   ``kill_switch_cancel_triggered`` and latches, rather than quietly re-arming
   over a book that is no longer there.
3. :meth:`shutdown` — cancel bot-owned open orders (§18.2 rule 5). Bot-owned
   is decided per §19.3: the exchange order's cloid reverse-looked-up in
   ``cloid_registry``; anything else is left alone (rule: never manage
   non-bot-owned orders) and reported in the completed event's detail. Every
   cancel round-trip is recorded in ``live_order_attempts`` (intent before
   the wire, outcome after — the same §8.3/§16.5 evidence protocol orders
   use), and a successful cancel patches the local orders row. A fully clean
   sweep then disarms the exchange-side scheduleCancel (§18.2 rule 6): the
   trigger is wallet-wide, so leaving it armed would cancel the very non-bot
   orders the sweep skipped. Any failure keeps it armed as the backstop.

Every state change lands in ``kill_switch_events`` (§18.5). The manager only
exists on runs where real orders are enabled — an unarmed (paper / gate-check)
run has no exchange orders for a dead man's switch to protect.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.cloid import LIVE_ORDER_ROLES
from ..persistence.db import Database
from .cancel import cancel_bot_order_with_evidence
from .config import KillSwitchConfig
from .order_gate import RealOrderGate
from .orders import local_status_for_exchange_status, parse_order_status

__all__ = [
    "KillSwitchManager",
    "kill_switch_timing_violation",
    "network_timeout_warning",
    "refresh_across_blocking_work",
    "sl_repair_delay_warning",
]

logger = logging.getLogger(__name__)

# Jitter budget for refresh_due(). When a caller ticks at the same period as
# refresh_interval_seconds (the intended 30s/30s wiring), a bare
# `elapsed >= interval` test skips every other refresh over mere milliseconds of
# scheduling drift — tick() runs a hair later in the cycle than the schedule it
# is compared against. Half a second dwarfs that drift while staying well inside
# the interval, so it only ever pulls a refresh slightly EARLIER — never pushes
# one past the deadline it exists to renew, and never turns a genuinely early
# call into a refresh. The backstop for a gap LARGER than this is the constructor
# invariant (refresh_interval + worst-case tick gap < schedule_cancel); the config
# guard cannot be one, because it cannot see the caller's tick gap at all.
_REFRESH_DUE_SLACK_S = 0.5

# Wall clocks routinely slew backwards by milliseconds under NTP discipline.
# Treating that as "the clock moved" would log a warning and burn a schedule
# call on every tick, so only a step LARGER than this counts. Well below any
# real step (seconds at minimum) and far inside the smallest sane
# schedule_cancel window.
_CLOCK_BACKWARDS_TOLERANCE_S = 1.0

# How far this host's clock may differ from the exchange's before arming is
# refused. scheduleCancel takes an ABSOLUTE deadline that we compute from the
# LOCAL clock, and _detect_expired_deadline measures the lapse against that same
# local clock — so the two are self-consistent and drift is INVISIBLE here while
# silently resizing the real, exchange-side protection window. Generous next to
# a round-trip's latency (sub-second), tight next to the smallest sane
# schedule_cancel window (MIN_SCHEDULE_CANCEL_SECONDS = 5s upward, 120s in the
# example config): a skew this large already means time sync is broken, not slow.
_MAX_CLOCK_SKEW_S = 5.0

# NOTE on breadth: the refresh/shutdown paths deliberately catch ``Exception``,
# not just ExchangeError — a round-trip can also fail in the persistence layer
# (sqlite lock/IntegrityError, an invariant ValueError) or at the client's own
# bound gate (LiveOrderGateRejected, NOT an ExchangeError), and any of those
# escaping would abort the sweep mid-loop (§18.2 rule 5: every remaining order
# still gets its cancel attempt; §18.4 rule 1: the loop keeps running) or kill
# the live loop (§18.2 rule 3 / §18.4 rules 2–4: a refresh failure stops new
# orders but must not stop monitoring and protection). The error type travels in
# the recorded event, so a code bug is loud in the audit trail, never re-raised
# into the safety path.


def kill_switch_timing_violation(
    config: KillSwitchConfig, max_tick_gap_seconds: float
) -> str | None:
    """The constructor's refresh-timing invariant as a checkable message.

    THE canonical account of the invariant (other sites point here). Worst
    case between two refreshes is ``refresh_interval + the tick gap`` — the
    tick after the interval elapses can land a whole gap late — and that must
    stay strictly inside ``schedule_cancel``, or the dead man's switch fires
    during normal operation and cancels every order on the wallet. The config
    layer's own guard (``schedule_cancel >= 2 × refresh``) is only the
    special case of a caller ticking exactly at the interval: it cannot see
    the caller's tick gap at all.

    Returns the violation text, or None when the timing is sound. ONE
    definition, used by the constructor (which raises on it) and by the CLI's
    live preflight — the preflight exists because the constructor runs only
    AFTER ``--create`` has written the run row and taken the run lock, so a
    violating config would otherwise surface as a late ValueError → generic
    exit 2 outside the documented 0/4/1 contract, with side effects already
    on disk (decided 2026-07-17).
    """
    if max_tick_gap_seconds <= 0:
        return f"max_tick_gap_seconds must be > 0, got {max_tick_gap_seconds}"
    worst_case_gap = config.refresh_interval_seconds + max_tick_gap_seconds
    if worst_case_gap >= config.schedule_cancel_seconds:
        return (
            f"the kill switch cannot be refreshed in time: a refresh may land "
            f"{worst_case_gap}s apart (refresh_interval "
            f"{config.refresh_interval_seconds}s + max_tick_gap "
            f"{max_tick_gap_seconds}s), but the scheduled cancel fires after "
            f"{config.schedule_cancel_seconds}s — the dead man's switch would "
            "cancel every order on the wallet during normal operation"
        )
    return None


# The longest run of BACK-TO-BACK REST calls a tick can make with no
# :func:`refresh_across_blocking_work` between them — the decision cycle's
# ``_build_context``, which ``driver.pump()`` runs ON THE LOOP THREAD:
# constructing the SDK client fetches perp meta, then the market snapshot, the
# candle window and the funding history. Four calls, each riding the full
# ``network_timeout_s`` (``HyperliquidClient.from_config`` resolves its timeout
# from that very key, so these are not on some smaller budget of their own).
#
# The order-submission chain in ``orders.submit_ioc_limit`` is three — the §8.3
# pre-check recovery probe (which falls THROUGH when it cannot resolve the cloid,
# rather than returning), the place itself, and the duplicate-ack recovery probe.
# Everything else refreshes ACROSS its blocking work: protection's repair ladder
# and its orderStatus confirmations, the reconcile legs and their per-order
# loops, BOTH page ladders (the fill backfill's and the reconcile fill
# cross-check's own inline one), and the day-roll baseline read. A stretch there
# is one call long however many calls the loop makes in total.
#
# This number is a MAXIMUM OVER CHAINS, so every seam between two chains has to
# refresh or the truth becomes their SUM. Twice now that was the bug:
#   - the first pass wired only the backfiller's ladder and left the
#     cross-check's identical one untouched, making the real maximum 20
#     (DEFAULT_MAX_PAGES) while this said 3;
#   - the second left ``engine.tick()`` and ``driver.pump()`` adjacent with
#     nothing between them, so the submit chain and the build_context chain ran
#     back to back for a real maximum of 7 (2026-08-01 lifecycle review).
# Any new REST loop MUST refresh per iteration, and any new pair of blocking
# calls MUST refresh between them, or this number is a lie. The test beside it
# derives the count from a simulated iteration rather than restating it.
_MAX_UNREFRESHED_REST_CALLS = 4


def network_timeout_warning(timeout: float | None, max_tick_gap_seconds: float) -> str | None:
    """The §18.2 residual-risk advisory as a checkable message (PR 5, decided
    2026-07-22 "soft mitigation").

    Sister of :func:`kill_switch_timing_violation`, advisory rather than
    enforced: nothing ties the per-request REST timeout to the caller's
    ``max_tick_gap_seconds`` promise, and REST calls riding their full timeout
    on a degraded (slow, not dead) network can stretch the wall gap past the
    promise — the dead man's switch then cancels the resting SL/TP while the
    process is still alive (protection re-covers on the next healthy tick, an
    unprotected window).

    Budgets ``_MAX_UNREFRESHED_REST_CALLS``, not one. The single-call form
    called a 10s timeout sound against a 30s gap while one unrefreshed chain
    could spend 30s inside it — the arithmetic contradicted the very sentence
    this docstring opened with ("a tick makes several sequential REST calls") and
    under-reported the one number the operator can actually turn (2026-07-31
    deadline review).

    Returns the warning text when the timeout cannot keep the promise (or is
    unbounded), None when it fits. The hard construction-time invariant is
    deferred to the network-layer rework.
    """
    budget = max_tick_gap_seconds / _MAX_UNREFRESHED_REST_CALLS
    if timeout is not None and timeout * _MAX_UNREFRESHED_REST_CALLS < max_tick_gap_seconds:
        return None
    return (
        f"network_timeout_s ({timeout}) leaves no room under the kill switch's "
        f"max tick gap ({max_tick_gap_seconds:g}s): a decision cycle's market-data "
        f"build can make {_MAX_UNREFRESHED_REST_CALLS} back-to-back REST calls with "
        "no refresh between them, so on a degraded network that chain alone can push "
        "the §18.2 refresh past the exchange-side deadline and cancel the resting "
        f"SL/TP. Set network_timeout_s below {budget:g} in the config for headroom."
    )


def sl_repair_delay_warning(delay_seconds: float, max_tick_gap_seconds: float) -> str | None:
    """The SL-repair-ladder sibling of :func:`network_timeout_warning` (advisory).

    ``protection._maybe_delay`` sleeps the FULL configured
    ``sl_repair_retry_delay_seconds`` between repair attempts and only refreshes
    the kill switch AFTER the sleep — so a delay at or above the
    ``max_tick_gap_seconds`` promise stretches the refresh gap during an SL
    repair episode, the one window where the position has no valid stop. Push it
    past the scheduleCancel budget and the dead man's switch cancels every
    resting order mid-repair. Config only enforces ``> 0``; like its sister
    this is a warn-not-refuse preflight, with the hard construction-time
    invariant deferred to PR 6. The default (5s vs 30s) never warns.
    """
    if delay_seconds < max_tick_gap_seconds:
        return None
    return (
        f"live.protection.sl_repair_retry_delay_seconds ({delay_seconds:g}) is "
        f"not below the kill switch's max tick gap ({max_tick_gap_seconds:g}s) "
        "— the repair ladder sleeps that long between attempts while the "
        "position has no valid stop, and can push the §18.2 refresh past the "
        "exchange-side deadline, cancelling every resting order mid-repair. "
        f"Set it below {max_tick_gap_seconds:g} for headroom."
    )


def refresh_across_blocking_work(kill_switch: KillSwitchManager | None, *, what: str) -> None:
    """Refresh the dead man's switch across a long blocking operation.

    THE helper for :meth:`KillSwitchManager.tick`'s stated contract — "the owner
    must call this at least once per ``max_tick_gap_seconds``, INCLUDING from
    inside a long decision cycle". The switch does not refresh itself, and the
    live loop is single-threaded, so every site that blocks it for anything
    approaching a network timeout has to call this or the exchange-side deadline
    lapses and cancels every resting order on the wallet while the process is
    alive and healthy — the §20.3 unprotected window opening at exactly the
    moment the network is least able to close it.

    CHEAP when a refresh is not due: :meth:`tick` reaches the wire only when
    ``refresh_due()`` says so, so calling this once per loop iteration costs a
    clock read plus the unconditional expired-deadline detection — and that
    detection is half the point, since a lapse discovered mid-sweep is a lapse
    the caller's own row-trust logic needs to know about.

    Guarded, and deliberately never re-raising: a refresh miss must not abort
    the work it was protecting. The caller is typically mid-repair or mid-sweep,
    where dying is strictly worse than a stale switch the next tick retries.
    Mirrors ``protection._maybe_delay``'s long-standing treatment, which this
    replaced.
    """
    if kill_switch is None:
        return
    try:
        kill_switch.tick()
    except Exception:  # noqa: BLE001 — a refresh miss must not abort its caller
        logger.warning("kill-switch refresh during %s failed", what, exc_info=True)


# The resting protective roles ``shutdown(keep_protective=True)`` leaves
# standing (decided 2026-07-22). Narrower than order_gate.PROTECTIVE_ORDER_ROLES
# for the same reason as protection._SLTP_ROLES: emergency_close is a one-shot
# IOC, never a resting order for a sweep to keep.
_KEEP_PROTECTIVE_ROLES = frozenset({"stop_loss", "take_profit"})
# Literal copy of LIVE_ORDER_ROLES members: a role renamed there without this
# file would silently drop it from the shutdown keep-set — the sweep would
# cancel a live position's resting SL/TP. Fail at import.
if not _KEEP_PROTECTIVE_ROLES <= LIVE_ORDER_ROLES:
    raise AssertionError("_KEEP_PROTECTIVE_ROLES drifted from LIVE_ORDER_ROLES")


class KillSwitchManager:
    """One run's dead man's switch: arm, refresh on cadence, cancel on shutdown."""

    def __init__(
        self,
        *,
        client: HyperliquidSignedClient,
        gate: RealOrderGate,
        db: Database,
        run_id: str,
        config: KillSwitchConfig,
        max_tick_gap_seconds: float,
        payload_dir: Path,
        clock: Clock | None = None,
    ) -> None:
        if not config.enabled:
            # LiveConfig already rejects allow_real_orders without the switch;
            # constructing a manager from a disabled config means the caller's
            # wiring is wrong, not that we should silently no-op a safety net.
            raise ValueError("KillSwitchManager requires live.kill_switch.enabled")
        # The manager only ever refreshes when its owner ticks it, so what really
        # bounds how late a refresh can land is the caller's TICK GAP, not the
        # configured interval. That number lives in the caller, so it is passed
        # in and the invariant is ENFORCED here instead of assumed in a comment:
        # the deadline must survive a refresh that is one whole tick gap late.
        #
        # ``max_tick_gap_seconds`` is the caller's WORST-CASE WALL TIME BETWEEN
        # TWO tick() CALLS — not its sleep interval. Read the contract literally:
        # in the paper loop this manager's owner will ride, one iteration runs
        # engine.tick() and scheduler.poll() SYNCHRONOUSLY before it sleeps, and
        # poll() can run a full multi-agent AI decision — MINUTES, not seconds.
        # A caller that passes its sleep cap (60s) while its cycle can block for
        # three minutes has satisfied this check and will still be cancelled off
        # the book mid-decision. The live loop (PR 5) satisfies the contract the
        # other way around: the AI decision runs on a background worker thread
        # (Option A, live/decision.py), so one live iteration never blocks on it
        # and the ordinary top-of-loop tick() IS the refresh cadence — there is
        # no in-cycle refresher, and none is needed. What can still stretch the
        # gap is a slow REST call riding its full timeout (§18.2 advisory,
        # ``network_timeout_warning``) — pass the true worst-case gap here.
        # The invariant itself (worst-case math, config-guard blindness) is
        # kill_switch_timing_violation's docstring.
        violation = kill_switch_timing_violation(config, max_tick_gap_seconds)
        if violation is not None:
            raise ValueError(violation)
        self._client = client
        self._gate = gate
        self._db = db
        self._run_id = run_id
        self._config = config
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()
        self._armed = False
        self._shutdown_started = False
        # Distinct from _shutdown_started on purpose. _shutdown_started closes
        # the §4.1 gate on the FIRST statement of shutdown() and can never be
        # unwound; _shutdown_completed says the sweep actually ran to its
        # completed event. Only the latter suppresses a re-entry — a shutdown
        # that died partway (the unguarded audit write hitting a busy DB) must
        # still be retryable, or a signal-handler + finally pair would swallow
        # the one call that cancels our live orders.
        self._shutdown_completed = False
        # The WIRE fact, kept apart from the audit fact: True the instant
        # clear_scheduled_cancel() returns, so a shutdown retry can never re-gate
        # the disarm on a fresh sweep and "un-disarm" a trigger the exchange has
        # already forgotten. See shutdown().
        self._scheduled_cancel_cleared = False
        self._last_scheduled_at: datetime | None = None
        # The schedule epoch whose lapse has already been reported, so one fired
        # switch produces one event. Keyed on the schedule, not on the latch —
        # see _detect_expired_deadline.
        self._expiry_reported_for: datetime | None = None
        # Firings this manager has OBSERVED, monotonic. Public via
        # :attr:`fired_total` so a reader can tell "a NEW firing happened since I
        # last looked" from "the switch is merely unhealthy" -- only the former
        # invalidates anyone's local order rows (2026-07-31 exit check).
        self._fired_total = 0
        # Sticky until PR 4's safe-mode release path clears it (§13.4). PRIVATE,
        # read through a property: it is the latch that blocks new orders, and a
        # bare writable attribute invites `ks.stop_new_orders = False` as a
        # shortcut past release_safe_mode() — which would reopen the §4.1 gate on
        # the next refresh with no proving round-trip and no §13.4
        # reconciliation. Released by luck is the one thing the sticky rule
        # exists to prevent, so there is one door, and it is release_safe_mode().
        self._stop_new_orders = False
        # Whether the operator has already been told we are in safe mode. Not
        # derivable from stop_new_orders: release_safe_mode() drops the latch
        # BEFORE its proving refresh, so a failed release would re-announce
        # "entering safe mode" on every retry of an unchanged state.
        self._safe_mode_announced = False

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def stop_new_orders(self) -> bool:
        """The sticky §18.2-rule-3 latch: no new order may pass while it is up.

        Read-only by design. PR 4's safe-mode state machine CONSUMES this flag;
        the only thing that lowers it is :meth:`release_safe_mode`, which earns
        the release with a proving refresh instead of asserting it.
        """
        return self._stop_new_orders

    def _record(
        self, event_type: str, *, detail: str | None = None, error: str | None = None
    ) -> None:
        with self._db.transaction() as conn:
            repo.insert_kill_switch_event(
                conn,
                run_id=self._run_id,
                event_type=event_type,
                detail=detail,
                error_message=error,
                timestamp=self._clock.now(),
            )

    def _schedule(self) -> None:
        """One scheduleCancel round-trip pushing the deadline to now + window."""
        now = self._clock.now()
        deadline = now + timedelta(seconds=self._config.schedule_cancel_seconds)
        self._client.schedule_cancel(cancel_at=deadline)
        self._last_scheduled_at = now

    def _check_clock_skew(self) -> None:
        """Refuse to arm against a host clock the exchange does not agree with.

        Every deadline this manager sends is ABSOLUTE and computed from the local
        clock, and every lapse it detects is measured against that same clock —
        so the pair is self-consistent and host drift is invisible to us while
        changing the window that actually protects the wallet. A host four
        minutes fast turns a 120s dead man's switch into a 360s one: the process
        can die and leave real orders exposed for three times the configured
        window, with nothing in the logs or events to say so. The declared
        max_tick_gap invariant cannot catch it — it reasons entirely in local
        time.

        Only the "host ahead" direction is silent, which is why it needs a guard:
        a host BEHIND sends a deadline the exchange sees as too near (or past),
        and the exchange rejects it — arm() then fails loud on its own.

        Skipped with a warning when the exchange hands back no timestamp: an
        unavailable clock must degrade the CHECK, never block an otherwise
        healthy startup.
        """
        try:
            exchange_now = self._client.exchange_time()
        except Exception as exc:
            logger.warning(
                "kill switch could not read the exchange clock (%s); host clock skew "
                "is unverified and the scheduleCancel window is only as accurate as "
                "this host's clock",
                exc,
            )
            return
        if exchange_now is None:
            logger.warning(
                "kill switch could not verify host clock skew: the exchange returned "
                "no timestamp — the scheduleCancel window is only as accurate as this "
                "host's clock"
            )
            return
        skew = (self._clock.now() - exchange_now).total_seconds()
        if abs(skew) >= _MAX_CLOCK_SKEW_S:
            raise ValueError(
                f"host clock differs from the exchange by {skew:+.1f}s (limit "
                f"{_MAX_CLOCK_SKEW_S}s): scheduleCancel deadlines are absolute and "
                f"computed from this clock, so the dead man's switch would not really "
                f"be the configured {self._config.schedule_cancel_seconds}s wide. "
                "Fix time sync (NTP) before trading."
            )
        logger.info("kill switch clock check: host is %+.2fs from the exchange", skew)

    def arm(self) -> None:
        """§18.2 rule 1: schedule cancel immediately after client init.

        An arming failure is a hard error (propagates): starting a live loop
        whose dead man's switch never engaged would run real orders with no
        crash protection at all.

        Startup-only, and self-enforcing: arming twice is a wiring bug, not a
        recovery path. PR 4's safe-mode release goes through
        release_safe_mode(), never through a second arm().
        """
        if self._shutdown_started:
            # Same terminal boundary tick()/refresh() enforce: re-arming a
            # retired manager would silently reopen the §4.1 gate condition
            # shutdown() just closed.
            raise RuntimeError("KillSwitchManager.arm() after shutdown()")
        if self._armed:
            raise RuntimeError("KillSwitchManager.arm() twice — already armed")
        # Before the first absolute deadline goes out, prove the clock we compute
        # it from agrees with the exchange's.
        self._check_clock_skew()
        self._schedule()
        self._armed = True
        # NOT an unconditional True: the sticky stop_new_orders latch outranks
        # arming, exactly as it does in refresh(). Were arm() ever made
        # re-runnable, an unconditional True here would silently reopen the
        # §4.1 gate that a refresh failure closed — releasing safe mode by luck
        # instead of through PR 4's §13.4 reconciliation.
        self._gate.kill_switch_active = not self.stop_new_orders
        logger.info(
            "kill switch armed: deadline=%ss refresh=%ss",
            self._config.schedule_cancel_seconds,
            self._config.refresh_interval_seconds,
        )
        self._record(
            "kill_switch_armed",
            detail=f"deadline={self._config.schedule_cancel_seconds}s "
            f"refresh={self._config.refresh_interval_seconds}s",
        )

    def release_safe_mode(self) -> bool:
        """PR 4 §13.4: the ONE way to clear the sticky refresh-failure latch.

        Two pieces of state must move together — the latch and the §4.1 gate
        condition — and clearing only one silently half-works: clearing the
        latch alone leaves the gate shut until the next successful refresh,
        while opening the gate alone is reverted by that same refresh (which
        recomputes it from the latch). Callers get one door, not two knobs.

        The release is EARNED, not asserted: the only state it can be called
        from is one where the last refresh failed, so the exchange-side
        deadline is stale and may be seconds from firing. Reopening the gate
        there would admit new orders against a switch about to cancel them.
        So the latch drops and the deadline is immediately pushed — a
        successful refresh reopens §4.1 (it recomputes the gate from the
        latch), a failing one re-latches and leaves us exactly where we were.
        Returns True when the release took effect.

        The §18.5 trace of a release is the ordinary ``kill_switch_refreshed``
        row its proving refresh writes — truthful, because a real scheduleCancel
        round-trip did happen. The DISTINGUISHABLE record ("safe mode released,
        by whom, why") belongs to PR 4's ``safe_mode_events`` table, which owns
        safe-mode history; it is deliberately not a tenth §18.5 event type.
        """
        if self._shutdown_started:
            raise RuntimeError("KillSwitchManager.release_safe_mode() after shutdown()")
        if not self._armed:
            raise RuntimeError("KillSwitchManager.release_safe_mode() before arm()")
        if not self._stop_new_orders:
            return True
        self._stop_new_orders = False
        self.refresh()
        # The latch, not the round-trip, is the verdict. refresh() can succeed
        # and STILL re-latch us — if the deadline had already elapsed, the switch
        # has fired and its orders are gone, so that refresh re-arms the exchange
        # but must not resume trading.
        #
        # PR 4 CONTRACT: that refresh also re-schedules, so calling this again
        # would see a fresh deadline and succeed. The "release only after
        # reconciliation" precondition is the CALLER's to honour — this method is
        # PR 4's post-reconciliation door, not a retry loop. Never wire it as
        # `while not release_safe_mode(): sleep()`: that would auto-release a
        # switch that just fired, on order rows still stale-'open'.
        if self.stop_new_orders:
            logger.warning("kill switch safe mode release failed: the switch is still not healthy")
            return False
        logger.info("kill switch safe mode released: new orders may pass the gate again")
        return True

    def refresh_due(self) -> bool:
        """True when the §18.2 rule-2 cadence calls for a refresh.

        The interval means "at least this often", not "no sooner than". A caller
        ticking at the same period as refresh_interval_seconds (the intended
        30s/30s wiring) would otherwise let ordinary scheduling jitter — the
        milliseconds between its tick boundary and this call — push elapsed just
        under the interval and SKIP the refresh, halving the real cadence to
        every other cycle. The slack keeps that from happening at all; what makes
        a skipped cycle SURVIVABLE if it happens anyway is the constructor
        invariant (refresh_interval + worst-case tick gap < schedule_cancel).
        """
        if self._last_scheduled_at is None:
            return True
        elapsed = (self._clock.now() - self._last_scheduled_at).total_seconds()
        if elapsed < -_CLOCK_BACKWARDS_TOLERANCE_S:
            # The clock moved BACKWARDS (an NTP step, a VM resume, a manual
            # correction). The exchange's deadline was computed from the wall
            # clock at schedule time and is ticking on ITS clock regardless, so
            # "no time has passed" is the one reading that is certainly wrong.
            # Refresh immediately — the fail-safe direction — rather than let a
            # negative elapsed park the switch until the local clock catches
            # back up, which for an hour-sized step means an hour of no
            # refreshes while the deadline fires (2026-07-31 clock review).
            logger.warning(
                "kill switch: clock moved backwards (%.1fs since the last schedule) — "
                "refreshing now rather than trusting the negative interval",
                elapsed,
            )
            return True
        return elapsed >= self._config.refresh_interval_seconds - _REFRESH_DUE_SLACK_S

    @property
    def fired_total(self) -> int:
        """Firings OBSERVED by this manager, monotonic.

        A firing means the exchange cancelled every order on the wallet without
        touching SQLite, so a consumer holding local order rows must re-verify
        them. A merely UNHEALTHY switch -- one failed refresh -- cancels
        nothing, and conflating the two makes an ordinary network blip look like
        a wiped book, which is how a healthy run gets failed.
        """
        return self._fired_total

    def _detect_expired_deadline(self) -> None:
        """Notice that the dead man's switch already FIRED before we got here.

        ``max_tick_gap_seconds`` is a promise the caller makes at construction;
        this is the only thing that checks whether it kept it. If more than
        schedule_cancel_seconds has passed since the last successful schedule,
        the exchange has already cancelled every order on the wallet — and a
        refresh that simply re-armed and reopened the §4.1 gate would leave the
        engine placing orders against a book it believes is still there, with
        nothing but a gap in the event timestamps to show for it.

        Treated exactly like a refresh failure (the same sticky latch, released
        only by PR 4's §13.4 reconciliation), because the consequence is the
        same and worse: our orders are provably gone, and the local rows still
        say 'open' until reconciliation settles them.

        Reported once per LAPSED DEADLINE, keyed on the schedule it lapsed from —
        NOT on the latch. The latch is also raised by a refresh failure, and a
        run of refresh failures is precisely what lets a deadline lapse (an API
        outage spanning it), so keying on the latch would go silent on the single
        most likely real firing. Keying on the schedule epoch reports exactly
        once in every path: while refreshes keep failing the epoch is frozen (one
        report), and while they succeed the deadline is pushed and never lapses.
        """
        if self._last_scheduled_at is None:
            return
        elapsed = (self._clock.now() - self._last_scheduled_at).total_seconds()
        if elapsed < self._config.schedule_cancel_seconds:
            return
        # Fail-safe state first: whatever happens to the audit write below, the
        # gate is shut and the latch is up.
        already_reported = self._expiry_reported_for == self._last_scheduled_at
        self._gate.kill_switch_active = False
        self._stop_new_orders = True
        self._safe_mode_announced = True
        if already_reported:
            return
        logger.error(
            "kill switch FIRED: %.1fs since the last refresh exceeds the %ss "
            "scheduled-cancel deadline — the exchange has cancelled every order "
            "on this wallet. New orders are BLOCKED until reconciliation "
            "(§18.2 rule 3); local order rows are stale until then.",
            elapsed,
            self._config.schedule_cancel_seconds,
        )
        self._record(
            "kill_switch_cancel_triggered",
            detail=f"no refresh for {elapsed:.1f}s "
            f"(deadline {self._config.schedule_cancel_seconds}s)",
        )
        # Marked reported only AFTER the event is durable. _record is fail-loud
        # by design (a busy DB raises), and marking first would let that single
        # failure permanently swallow the one record that the switch fired: the
        # retry — the next refresh, or the shutdown re-entry — would compute
        # already_reported and skip both the log and the write. Worst case now is
        # a repeated ERROR line on retry; never a lost firing.
        self._expiry_reported_for = self._last_scheduled_at
        self._fired_total += 1

    def refresh(self) -> bool:
        """Push the exchange-side deadline back; False (plus flags) on failure.

        §18.2 rule 3: a failed refresh means the switch may fire and cancel
        our orders — new orders must stop (``stop_new_orders``; PR 4 turns
        this into recoverable safe mode) and the §4.1 gate condition drops.
        The failure is recorded and NOT raised: the caller's loop must keep
        running (monitoring, protection, future refresh attempts continue —
        §18.4 rules 2–4).

        A refresh that arrives AFTER the deadline already elapsed still
        re-arms the exchange-side switch (protection going forward), but the
        latch it raises keeps the §4.1 gate shut — see _detect_expired_deadline.
        """
        if self._shutdown_started:
            raise RuntimeError("KillSwitchManager.refresh() after shutdown()")
        if not self._armed:
            raise RuntimeError("KillSwitchManager.refresh() before arm()")
        # Before anything else: did the switch already fire while we were away?
        self._detect_expired_deadline()
        try:
            self._schedule()
        except Exception as exc:
            self._gate.kill_switch_active = False
            self._stop_new_orders = True
            # Log BEFORE the durable record: if the event write itself dies
            # (broken DB at the worst moment), the root cause is already on
            # the log. The write stays unguarded — losing the audit trail
            # must fail loud, same as every other event write here.
            logger.warning("kill switch refresh failed: %s", exc)
            if not self._safe_mode_announced:
                self._safe_mode_announced = True
                # The state transition, called out once: from here new orders
                # are blocked until PR 4's §13.4 reconciliation releases them.
                # An operator tailing the service log must not have to infer
                # that from a repeating warning — nor read it afresh on every
                # failed release attempt of an unchanged state.
                logger.error(
                    "kill switch entering safe mode: new orders are BLOCKED until "
                    "reconciliation releases them (§18.2 rule 3)"
                )
            self._record("kill_switch_refresh_failed", error=str(exc))
            return False
        # The exchange-side switch is re-armed, but the §4.1 condition only
        # re-opens if no failure is outstanding: after a refresh failure,
        # release goes through PR 4's §13.4 reconciliation, not through one
        # lucky refresh — the sticky flag is enforced here, at the gate,
        # not merely stored for PR 4 to poll.
        self._gate.kill_switch_active = not self.stop_new_orders
        if not self.stop_new_orders:
            # Out of safe mode <=> no announcement outstanding. Reset HERE, next
            # to the gate it mirrors, not in release_safe_mode() after this call
            # returns: the unguarded _record below can raise (a busy DB), and a
            # reset stranded behind it would leave the flag set on a released
            # switch — silencing the ERROR on the NEXT genuine entry into safe
            # mode, which is the one announcement that must never be missed.
            # A merely-successful refresh while the latch is still down does not
            # clear it (stop_new_orders is still True), so the sticky rule holds.
            self._safe_mode_announced = False
        self._record("kill_switch_refreshed")
        return True

    def tick(self) -> None:
        """Refresh if due.

        The owner must call this at least once per ``max_tick_gap_seconds`` —
        INCLUDING from inside a long decision cycle. Ticking only at the top of
        the loop is not enough: the decision cycle runs synchronously and can
        block for minutes, and the switch does not refresh itself (§18.2).
        """
        if self._shutdown_started:
            raise RuntimeError("KillSwitchManager.tick() after shutdown()")
        # Fire detection runs UNCONDITIONALLY, not only when a refresh is due.
        # It used to be reachable solely from inside refresh(), which tick()
        # calls only when refresh_due() says so — so the one condition that
        # suppresses refresh_due() (a backwards clock making elapsed negative)
        # silently disabled the detector too, in the exact scenario where the
        # exchange-side deadline is measured on a clock nobody moved
        # (2026-07-31 clock review).
        self._detect_expired_deadline()
        if self.refresh_due():
            self.refresh()

    def _cancel_with_evidence(self, *, coin: str, cloid_hex: str, cloid_logical: str) -> None:
        """One bot-owned cancel under the §8.3/§16.5 evidence protocol.

        Delegates to the shared :func:`~.cancel.cancel_bot_order_with_evidence`
        (the §19.3 startup sweep runs the identical protocol); this wrapper
        binds the manager's dependencies and the shutdown-specific
        ``cancel_reason``. Raises the underlying failure — the sweep loop
        records it in the §18.5 completed event and carries on.
        """
        cancel_bot_order_with_evidence(
            db=self._db,
            client=self._client,
            run_id=self._run_id,
            payload_dir=self._payload_dir,
            clock=self._clock,
            coin=coin,
            cloid_hex=cloid_hex,
            cloid_logical=cloid_logical,
            cancel_reason="shutdown_cancel",
        )

    def _confirm_settled(self, order_id: str, cloid_hex: str) -> bool:
        """Ask the EXCHANGE whether a locally-live order is really finished.

        Used only by the disarm cross-check, for an order the sweep never saw in
        ``open_orders()``. Returns True when the exchange proves the order is
        settled, False when it is still live — or when the exchange's answer
        contradicts our evidence and we therefore cannot claim it is settled.
        Transport failures raise: the caller counts them as a failure, which
        keeps the trigger armed.

        ``unknownOid`` splits in two, and the split matters. If durable evidence
        says the exchange once TOOK this cloid (§8.3 rule 10), then "I don't know
        it" contradicts that, and an order we cannot account for must not earn
        the disarm. If it does NOT — a 'submitted' row whose send never actually
        reached the exchange — then there is no order and nothing to protect, so
        it must not block the disarm forever either.

        A settled answer is WRITTEN BACK. The exchange has just told us this
        order's fate; leaving the row non-terminal would make the very next
        shutdown — of this run and of every future run, since
        ``iter_open_live_orders`` deliberately spans runs — pay the same
        round-trip again for an order resolved long ago, so the cross-check's
        cost would grow without bound across restarts. This is the same
        "make SQLite agree with what the exchange just said" rule the §8.3
        recovery back-fill follows.
        """
        payload = self._client.query_order_by_cloid(cloid_hex)
        parsed = parse_order_status(payload)
        if parsed is None:
            if repo.has_exchange_known_cloid(self._db.conn, cloid_hex=cloid_hex):
                logger.warning(
                    "kill switch shutdown: orderStatus does not know cloid %s, but local "
                    "evidence says the exchange took it — cannot call it settled",
                    cloid_hex,
                )
                return False
            # The send never landed: no exchange order exists under this cloid.
            # Nothing to settle the row FROM (the exchange has no verdict to
            # record), so it stays as it is for PR 4's reconciliation.
            #
            # NAMED BOUNDARY: this is the one corner where the disarm still rests
            # on a single Info read. A place that timed out (attempt 'failed', no
            # exchange_order_id) whose order actually rested WOULD, if Info has
            # not caught up, answer unknownOid here — and be let through. It is
            # the same definition of "durable proof" §8.3 rule 10 already runs on
            # (has_exchange_known_cloid), so tightening it here without tightening
            # rule 10 would make the two disagree about the same evidence. Closing
            # it needs a stronger proof of receipt than we currently record, which
            # is PR 3/4 territory (exchange fill events, reconciliation).
            return True
        exchange_order_id, exchange_status = parsed
        local_status = local_status_for_exchange_status(exchange_status)
        # LIVE_ORDER_STATUSES is the codebase's authority on "non-terminal", and
        # it is partition-guarded against its terminal half — a bare
        # `!= "open"` test would silently call a future 'partially_filled'
        # mapping settled and drop the backstop over a live order.
        if local_status in repo.LIVE_ORDER_STATUSES:
            return False
        with self._db.transaction() as conn:
            repo.update_order(
                conn,
                order_id,
                status=local_status,
                exchange_order_id=exchange_order_id,
                exchange_status=local_status,
                exchange_raw_status=exchange_status,
                updated_at=self._clock.now(),
            )
        return True

    def _cross_check_local_orders(
        self,
        handled_cloids: set[str],
        *,
        enumeration_failed: bool,
        keep_protective: bool = False,
        kept_protective: list[str] | None = None,
    ) -> list[str]:
        """Local orders the sweep did not account for — each one blocks the disarm.

        The disarm is the ONE irreversible removal of the crash backstop, and
        until this check it rested entirely on a single ``open_orders()`` read.
        An empty answer cannot be told apart from an Info view that has not caught
        up with an order placed a second ago — and in that case failures is empty,
        the sweep reads as clean, and the wallet-wide trigger is dropped over a
        live order. Precisely the absence-of-evidence this system refuses to trust
        anywhere else: §8.3 rule 10 (``has_exchange_known_cloid``) exists because
        the exchange saying "I don't know it" does not outrank durable local proof.

        So every orders row SQLite still calls non-terminal gets accounted for. The
        sweep's own cancels have already patched their rows terminal, so they drop
        out on their own; anything left is asked about directly. Still live, or
        unconfirmable, counts as a failure — the trigger stays armed.

        ``keep_protective`` (decided 2026-07-22): a kept-role (SL/TP) row that
        ``orderStatus`` POSITIVELY confirms still live is counted kept — appended
        to ``kept_protective``, not to the returned unaccounted list — so a stale
        ``open_orders()`` read (likeliest right after a pre-shutdown modify)
        cannot block the rule-6 disarm and let the wallet-wide scheduleCancel
        later sweep exactly the orders the keep exists to protect. Only the
        positive confirmation earns the exemption: an unconfirmable row (the
        query raised, or the enumeration failed so no query ran) still blocks
        the disarm — the fail-safe posture is unchanged where there is no
        evidence.

        ``enumeration_failed`` suppresses the exchange round-trips, and that is a
        LATENCY guard, not a policy one. When ``open_orders()`` has just failed
        the endpoint is almost certainly unreachable, so every ``orderStatus``
        here is doomed for the same reason — and each one can burn the full
        network timeout, sequentially, inside a signal handler. A handful of open
        orders against a dead endpoint would push shutdown past systemd's
        TimeoutStopSec and get the process SIGKILLed mid-sweep, destroying the
        very teardown these diagnostics document. The outcome is unchanged either
        way (a sweep_error already keeps the trigger armed), so the orders are
        still NAMED in the §18.5 detail — just not re-confirmed over a network
        that is not answering.
        """
        unaccounted: list[str] = []
        try:
            rows = repo.iter_open_live_orders(self._db.conn)
        except Exception as exc:
            # We cannot prove the sweep was clean, so we must not claim it was.
            logger.warning("kill switch shutdown cannot read local live orders: %s", exc)
            return [f"?: local live-order cross-check failed: {exc}"]
        for row in rows:
            cloid = row["cloid_hex"]
            if cloid in handled_cloids:
                continue  # the sweep already cancelled it, or already failed it
            order_id = row["order_id"]
            if enumeration_failed:
                # Named, not queried — see the docstring. It blocks the disarm
                # regardless, exactly as the sweep_error already does.
                unaccounted.append(
                    f"{order_id}: local live order, open_orders enumeration failed — unconfirmed"
                )
                continue
            try:
                settled = self._confirm_settled(order_id, cloid)
            except Exception as exc:
                logger.warning(
                    "kill switch shutdown cannot confirm local order %s (cloid %s): %s",
                    order_id,
                    cloid,
                    exc,
                )
                unaccounted.append(f"{order_id}: could not confirm settled: {exc}")
                continue
            if not settled:
                if keep_protective and row["order_role"] in _KEEP_PROTECTIVE_ROLES:
                    # Confirmed live + protective role: this IS a keep, found
                    # via the local record instead of the (stale) enumeration.
                    handled_cloids.add(cloid)
                    if kept_protective is not None:
                        kept_protective.append(order_id)
                    logger.warning(
                        "kill switch shutdown: local protective order %s (cloid %s) "
                        "confirmed live but absent from open_orders — kept (reduce-only), "
                        "not counted against the disarm",
                        order_id,
                        cloid,
                    )
                    continue
                logger.warning(
                    "kill switch shutdown: local order %s (cloid %s) is still live at the "
                    "exchange but was absent from open_orders",
                    order_id,
                    cloid,
                )
                unaccounted.append(
                    f"{order_id}: still live at the exchange, absent from open_orders"
                )
        return unaccounted

    def shutdown(self, *, keep_protective: bool = False) -> None:
        """§18.2 rules 5–7: cancel bot-owned open orders; never force-close.

        Per-order failures are recorded and do not stop the sweep — every
        remaining order still gets its cancel attempt. The exchange-side
        scheduleCancel trigger is wallet-wide (it would also take out the
        non-bot orders the sweep deliberately skips per §19.3), so a fully
        clean sweep (enumeration succeeded, zero failures) disarms it; any
        failure leaves it armed as the backstop for whatever survived.

        ``keep_protective`` (decided 2026-07-22): the resting SL/TP
        (``_KEEP_PROTECTIVE_ROLES``) are deliberately LEFT STANDING instead of
        cancelled — the caller's exit path knows a live (or unreadable ≠ flat)
        position sits behind them and stripping its reduce-only protection
        would trade an unclean verdict for a naked position. A kept order is
        accounted-for, not failed: it does not block the rule-6 disarm, and the
        disarm is REQUIRED for the keep to mean anything — an armed wallet-wide
        scheduleCancel would sweep exactly those kept orders at its deadline.
        They then rest unattended (reduce-only, so they can only shrink the
        position) until a ``--loop`` run re-adopts them or the operator acts.
        The keep is honored on BOTH accounting legs: the ``open_orders()``
        enumeration below and the local cross-check (a kept-role row the stale
        enumeration missed but ``orderStatus`` positively confirms live).

        "Clean" is decided from BOTH sides. The exchange's ``open_orders()`` is
        an eventually-consistent read: an empty answer is indistinguishable from
        an Info view that has not yet caught up with an order placed a second
        ago, and believing it would drop the wallet-wide backstop over a live
        order. So the local record gets a vote too — every orders row SQLite
        still calls non-terminal that the sweep did not handle is put to the
        exchange directly (``_cross_check_local_orders``), and one that is still
        live, or that cannot be confirmed settled, is a failure like any other.

        Idempotent once it has COMPLETED. A second call after a successful
        sweep is a no-op rather than an error: shutdown is a teardown path (a
        signal handler plus a ``finally`` will plausibly both reach it), and
        re-running the sweep would re-cancel already-canceled orders, record
        their "unknown order" rejects as fresh failures, and write a second
        started/completed event pair into the §18.5 audit trail. Raising
        instead would turn a benign double-call into a crash that masks
        whatever the real shutdown reason was.

        A shutdown that RAISED partway (the unguarded audit write meeting a
        locked DB) is retried, not skipped: suppressing on "started" would let
        that same signal-handler/finally pair swallow the only call that
        cancels our live orders, leaving them resting for the wallet-wide
        trigger to sweep — which also takes out the non-bot orders §19.3 says
        never to touch.

        The retry re-runs the whole sweep, which is safe because it re-enumerates
        ``open_orders()`` — orders the first attempt cancelled are simply gone.
        Residual, accepted: if the first attempt cancelled orders and then died
        on the COMPLETED write, and the exchange's open-orders view is still
        stale on the retry, the re-cancels answer "unknown order", count as
        failures, and block the rule-6 disarm. That leaves the wallet-wide
        trigger armed — the fail-safe direction, and PR 4's reconciliation
        settles it.
        """
        if self._shutdown_completed:
            return
        # Shutdown is the one method that still runs after the caller has stopped
        # ticking, so it must ask the same question refresh() does: did the switch
        # already fire? Without this, a lapsed deadline produces the most
        # misleading audit trail of all — the exchange has swept the wallet, so
        # open_orders() comes back empty, the sweep finds nothing to cancel, and
        # the run records a completed event with `canceled: []` and disarms,
        # reading as a perfectly clean shutdown while the local rows still say
        # 'open' and nothing anywhere says the orders were cancelled for us.
        if self._armed and not self._scheduled_cancel_cleared:
            self._detect_expired_deadline()
        # The boundary is self-enforcing, not a caller convention: from here on
        # no new order may pass the §4.1 gate (the sweep must not race fresh
        # submissions), and tick()/refresh() refuse to touch a switch whose
        # manager is retiring. (The expiry check above runs first, and can only
        # shut the gate *further* — it raises the sticky latch before its own
        # audit write, so even if that write dies the gate stays closed.)
        self._shutdown_started = True
        self._gate.kill_switch_active = False
        logger.info("kill switch shutdown: sweeping bot-owned open orders")
        self._record("shutdown_cancel_orders_started")
        canceled: list[str] = []
        skipped_non_bot: list[str] = []
        kept_protective: list[str] = []
        failures: list[str] = []
        open_orders: list = []
        sweep_error: str | None = None
        # Every bot cloid this sweep took responsibility for — cancelled OR
        # failed. The cross-check below skips these and asks the exchange about
        # whatever local live orders remain.
        handled_cloids: set[str] = set()
        try:
            raw_orders = self._client.open_orders()
            if isinstance(raw_orders, list):
                open_orders = raw_orders
            else:
                sweep_error = f"open_orders returned {type(raw_orders).__name__}, expected a list"
        except Exception as exc:
            sweep_error = f"open_orders failed: {exc}"
        if sweep_error is not None:
            # Can't enumerate: the scheduled cancel deadline is the backstop.
            # Same ordering rule as refresh(): log before the durable event
            # write so a failing write cannot erase the root cause.
            logger.warning("kill switch shutdown cannot enumerate open orders: %s", sweep_error)
        for order in open_orders:
            if not isinstance(order, dict):
                # Unknown shape means unknown ownership: this might be a bot
                # order the sweep cannot cancel, so it is a FAILURE (keeps the
                # wallet-wide backstop armed), not a skip — and it must not
                # abort the sweep for the well-formed entries that follow.
                logger.warning("kill switch shutdown: malformed open_orders entry: %r", order)
                failures.append(f"?: malformed open_orders entry ({type(order).__name__})")
                continue
            oid = str(order.get("oid", "?"))
            coin = order.get("coin")
            cloid = order.get("cloid")
            try:
                # The ownership lookup sits INSIDE the per-order guard: a
                # repo-layer error here (e.g. lock contention) is the same
                # must-not-stop-the-sweep class as a failed cancel.
                if not isinstance(cloid, str):
                    # §19.3: no cloid = non-bot-owned; §25 — never manage it.
                    skipped_non_bot.append(oid)
                    continue
                row = repo.get_cloid_by_hex(self._db.conn, cloid)
                if row is None:
                    # §19.3: unknown cloid = non-bot-owned; §25 — never
                    # manage it. PR 4's reconciliation escalates to manual
                    # safe mode.
                    skipped_non_bot.append(oid)
                    continue
                if keep_protective and row["order_role"] in _KEEP_PROTECTIVE_ROLES:
                    # Deliberately left standing (see the docstring): accounted
                    # for, so the cross-check below must not read it as
                    # unaccounted and the rule-6 disarm must not be blocked.
                    handled_cloids.add(cloid)
                    kept_protective.append(oid)
                    continue
                if coin is None:
                    # Bot-owned but the payload carries no coin to cancel
                    # with: "ours but uncancelable" is a FAILURE, not "not
                    # ours" — the sweep is not clean and the wallet-wide
                    # backstop must stay armed for this order.
                    handled_cloids.add(cloid)
                    failures.append(f"{oid}: bot-owned order missing 'coin' in open_orders")
                    continue
                # Ours: from here the sweep owns this cloid's outcome, cancelled
                # or failed. Recorded BEFORE the attempt, so a raise cannot leave
                # the cross-check double-counting it as unaccounted-for.
                handled_cloids.add(cloid)
                self._cancel_with_evidence(
                    coin=coin, cloid_hex=cloid, cloid_logical=row["cloid_logical"]
                )
            except Exception as exc:
                # ANY per-order failure — exchange, gate, or persistence-layer
                # (ValueError/sqlite3.*) — must not stop the sweep: the
                # remaining orders still get their cancel attempt, and the
                # completed event must always be written. Log at capture, same
                # ordering rule as refresh().
                logger.warning("kill switch shutdown cancel failed for %s: %s", oid, exc)
                failures.append(f"{oid}: {type(exc).__name__}: {exc}")
                continue
            canceled.append(oid)
        # The exchange's open-orders view has had its say; now the local record
        # gets its say, and an order it still calls live that the sweep never
        # touched blocks the disarm. Runs even when the enumeration FAILED — a
        # sweep_error already keeps the trigger armed, but the operator still
        # wants these orders named in the §18.5 detail.
        unaccounted = self._cross_check_local_orders(
            handled_cloids,
            enumeration_failed=sweep_error is not None,
            keep_protective=keep_protective,
            kept_protective=kept_protective,
        )
        failures.extend(unaccounted)
        # The one line that answers "is real money still exposed?" without
        # opening SQLite: what the sweep cancelled, what it could not, and what
        # it deliberately left alone.
        logger.info(
            "kill switch shutdown sweep: %d canceled, %d failed (%d of them local orders "
            "the exchange never listed), %d skipped (non-bot), %d kept (protective)",
            len(canceled),
            len(failures),
            len(unaccounted),
            len(skipped_non_bot),
            len(kept_protective),
        )
        self._record(
            "shutdown_cancel_orders_completed",
            detail=json.dumps(
                {
                    "canceled": canceled,
                    "skipped_non_bot": skipped_non_bot,
                    "kept_protective": kept_protective,
                    "failures": failures,
                }
            ),
            error=sweep_error,
        )
        # A clean sweep earns the disarm — but so does "we already cleared it on a
        # previous attempt whose audit write died". The wire fact outranks a fresh
        # sweep: without it, a retry whose open_orders() happens to fail would skip
        # the disarm block entirely and log "left ARMED, the trigger will fire" for
        # a trigger the exchange has already forgotten.
        already_cleared = self._scheduled_cancel_cleared
        disarm_due = already_cleared or (sweep_error is None and not failures)
        if self._armed and disarm_due:
            # Clean sweep: no bot order is left for the wallet-wide trigger to
            # protect, and letting it fire would cancel the skipped non-bot
            # orders — and any deliberately-kept SL/TP, which is exactly what
            # ``keep_protective`` must prevent. Disarm; a disarm failure stays
            # armed (fail-safe) and is
            # recorded, never raised out of the shutdown path. An unarmed
            # manager has nothing to disarm — a shutdown before arm() must
            # not record a phantom disarmed event.
            try:
                if not self._scheduled_cancel_cleared:
                    self._client.clear_scheduled_cancel()
                    # Latched the instant the wire call returns, BEFORE anything
                    # that can raise: from here the trigger is physically gone,
                    # and no retry may issue the call (or judge the disarm) again.
                    self._scheduled_cancel_cleared = True
            except Exception as exc:
                # Same ordering rule as refresh(): log first so a failing
                # event write cannot erase the disarm failure's root cause.
                logger.warning("kill switch disarm failed: %s", exc)
                self._record("kill_switch_disarm_failed", error=str(exc))
            else:
                # Say WHICH attempt earned the disarm. On a retry that rides the
                # wire latch, this very shutdown may have recorded a failed
                # enumeration a few lines above — claiming "clean shutdown sweep"
                # there would put two flatly contradictory rows next to each other
                # in the §18.5 trail that PR 4 reconciles against.
                detail = (
                    "scheduled cancel already cleared on a prior attempt"
                    if already_cleared
                    else "clean shutdown sweep"
                )
                logger.info("kill switch disarmed: %s", detail)
                self._record("kill_switch_disarmed", detail=detail)
                # Cleared LAST, for the same reason _shutdown_completed is: this
                # flag is what makes the disarm re-attemptable. If the event write
                # above dies on a busy DB, the retry must still find _armed True
                # and land the event — otherwise the audit trail would claim the
                # wallet-wide backstop is still armed when it is not.
                self._armed = False
        elif self._armed:
            # The other half of the operator's question: the wallet-wide trigger
            # is STILL LIVE and will fire at the deadline. That is the intended
            # backstop, but it is not a quiet outcome — say so.
            logger.warning(
                "kill switch left ARMED after shutdown (sweep not clean): the "
                "wallet-wide scheduled cancel will fire at its deadline"
            )
        # Marked complete LAST — every durable write above (the sweep outcome AND
        # the disarm verdict) is now on disk. Latching before the disarm block
        # would let one failed audit write lose the kill_switch_disarmed event
        # forever: _record is fail-loud, the exception would unwind shutdown(),
        # and the signal-handler/finally retry this flag exists to serve would
        # early-return over the very write that failed. Same rule as
        # _detect_expired_deadline's report mark: the flag that suppresses a
        # retry is set only once the thing it suppresses is durable.
        self._shutdown_completed = True
