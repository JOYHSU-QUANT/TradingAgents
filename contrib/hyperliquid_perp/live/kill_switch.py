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
   use), and a successful cancel patches the local orders row.

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
        clock: Clock | None = None,
    ) -> None:
        if not config.enabled:
            # LiveConfig already rejects allow_real_orders without the switch;
            # constructing a manager from a disabled config means the caller's
            # wiring is wrong, not that we should silently no-op a safety net.
            raise ValueError("KillSwitchManager requires live.kill_switch.enabled")
        self._client = client
        self._gate = gate
        self._db = db
        self._run_id = run_id
        self._config = config
        self._clock = clock or WallClock()
        self._armed = False
        self._last_scheduled_at: datetime | None = None
        # Sticky until PR 4's safe-mode release path clears it (§13.4).
        self.stop_new_orders = False

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
        """
        self._schedule()
        self._armed = True
        self._gate.kill_switch_active = True
        self._record(
            "kill_switch_armed",
            detail=f"deadline={self._config.schedule_cancel_seconds}s "
            f"refresh={self._config.refresh_interval_seconds}s",
        )

    def refresh_due(self) -> bool:
        """True when the §18.2 rule-2 cadence calls for a refresh."""
        if self._last_scheduled_at is None:
            return True
        elapsed = (self._clock.now() - self._last_scheduled_at).total_seconds()
        return elapsed >= self._config.refresh_interval_seconds

    def refresh(self) -> bool:
        """Push the exchange-side deadline back; False (plus flags) on failure.

        §18.2 rule 3: a failed refresh means the switch may fire and cancel
        our orders — new orders must stop (``stop_new_orders``; PR 4 turns
        this into recoverable safe mode) and the §4.1 gate condition drops.
        The failure is recorded and NOT raised: the caller's loop must keep
        running (monitoring, protection, future refresh attempts continue —
        §18.4 rules 2–4).
        """
        if not self._armed:
            raise RuntimeError("KillSwitchManager.refresh() before arm()")
        try:
            self._schedule()
        except Exception as exc:
            self._gate.kill_switch_active = False
            self.stop_new_orders = True
            self._record("kill_switch_refresh_failed", error=str(exc))
            logger.warning("kill switch refresh failed: %s", exc)
            return False
        # The exchange-side switch is re-armed, but the §4.1 condition only
        # re-opens if no failure is outstanding: after a refresh failure,
        # release goes through PR 4's §13.4 reconciliation, not through one
        # lucky refresh — the sticky flag is enforced here, at the gate,
        # not merely stored for PR 4 to poll.
        self._gate.kill_switch_active = not self.stop_new_orders
        self._record("kill_switch_refreshed")
        return True

    def tick(self) -> None:
        """Refresh if due — the live loop calls this every cycle (30s)."""
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
            self._db.conn, self._run_id, action="cancel_by_cloid", cloid_hex=cloid_hex
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
        """§18.2 rules 5–6: cancel bot-owned open orders; never force-close.

        Per-order failures are recorded and do not stop the sweep — every
        remaining order still gets its cancel attempt, and whatever survives
        is covered by the still-armed exchange-side scheduleCancel deadline.
        """
        self._record("shutdown_cancel_orders_started")
        canceled: list[str] = []
        skipped_non_bot: list[str] = []
        failures: list[str] = []
        sweep_error: str | None = None
        try:
            open_orders = self._client.open_orders()
        except Exception as exc:
            # Can't enumerate: the scheduled cancel deadline is the backstop.
            open_orders = []
            sweep_error = f"open_orders failed: {exc}"
        for order in open_orders:
            oid = str(order.get("oid", "?"))
            coin = order.get("coin")
            cloid = order.get("cloid")
            row = repo.get_cloid_by_hex(self._db.conn, cloid) if isinstance(cloid, str) else None
            if row is None or coin is None:
                # §19.3: unknown cloid (or none) = non-bot-owned; §25 — never
                # manage it. PR 4's reconciliation escalates to manual safe mode.
                skipped_non_bot.append(oid)
                continue
            try:
                self._cancel_with_evidence(
                    coin=coin, cloid_hex=cloid, cloid_logical=row["cloid_logical"]
                )
            except Exception as exc:
                # ANY per-order failure — exchange, gate, or persistence-layer
                # (ValueError/sqlite3.*) — must not stop the sweep: the
                # remaining orders still get their cancel attempt, and the
                # completed event must always be written.
                failures.append(f"{oid}: {type(exc).__name__}: {exc}")
                continue
            canceled.append(oid)
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
