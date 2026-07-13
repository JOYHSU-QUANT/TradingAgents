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

from ..exchanges.hyperliquid.errors import ExchangeError
from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import live_order_attempt_id
from .config import KillSwitchConfig
from .order_gate import RealOrderGate

__all__ = ["KillSwitchManager"]

logger = logging.getLogger(__name__)

# Jitter budget for refresh_due(). The live loop's period equals
# refresh_interval_seconds by default, so a bare `elapsed >= interval` test
# skips every other refresh over mere milliseconds of scheduling drift (tick()
# runs a hair later in the cycle than the schedule it is compared against).
# Half a second dwarfs that drift while staying well inside the interval, so it
# only ever pulls a refresh slightly EARLIER — never pushes one past the
# deadline it exists to renew, and never turns a genuinely early call into a
# refresh. The real backstop, if a loop's jitter ever exceeds this, is the
# constructor invariant (refresh_interval + loop_period < schedule_cancel) —
# the config guard alone cannot be one, because it cannot see the loop's period.
_REFRESH_DUE_SLACK_S = 0.5

# NOTE on breadth: the refresh/shutdown paths deliberately catch ``Exception``,
# not just ExchangeError — a round-trip can also fail in the persistence layer
# (sqlite lock/IntegrityError, an invariant ValueError) or at the client's own
# bound gate (LiveOrderGateRejected, NOT an ExchangeError), and any of those
# escaping would abort the sweep mid-loop / kill the live loop — exactly what
# §18.2 rule 3 and §18.4 rules 2–4 forbid. The error type travels in the
# recorded event, so a code bug is loud in the audit trail, never re-raised
# into the safety path.


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
        loop_period_seconds: float,
        clock: Clock | None = None,
    ) -> None:
        if not config.enabled:
            # LiveConfig already rejects allow_real_orders without the switch;
            # constructing a manager from a disabled config means the caller's
            # wiring is wrong, not that we should silently no-op a safety net.
            raise ValueError("KillSwitchManager requires live.kill_switch.enabled")
        # The manager only ever refreshes when its owner ticks it, so the LOOP's
        # period — not the configured interval — is what really bounds how late
        # a refresh can land. That number lives in the caller (PR 5's live loop;
        # the paper loop it rides sleeps up to 60s), so it is passed in and the
        # invariant is ENFORCED here rather than assumed in a comment: the
        # deadline must survive a refresh that is one whole loop period late.
        #
        # Worst case between two refreshes is refresh_interval + loop_period
        # (the tick after the interval elapses can be a full period away). The
        # config guard's `schedule_cancel >= 2 x refresh_interval` is only the
        # special case of this where the loop ticks at the interval — it cannot
        # see the loop's period, so it cannot catch e.g. schedule_cancel=60 /
        # refresh=30 driven by a 60s-waking loop, which fires the switch DURING
        # NORMAL OPERATION and cancels every order on the wallet.
        if loop_period_seconds <= 0:
            raise ValueError(f"loop_period_seconds must be > 0, got {loop_period_seconds}")
        worst_case_gap = config.refresh_interval_seconds + loop_period_seconds
        if worst_case_gap >= config.schedule_cancel_seconds:
            raise ValueError(
                f"the kill switch cannot be refreshed in time: a refresh may land "
                f"{worst_case_gap}s apart (refresh_interval "
                f"{config.refresh_interval_seconds}s + loop period "
                f"{loop_period_seconds}s), but the scheduled cancel fires after "
                f"{config.schedule_cancel_seconds}s — the dead man's switch would "
                "cancel every order on the wallet during normal operation"
            )
        self._client = client
        self._gate = gate
        self._db = db
        self._run_id = run_id
        self._config = config
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
        self._last_scheduled_at: datetime | None = None
        # Sticky until PR 4's safe-mode release path clears it (§13.4).
        self.stop_new_orders = False
        # Whether the operator has already been told we are in safe mode. Not
        # derivable from stop_new_orders: release_safe_mode() drops the latch
        # BEFORE its proving refresh, so a failed release would re-announce
        # "entering safe mode" on every retry of an unchanged state.
        self._safe_mode_announced = False

    @property
    def armed(self) -> bool:
        return self._armed

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
        if not self.stop_new_orders:
            return True
        self.stop_new_orders = False
        if not self.refresh():
            # refresh() already re-latched, closed the gate and recorded why.
            logger.warning("kill switch safe mode release failed: the switch still cannot refresh")
            return False
        logger.info("kill switch safe mode released: new orders may pass the gate again")
        return True

    def refresh_due(self) -> bool:
        """True when the §18.2 rule-2 cadence calls for a refresh.

        The interval means "at least this often", not "no sooner than". The
        live loop's period EQUALS refresh_interval_seconds by default (both
        30s), so an exact ``>=`` test lets ordinary scheduling jitter — the
        milliseconds between the loop's tick boundary and this call — push
        elapsed just under the interval and SKIP the refresh, halving the real
        cadence to every other cycle. The slack keeps that from happening at
        all; what makes a skipped cycle SURVIVABLE if it happens anyway is the
        constructor invariant (refresh_interval + loop_period < schedule_cancel).
        """
        if self._last_scheduled_at is None:
            return True
        elapsed = (self._clock.now() - self._last_scheduled_at).total_seconds()
        return elapsed >= self._config.refresh_interval_seconds - _REFRESH_DUE_SLACK_S

    def refresh(self) -> bool:
        """Push the exchange-side deadline back; False (plus flags) on failure.

        §18.2 rule 3: a failed refresh means the switch may fire and cancel
        our orders — new orders must stop (``stop_new_orders``; PR 4 turns
        this into recoverable safe mode) and the §4.1 gate condition drops.
        The failure is recorded and NOT raised: the caller's loop must keep
        running (monitoring, protection, future refresh attempts continue —
        §18.4 rules 2–4).
        """
        if self._shutdown_started:
            raise RuntimeError("KillSwitchManager.refresh() after shutdown()")
        if not self._armed:
            raise RuntimeError("KillSwitchManager.refresh() before arm()")
        try:
            self._schedule()
        except Exception as exc:
            self._gate.kill_switch_active = False
            self.stop_new_orders = True
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
        """Refresh if due — the live loop calls this every cycle (30s)."""
        if self._shutdown_started:
            raise RuntimeError("KillSwitchManager.tick() after shutdown()")
        if self.refresh_due():
            self.refresh()

    def _cancel_with_evidence(self, *, coin: str, cloid_hex: str, cloid_logical: str) -> None:
        """One bot-owned cancel under the §8.3/§16.5 evidence protocol.

        Attempt row before the wire, outcome patch after; a successful cancel
        also settles the local orders row (status/canceled_at/cancel_reason)
        so shutdown never leaves phantom 'open' rows behind. Raises the
        underlying failure — the sweep loop records it and carries on.
        """
        attempt_index = repo.next_live_attempt_index(
            self._db.conn, action="cancel_by_cloid", cloid_hex=cloid_hex
        )
        attempt_id = live_order_attempt_id(
            self._run_id, "cancel_by_cloid", cloid_hex, attempt_index
        )
        now = self._clock.now()
        local_order = repo.get_order_by_cloid_hex(self._db.conn, cloid_hex)
        with self._db.transaction() as conn:
            repo.insert_live_order_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=self._run_id,
                action="cancel_by_cloid",
                symbol=coin,
                attempt_index=attempt_index,
                order_id=None if local_order is None else local_order["order_id"],
                cloid_logical=cloid_logical,
                cloid_hex=cloid_hex,
                requested_at=now,
            )
        try:
            ack = self._client.cancel_by_cloid(coin=coin, cloid_hex=cloid_hex)
        except Exception as exc:
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn, attempt_id, status="failed", error_message=str(exc)
                )
            raise
        done_at = self._clock.now()
        with self._db.transaction() as conn:
            repo.update_live_order_attempt(
                conn,
                attempt_id,
                status="acknowledged" if ack.success else "rejected",
                error_message=ack.error,
                acknowledged_at=done_at,
            )
            if ack.success and local_order is not None:
                repo.update_order(
                    conn,
                    local_order["order_id"],
                    status="canceled",
                    exchange_status="canceled",
                    canceled_at=done_at,
                    cancel_reason="shutdown_cancel",
                    updated_at=done_at,
                )
        if not ack.success:
            raise ExchangeError(f"cancel rejected: {ack.error}")

    def shutdown(self) -> None:
        """§18.2 rules 5–7: cancel bot-owned open orders; never force-close.

        Per-order failures are recorded and do not stop the sweep — every
        remaining order still gets its cancel attempt. The exchange-side
        scheduleCancel trigger is wallet-wide (it would also take out the
        non-bot orders the sweep deliberately skips per §19.3), so a fully
        clean sweep (enumeration succeeded, zero failures) disarms it; any
        failure leaves it armed as the backstop for whatever survived.

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
        # The boundary is self-enforcing, not a caller convention: from the
        # first statement no new order may pass the §4.1 gate (the sweep must
        # not race fresh submissions), and tick()/refresh() refuse to touch a
        # switch whose manager is retiring.
        self._shutdown_started = True
        self._gate.kill_switch_active = False
        logger.info("kill switch shutdown: sweeping bot-owned open orders")
        self._record("shutdown_cancel_orders_started")
        canceled: list[str] = []
        skipped_non_bot: list[str] = []
        failures: list[str] = []
        open_orders: list = []
        sweep_error: str | None = None
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
                if coin is None:
                    # Bot-owned but the payload carries no coin to cancel
                    # with: "ours but uncancelable" is a FAILURE, not "not
                    # ours" — the sweep is not clean and the wallet-wide
                    # backstop must stay armed for this order.
                    failures.append(f"{oid}: bot-owned order missing 'coin' in open_orders")
                    continue
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
        # The one line that answers "is real money still exposed?" without
        # opening SQLite: what the sweep cancelled, what it could not, and what
        # it deliberately left alone.
        logger.info(
            "kill switch shutdown sweep: %d canceled, %d failed, %d skipped (non-bot)",
            len(canceled),
            len(failures),
            len(skipped_non_bot),
        )
        self._record(
            "shutdown_cancel_orders_completed",
            detail=json.dumps(
                {
                    "canceled": canceled,
                    "skipped_non_bot": skipped_non_bot,
                    "failures": failures,
                }
            ),
            error=sweep_error,
        )
        # The sweep ran and its outcome is durable. From here a re-entry is a
        # genuine no-op; before here it was a retry of unfinished work.
        self._shutdown_completed = True
        if sweep_error is None and not failures and self._armed:
            # Clean sweep: no bot order is left for the wallet-wide trigger to
            # protect, and letting it fire would cancel the skipped non-bot
            # orders. Disarm; a disarm failure stays armed (fail-safe) and is
            # recorded, never raised out of the shutdown path. An unarmed
            # manager has nothing to disarm — a shutdown before arm() must
            # not record a phantom disarmed event.
            try:
                self._client.clear_scheduled_cancel()
            except Exception as exc:
                # Same ordering rule as refresh(): log first so a failing
                # event write cannot erase the disarm failure's root cause.
                logger.warning("kill switch disarm failed: %s", exc)
                self._record("kill_switch_disarm_failed", error=str(exc))
            else:
                self._armed = False
                logger.info("kill switch disarmed after a clean shutdown sweep")
                self._record("kill_switch_disarmed", detail="clean shutdown sweep")
        elif self._armed:
            # The other half of the operator's question: the wallet-wide trigger
            # is STILL LIVE and will fire at the deadline. That is the intended
            # backstop, but it is not a quiet outcome — say so.
            logger.warning(
                "kill switch left ARMED after shutdown (sweep not clean): the "
                "wallet-wide scheduled cancel will fire at its deadline"
            )
