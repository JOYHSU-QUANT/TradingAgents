"""§17 live stop-loss / take-profit protection manager (PR 5).

The Phase 2 protection invariant, now enforced with REAL reduce-only trigger
orders on the exchange (§17.1 rule 8) instead of the paper engine's simulated
trigger prices:

    position == 0            → no active SL / TP
    position != 0            → SL covers the whole position
    execution plan terminal  → TP covers the whole position

The SL/TP prices come from the shared pure math (:mod:`..paper.stops` — the same
functions the paper engine uses), so the two engines can never disagree on where
a stop sits. The lifecycle is live-only:

- SL is (re)computed after every position-changing fill; it is placed, or moved
  in place with ``modify`` (§17.4 modify-before-cancel), to cover the current
  size at the current entry;
- a create/modify that fails is repaired up to ``sl_repair_max_attempts`` times
  ``sl_repair_retry_delay_seconds`` apart; still failing, the position is
  emergency-closed (§17.2) — the manager reports ``NEEDS_EMERGENCY_CLOSE`` and
  the engine performs the aggressive reduce-only IOC (§9.4);
- TP is suspended while a slice plan runs (§17.1 rule 5, SL kept) and
  (re)established once the plan is terminal (rule 6); a TP failure does NOT
  close — it enters degraded protection (§17.3): SL stays, new entry/rebalance
  stops (``gate.unresolved_protection_failure``), repair keeps trying;
- a position that reaches flat has its resting SL/TP cancelled (rule 4).

Every SL/TP order carries its own cloid (§8.2, registered for §19.3 bot-owned
lookup), an ``orders`` row (role ``stop_loss`` / ``take_profit``, type
``stop_market`` / ``take_market``), and a ``protection_order_events`` audit row.
The gate's §4.1 ``unresolved_protection_failure`` line is this module's to move:
raised whenever the position lacks a valid SL/TP it should have, lowered when
protection is whole.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, localcontext
from enum import Enum

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..exchanges.hyperliquid.errors import ExchangeError, ExchangeThrottledError
from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
from ..paper.clock import Clock, WallClock
from ..paper.stops import (
    StopAction,
    StopConfig,
    round_to_tick,
    stop_loss_decision,
    take_profit_price,
)
from ..paper.twap import floor_to_step
from ..persistence import repository as repo
from ..persistence.cloid import LIVE_ORDER_ROLES, cloid_hex as derive_cloid_hex, cloid_logical
from ..persistence.db import Database
from ..persistence.models import PositionState, Side
from .config import LiveProtectionConfig
from .kill_switch import refresh_across_blocking_work
from .order_gate import LiveOrderGateRejected, RealOrderGate
from .orders import is_known_exchange_status, local_status_for_exchange_status, parse_order_status

__all__ = ["ProtectionManager", "ProtectionOutcome"]

logger = logging.getLogger(__name__)

# The two RESTING protective roles this manager owns. Deliberately narrower
# than order_gate.PROTECTIVE_ORDER_ROLES: emergency_close is a one-shot IOC,
# never a resting trigger, so it has no row for a clear/residual sweep to find.
_SLTP_ROLES = ("stop_loss", "take_profit")
_ROLE_ORDER_TYPE = {"stop_loss": "stop_market", "take_profit": "take_market"}
_ROLE_TPSL = {"stop_loss": "sl", "take_profit": "tp"}
# Literal copies of LIVE_ORDER_ROLES members: a role renamed there without this
# file would silently drop it from the clear/residual sweeps. Fail at import
# (same guard family as startup.py's partition check).
if not set(_SLTP_ROLES) <= LIVE_ORDER_ROLES:
    raise AssertionError("_SLTP_ROLES drifted from LIVE_ORDER_ROLES")
if set(_ROLE_ORDER_TYPE) != set(_ROLE_TPSL) or set(_ROLE_TPSL) != set(_SLTP_ROLES):
    raise AssertionError("protection role tables must cover exactly _SLTP_ROLES")
# Keys are not enough: _ROLE_ORDER_TYPE is a literal copy of the repository's
# role→order_type mapping, and only its VALUES decide how an audit row is
# labelled. Left key-checked, a changed order_type spelling would have this
# file writing the old label while reconcile.py's orphan backfill writes the
# new one — the same logical SL carrying two order_types depending on which
# path recorded it. Compare the whole mapping (strictly stronger).
if {r: repo.ROLE_TO_ORDER_TYPE[r] for r in _SLTP_ROLES} != _ROLE_ORDER_TYPE:
    raise AssertionError("_ROLE_ORDER_TYPE drifted from repository.ROLE_TO_ORDER_TYPE")
# Bound, not re-typed: _row_still_rests asks the EXCHANGE the same "is it still on
# the book" question active_protection_order asks SQLite, and the two answering
# from different status vocabularies is precisely the drift this file's other
# import-time guards exist to prevent.
_RESTING_ORDER_STATUSES = frozenset(repo.RESTING_ORDER_STATUSES)

# §9.4 aggressive family (decided 2026-07-22): a stop-loss trigger only FIRES in
# the violent move it protects against, so its fire-time limit needs the same
# "marketable under stress, still price-bounded" band the emergency close uses —
# a routine ±0.5% band can be gapped straight through, leaving the fired SL
# resting unfilled while the position keeps losing (and the next sync reading
# PROTECTED off the untouched trigger/qty row). Floor, not override: a routine
# band configured wider than this wins. The take-profit keeps the routine band —
# a missed TP is opportunity cost, a missed SL is an uncapped loss. Keep in sync
# with engine._EMERGENCY_SLIPPAGE_PCT (same §9.4 rationale, same 3%).
_SL_FIRE_BAND_FLOOR_PCT = Decimal("0.03")

# Ceiling on ONE backoff sleep inside the repair ladder. The ladder blocks the
# single-threaded tick, and the §18.2 invariant every entry point preflights is
# stated against the caller's worst-case tick gap (cli's _RECOVERY_MAX_TICK_GAP
# _SECONDS, 30s). A backoff that grew past it would break the very promise the
# preflight proved. Never shortens a configured delay — a delay already longer
# than this is the operator's own choice, and sl_repair_delay_warning warns at or
# above the same one-third budget.
#
# ONE THIRD of that gap, not all of it. The sleep is not alone in the stretch
# between two refreshes: ``_maybe_delay``'s own docstring says every rung reaches
# it after a wire call, and the ExchangeError lane after a wire call AND its
# orderStatus recovery probe — two full ``network_timeout_s`` back to back. A
# clamp set to the WHOLE gap therefore permitted 8 + 8 + 30 = 46s inside a 30s
# promise at the RUNBOOK's recommended timeout, i.e. the constant contradicted
# the docstring three lines below it. Three slots is the budget
# ``network_timeout_warning`` already enforces (``_MAX_UNREFRESHED_REST_CALLS``
# = 3, which is why it demands ``timeout * 3 < max_tick_gap``): the two timeouts
# take two slots, this sleep takes the third (2026-08-01 round-13 concept scan).
_MAX_REPAIR_SLEEP_S = 10.0


class _EstablishResult(Enum):
    """How one §17.4 place/modify ladder ended (internal to the manager)."""

    ESTABLISHED = "established"  # the order is acknowledged resting on the book
    EXHAUSTED = "exhausted"  # real wire failures burned every repair attempt
    # Every failure was a PRE-SEND §4.1 gate refusal (nothing ever transmitted).
    # With the protective entrypoint exempting the safe-mode lines (§13.1), in
    # practice this means the kill switch: escalating to an emergency close is
    # futile (the same gate blocks it) — hold and retry next sync instead.
    GATE_BLOCKED = "gate_blocked"
    # Every failure that reached the wire was the venue REFUSING TO SERVE the
    # request (429 / overload), never a rejection of the order itself. Same
    # disposition as GATE_BLOCKED — hold, retry next sync — for the same reason:
    # escalating is wrong when nothing said this order cannot exist. §17.2's
    # emergency close would market-out a healthy position and latch the run for
    # a human, and a venue throttles hardest in exactly the violent move a stop
    # exists for, so "rate limited" read as "this stop can never be placed" is
    # the worst possible time to be wrong (2026-07-31 partial-failure review).
    THROTTLED = "throttled"


class ProtectionOutcome(str, Enum):
    """What the §17 sync established for the current position."""

    FLAT = "flat"  # position is flat; any resting SL/TP was cancelled
    PROTECTED = "protected"  # SL (and TP when no plan runs) are on the book
    DEGRADED = "degraded"  # §17.3: SL up but TP could not be (re)established
    NEEDS_EMERGENCY_CLOSE = "needs_emergency_close"  # §17.2: no safe SL — engine must close
    # The wire gate refused every SL attempt PRE-SEND (kill switch). The SL the
    # sync WANTED is not on the book — a previous one may still be, covering or
    # not (the event records which) — but escalating is futile (the same gate
    # blocks the close), so the gate line stays up, the sync retries next tick,
    # and §12.3 SL-missing is the standing net.
    BLOCKED = "blocked"


class ProtectionManager:
    """One run's §17 SL/TP order lifecycle over a single symbol."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        coin: str,
        client: HyperliquidSignedClient,
        gate: RealOrderGate,
        tick_size: Decimal,
        qty_step: Decimal,
        stop_config: StopConfig,
        max_slippage_pct: Decimal,
        protection_config: LiveProtectionConfig,
        owner_prefix: str,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        kill_switch=None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._coin = coin
        self._client = client
        self._gate = gate
        self._tick = tick_size
        self._qty_step = qty_step
        self._stop = stop_config
        self._slippage = max_slippage_pct
        self._config = protection_config
        self._prefix = owner_prefix
        self._clock = clock or WallClock()
        self._sleep = sleep or time.sleep
        # §18.2 / §10.3 kill-switch timing: SL/TP repair blocks the single-threaded
        # tick with a synchronous delay between attempts. Refreshing the dead man's
        # switch across that delay (as the startup sweep does) keeps the refresh
        # cadence inside ``max_tick_gap`` even during a repair episode, so the
        # switch cannot self-trip when the system is least healthy.
        self._kill_switch = kill_switch
        # §12.2 rule 6 signal: whether the LAST sync() placed / modified /
        # cancelled any exchange order — the engine reconciles that tick.
        self._orders_changed = False
        # Latched when the kill switch is observed to have FIRED. While it is up,
        # an ``orders`` row is not evidence in ITSELF that anything rests on the
        # exchange: a fired scheduleCancel wipes the wallet's book without
        # touching SQLite. What restores a row's standing is a positive
        # orderStatus confirmation OF THAT ROW, recorded in ``_confirmed_cloid``
        # below — the latch itself stays up, because a firing invalidates rows
        # this manager has not looked at yet and will place later.
        #
        # A latch rather than a live read, because §13.4 reopens the gate while
        # the rows are still stale — the staleness outlives the outage.
        #
        # Keyed on the switch's FIRING COUNT, not on ``gate.kill_switch_active``.
        # The gate also drops on a single failed refresh, which cancels nothing:
        # keying on it made one network blip force a fail-closed orderStatus read
        # that the same blip would fail, so a still-protected position recorded a
        # window that only a stop_loss_placed can close — and the no-op path never
        # writes one. That is the "one blip fails an otherwise-healthy 30-cycle
        # run" bug this file already fixed once, reintroduced from the other side
        # (2026-07-31 exit check).
        self._rows_unconfirmed = False
        # PER ROLE, because the suspicion is per ORDER. ``_rows_unconfirmed`` says
        # "a firing invalidated the rows"; this says WHICH row has since been
        # positively confirmed, as role -> the cloid that was confirmed. A single
        # shared "spent" flag was wrong in one specific, routine way: sync always
        # establishes the SL first, so the SL's confirmation cleared the latch
        # before the TP guard ever ran, and the TP then took the free path on the
        # strength of evidence about a DIFFERENT order. After a firing the SL is
        # commonly a freshly placed row, so its resting says nothing at all about
        # the stale TP beside it — and a TP reported as resting when the switch
        # cancelled it lifts §17.3's DEGRADED block and keeps
        # position_protection.take_profit_price asserting a price that is gone
        # (2026-08-01 malformed-response review).
        #
        # Bounded at one entry per role by construction (there is one active row
        # per role), and reset wholesale on the next firing.
        self._confirmed_cloid: dict[str, str] = {}
        self._last_seen_firings = 0

    @property
    def orders_changed_last_sync(self) -> bool:
        """Whether the last :meth:`sync` changed any exchange order (§12.2 rule 6)."""
        return self._orders_changed

    def _note_firings(self) -> None:
        """Latch row-suspicion if the switch has fired since we last looked.

        Called from :meth:`sync`'s top as well as from :meth:`_row_still_rests`,
        because that method sits at the tail of two ``and`` chains: an earlier
        term (a resized qty, a drifted trigger) short-circuits it away, and the
        firing would then go unobserved until some later sync happened to reach
        it — by which time §13.4 has reopened the gate and the no-op path is
        reading a stale row as proof of protection.
        """
        firings = getattr(self._kill_switch, "fired_total", 0)
        if firings > self._last_seen_firings:
            self._last_seen_firings = firings
            self._rows_unconfirmed = True
            # A NEW firing invalidates every earlier confirmation: an order
            # confirmed resting before this scheduleCancel is exactly what the
            # scheduleCancel just cancelled.
            self._confirmed_cloid.clear()

    def _row_still_rests(self, row, *, role: str) -> bool:
        """Whether ``row``'s order is CONFIRMED still resting on the exchange.

        Fast path: while the kill switch has never FIRED, nothing can have
        mass-cancelled the book behind our back, so the local row stands on its
        own and this costs nothing. After a firing the rows are suspect until
        orderStatus says otherwise (§4.1 gates MUTATIONS, not reads, so this
        question is always askable).

        Fail-CLOSED on every uncertainty — unreadable payload, transport error,
        a status word that is not a resting one — matching
        ``reconcile._has_valid_sl``, which answers the same question from
        ``open_orders`` and reads ``None`` as False. A false "still rests" leaves
        a position naked while the audit trail calls it protected; a false "gone"
        costs one redundant re-place of an order we want resting anyway.
        """
        self._note_firings()
        if not self._rows_unconfirmed:
            return True
        hexid = row["cloid_hex"]
        if hexid and self._confirmed_cloid.get(role) == hexid:
            # THIS order was positively confirmed since the last firing. Confirming
            # a sibling role buys nothing here (see _confirmed_cloid).
            return True
        if not hexid:
            # No cloid to ask about (a pre-cloid or hand-repaired row). Cannot be
            # confirmed, so it does not count as evidence.
            return False
        try:
            parsed = parse_order_status(self._client.query_order_by_cloid(str(hexid)))
        except Exception:  # noqa: BLE001 — an unresolvable read must not crash the tick
            logger.warning(
                "orderStatus check for the resting %s (cloid %s) could not resolve — "
                "treating it as NOT resting (fail-closed)",
                role,
                hexid,
                exc_info=True,
            )
            return False
        finally:
            # §18.2: that read just blocked the single-threaded tick for up to a
            # full network timeout, and ONE sync can reach here three times (the
            # SL no-op guard, the SL covering check on a gate-blocked repair, the
            # TP no-op guard) because only a POSITIVE confirmation of THAT order
            # caches — so the repeats are exactly the degraded-network case.
            # In ``finally`` for the same reason: the timing-out read is the one
            # that costs the most and the one that returns early
            # (2026-07-31 deadline review).
            refresh_across_blocking_work(self._kill_switch, what="orderStatus confirmation")
        if parsed is None:
            # The documented unknownOid marker: the exchange has never seen it,
            # or no longer carries it. Either way nothing of ours rests.
            return False
        # KNOWN and resting, not merely "maps to a resting word": an unknown word
        # maps to "open" by design (see is_known_exchange_status), and "open" is a
        # resting status — so delegating the whole question would answer "yes, it
        # rests" for a word we cannot classify, which is the exact fail-OPEN this
        # docstring promises not to do (2026-08-01 malformed-response review).
        if not is_known_exchange_status(parsed[1]):
            logger.warning(
                "orderStatus answered %r for the resting %s (cloid %s) — a word this "
                "build cannot classify, so it is NOT evidence of a resting order "
                "(fail-closed)",
                parsed[1],
                role,
                hexid,
            )
            return False
        if local_status_for_exchange_status(parsed[1]) not in _RESTING_ORDER_STATUSES:
            return False
        # Positively confirmed live — for THIS order only. The next sync takes the
        # free path for this cloid; every other row stays suspect until it is
        # confirmed on its own evidence.
        self._confirmed_cloid[role] = str(hexid)
        return True

    # -- public entry ---------------------------------------------------------

    def sync(
        self,
        *,
        position: PositionState,
        liquidation_price: Decimal | None,
        mark: Decimal,
        plan_active: bool,
    ) -> ProtectionOutcome:
        """Bring on-exchange protection into line with the current position (§17).

        ``plan_active`` suspends the TP for the duration of a slice plan (§17.1
        rule 5) while always keeping the SL. Sets the gate's
        ``unresolved_protection_failure`` line from the result.
        """
        now = self._clock.now()
        self._orders_changed = False
        # Sample the firing count BEFORE any short-circuiting predicate can hide
        # it (see _note_firings): the engine can also skip _sync_protection
        # entirely on a bad market-data snapshot, which is caused by the same
        # outage that lapses a deadline.
        self._note_firings()
        # Whether this sync began already carrying an unresolved protection failure
        # — so a recovery to PROTECTED can emit degraded_protection_cleared (the §17
        # audit trail should show protection RESTORED, not only degradation entered).
        was_failed = self._gate.unresolved_protection_failure
        if position.is_flat:
            self._clear(now)
            residual = next(
                (
                    role
                    for role in _SLTP_ROLES
                    if repo.active_protection_order(self._db.conn, self._run_id, self._coin, role)
                    is not None
                ),
                None,
            )
            if residual is not None:
                # §17.1 rule 4 is NOT yet met: a reduce-only trigger the cancel
                # could not confirm gone still rests on the exchange. Reporting
                # FLAT here would lower the gate line and admit a NEW entry
                # under a stale trigger that can fire against it — mirror the
                # active-plan posture instead: degrade, keep the failure line
                # up, and retry the cancel every sync until it is confirmed
                # gone (§12.3 / the §18.2 sweep remain the standing nets).
                self._gate.unresolved_protection_failure = True
                self._record_event(
                    "degraded_protection_entered",
                    detail=(
                        f"flat but the resting {residual} could not be cancelled (§17.1 rule 4)"
                    ),
                    now=now,
                )
                return ProtectionOutcome.DEGRADED
            if was_failed:
                self._record_event(
                    "degraded_protection_cleared",
                    detail="flat and no protection orders rest",
                    now=now,
                )
            self._gate.unresolved_protection_failure = False
            return ProtectionOutcome.FLAT

        assert position.entry_price is not None  # a sized position always has one
        # ``side`` is the POSITION direction (buy = long), as the paper engine
        # passes it — stops.py bands off that, and the closing order is the
        # opposite direction (a long's SL is a sell).
        pos_side = Side.BUY if position.size > 0 else Side.SELL
        sl_decision = stop_loss_decision(
            side=pos_side,
            entry_price=position.entry_price,
            liquidation_price=liquidation_price,
            tick_size=self._tick,
            config=self._stop,
        )
        if sl_decision.action is StopAction.CLOSE_NOW:
            # §3.6 / §17.2: no band leaves a safe SL (liquidation too close, or
            # the entry is already out of range) — the engine must close now.
            self._gate.unresolved_protection_failure = True
            self._record_event(
                "stop_loss_repair_exhausted",
                detail=f"no safe SL band: {sl_decision.reason}",
                now=now,
            )
            return ProtectionOutcome.NEEDS_EMERGENCY_CLOSE

        assert sl_decision.price is not None
        sl_result = self._establish("stop_loss", position, sl_decision.price, now)
        if sl_result in (_EstablishResult.GATE_BLOCKED, _EstablishResult.THROTTLED):
            # Two ways to arrive, one disposition — hold, do not escalate.
            #
            # GATE_BLOCKED: every attempt was refused PRE-SEND by the §4.1 wire
            # gate (in practice: the kill switch — the protective entrypoint
            # exempts the safe-mode lines). Nothing failed ON the exchange, and
            # the same gate would refuse the emergency close too, so escalating
            # would be a futile close storm.
            #
            # THROTTLED: every attempt that reached the wire was the venue
            # declining to SERVE it. Nothing said this order cannot exist, so
            # §17.2's market-out-and-latch would be a response to a rate limit —
            # taken during the violent move a stop exists for, which is when a
            # venue throttles hardest.
            #
            # Either way: keep the failure line up (blocks new risk), retry next
            # sync; §12.3's SL-missing check remains the standing net for the
            # unprotected window.
            self._gate.unresolved_protection_failure = True
            # §17.4 is modify-before-cancel, so a gate-refused MODIFY can leave
            # the previous SL resting while a gate-refused CREATE leaves
            # nothing. Both reach here, and the §20.3 validator has to tell them
            # apart: counting a still-protected position as unprotected seconds
            # would fail an otherwise-healthy 30-cycle run on one kill-switch
            # refresh blip. The distinction is recorded as this event's
            # ``order_id`` — but the test is COVERAGE, not existence. §17.1
            # rule 1 is a coverage rule and reconcile._has_valid_sl agrees
            # (closing side + qty >= position), and the commonest blocked MODIFY
            # is a RESIZE: a later slice filled and the resting SL now
            # under-covers. Stamping it on the strength of "a row exists" would
            # report "protected" while part of the position carries no stop at
            # all. Compare on the same floored basis _establish uses, or step
            # rounding alone re-opens the window.
            resting = repo.active_protection_order(
                self._db.conn, self._run_id, self._coin, "stop_loss"
            )
            with localcontext(DECIMAL_CONTEXT):
                needed = floor_to_step(abs(position.size), self._qty_step)
            closing_side = (Side.BUY if position.size < 0 else Side.SELL).value
            # EXISTENCE of the row is not evidence here, and this branch is the
            # one place where that is guaranteed: the only §4.1 line that refuses
            # a PROTECTIVE order is the kill switch, so reaching this code means
            # the switch is down — and a switch that went down by LAPSING its
            # deadline has already had the exchange cancel every order on the
            # wallet, leaving these rows untouched and wrong. Confirm against
            # orderStatus before letting the row suppress a §20.3 window; a read
            # that cannot confirm leaves ``covering`` None, so the window opens
            # (fail-closed, matching reconcile._has_valid_sl).
            covering = (
                resting
                if resting is not None
                and resting["side"] == closing_side
                and Decimal(resting["qty"]) >= needed
                and self._row_still_rests(resting, role="stop_loss")
                else None
            )
            shortfall = (
                ""
                if resting is None
                else f" (resting {resting['side']} {resting['qty']} vs {needed} needed)"
            )
            self._record_event(
                "stop_loss_repair_blocked",
                # Only a COVERING order suppresses the unprotected window.
                order_id=None if covering is None else covering["order_id"],
                cloid_hex=None if covering is None else covering["cloid_hex"],
                detail=(
                    (
                        "wire gate refused every SL attempt pre-send (kill switch); retrying "
                        if sl_result is _EstablishResult.GATE_BLOCKED
                        else "the exchange rate-limited every SL attempt (nothing was "
                        "rejected, only unserved); retrying "
                    )
                    + (
                        "next sync — the previous SL still rests and covers the position"
                        if covering is not None
                        else f"next sync — NO covering stop-loss rests{shortfall}"
                    )
                ),
                now=now,
            )
            return ProtectionOutcome.BLOCKED
        if sl_result is not _EstablishResult.ESTABLISHED:
            # §17.2: SL repair exhausted → emergency close.
            self._gate.unresolved_protection_failure = True
            self._record_event(
                "stop_loss_repair_exhausted",
                detail=f"{self._config.sl_repair_max_attempts} SL attempts failed",
                now=now,
            )
            return ProtectionOutcome.NEEDS_EMERGENCY_CLOSE

        if plan_active:
            # §17.1 rule 5: TP suspended while a plan runs; SL stays. Cancel a
            # stale TP so it can't fire against the in-flight plan.
            self._cancel_role("take_profit", now)
            if (
                repo.active_protection_order(self._db.conn, self._run_id, self._coin, "take_profit")
                is not None
            ):
                # The cancel could not land — a stale reduce-only TP is still
                # resting and can fire mid-plan, selling into the very position the
                # plan is building. That is not "protected": degrade so the gate
                # blocks new entry/rebalance until a later tick confirms the TP is
                # gone (the cancel is retried every sync). Never report PROTECTED
                # over an un-suspended TP (the raw cancel result was previously
                # discarded, masking exactly this).
                self._gate.unresolved_protection_failure = True
                self._record_event(
                    "degraded_protection_entered",
                    detail="stale take-profit could not be suspended during active plan (§17.1 rule 5)",
                    now=now,
                )
                return ProtectionOutcome.DEGRADED
            # The TP is provably off the book (the still-resting check above
            # returned), so drop the recorded price too. Previously only
            # ``_clear`` (the flat path) nulled these columns, which left the
            # row — and therefore every position_snapshots / ai_inputs row and
            # both CSV exports written during the plan window — asserting a
            # take-profit that no longer existed, for as long as the plan ran.
            # Written AFTER the still-resting check on purpose: in the DEGRADED
            # case a stale TP really IS still on the exchange and the column
            # should keep saying so. Rule 6 re-establishment restores the value
            # through _persist_placed.
            recorded = repo.get_position_protection(self._db.conn, self._run_id, self._coin)
            if recorded is not None and recorded[1] is not None:
                with self._db.transaction() as conn:
                    self._write_protection_price(conn, "take_profit", None, now)
            if was_failed:
                self._record_event(
                    "degraded_protection_cleared",
                    detail="protection restored (SL up, TP suspended for the active plan)",
                    now=now,
                )
            self._gate.unresolved_protection_failure = False
            return ProtectionOutcome.PROTECTED

        tp_price = take_profit_price(
            side=pos_side,
            entry_price=position.entry_price,
            tick_size=self._tick,
            config=self._stop,
        )
        tp_ok = (
            self._establish("take_profit", position, tp_price, now) is _EstablishResult.ESTABLISHED
        )
        if not tp_ok:
            # §17.3: TP failure does NOT close — degraded protection (SL kept),
            # new entry/rebalance blocked until TP is repaired.
            self._gate.unresolved_protection_failure = True
            self._record_event(
                "degraded_protection_entered",
                detail="take-profit could not be established (§17.3)",
                now=now,
            )
            return ProtectionOutcome.DEGRADED

        if was_failed:
            self._record_event(
                "degraded_protection_cleared",
                detail="protection restored (SL and TP re-established)",
                now=now,
            )
        self._gate.unresolved_protection_failure = False
        return ProtectionOutcome.PROTECTED

    # -- SL/TP establishment (place or modify, with §17.2 repair) -------------

    def _establish(
        self, role: str, position: PositionState, trigger_price: Decimal, now: datetime
    ) -> _EstablishResult:
        """Place, or modify in place, the resting ``role`` order to cover the position.

        Modify-before-cancel (§17.4): an existing order with an exchange id is
        moved with ``modify``; otherwise a fresh order is placed. Retries up to
        ``sl_repair_max_attempts`` on failure, ``sl_repair_retry_delay_seconds``
        apart. ``ESTABLISHED`` once the order is acknowledged on the book;
        ``GATE_BLOCKED`` when every failure was a pre-send §4.1 refusal (nothing
        transmitted — not a repair exhaustion); ``EXHAUSTED`` otherwise.

        v1 LIMITATION (deferred to the PR 6 restart recovery, alongside the
        ``_emergency_close_pending`` persistence): unlike ``LiveOrderSubmitter``
        this does NOT write intent BEFORE the wire call — a crash between a
        successful ack and ``_persist_placed`` leaves a live, resting SL/TP with no
        ``orders`` / ``cloid_registry`` row, which reconciliation then reads as
        §12.3 SL-missing. The §12.3 SL-missing recoverable safe mode is the interim
        net (and, with the protective-gate fix, the repair it triggers can still
        send); intent-first protection writes land with restart recovery.
        """
        with localcontext(DECIMAL_CONTEXT):
            size = floor_to_step(abs(position.size), self._qty_step)
        if size <= 0:
            return _EstablishResult.EXHAUSTED
        # Closing direction: a long (size > 0) is protected by a SELL, a short
        # by a BUY — the reduce-only order that shrinks the position.
        is_buy = position.size < 0
        trigger_price = round_to_tick(trigger_price, self._tick, up=is_buy)
        limit_price = self._protected_limit(trigger_price, is_buy, role=role)

        existing = repo.active_protection_order(self._db.conn, self._run_id, self._coin, role)
        existing_oid = existing["exchange_order_id"] if existing is not None else None
        # A no-op: the resting order already covers this size at this trigger, on
        # the closing side. SIDE is part of the test for the same reason it is part
        # of the ``covering`` computation in sync's GATE_BLOCKED branch below:
        # trigger and qty alone can match an order left over from the OPPOSITE
        # direction (a same-size flip whose old SL was never confirmed cancelled),
        # and returning ESTABLISHED over it would report PROTECTED while nothing on
        # the book actually closes the current position.
        #
        # But the row is only evidence while the dead man's switch is healthy.
        # ``scheduleCancel`` firing cancels EVERY order on the wallet and touches
        # no SQLite: kill_switch.py's own ERROR says "local order rows are stale
        # until then". Every field this test reads — side, trigger, qty — survives
        # that untouched, so the match is 100% reliable and 100% wrong. That is
        # WORSE than the GATE_BLOCKED branch's stale read below: this returns
        # ESTABLISHED before any wire call, so no protection event is written at
        # all (the §20.3 unprotected window never opens) and the outcome is
        # PROTECTED, which LOWERS gate.unresolved_protection_failure. The part
        # that actually costs money: once §13.4 reopens the gate this same stale
        # row keeps matching, so the SL the exchange cancelled is never re-placed
        # until reconciliation settles the row a heartbeat later — the no-op
        # actively fights the recovery (2026-07-31 stale-evidence review).
        if (
            existing is not None
            and existing_oid is not None
            and existing["side"] == (Side.BUY if is_buy else Side.SELL).value
            and existing["trigger_price"] is not None
            and Decimal(existing["trigger_price"]) == trigger_price
            and existing["qty"] is not None
            and Decimal(existing["qty"]) == size
            and self._row_still_rests(existing, role=role)
        ):
            return _EstablishResult.ESTABLISHED

        # One cloid sequence per placement, computed BEFORE the retry loop so
        # all attempts of THIS placement reuse the same cloid (§8.3 idempotent
        # resend); the next placement's count is one higher (a modify is a new
        # logical identity, §17.4).
        seq = repo.count_orders_by_role(self._db.conn, self._run_id, self._coin, role)
        side = Side.BUY if is_buy else Side.SELL
        attempts = self._config.sl_repair_max_attempts
        # Flipped the moment any attempt reaches (or may have reached) the wire;
        # a ladder that ends with this still True never transmitted anything.
        all_gate_blocked = True
        # Throttle bookkeeping: a ladder whose only wire-reaching failures were
        # the venue declining to SERVE us must not escalate (see
        # _EstablishResult.THROTTLED). One rejection of the ORDER anywhere in the
        # ladder is enough to make this an ordinary exhaustion again.
        saw_throttle = False
        saw_other_wire_failure = False
        for attempt in range(1, attempts + 1):
            order_id, logical, hexid = self._mint_ids(role, seq)
            try:
                if existing_oid is not None:
                    ack = self._client.modify_trigger_order(
                        target=str(existing_oid),
                        coin=self._coin,
                        is_buy=is_buy,
                        size=size,
                        limit_price=limit_price,
                        trigger_price=trigger_price,
                        tpsl=_ROLE_TPSL[role],
                        cloid_hex=hexid,
                        reduce_only=True,
                    )
                else:
                    ack = self._client.place_trigger_order(
                        coin=self._coin,
                        is_buy=is_buy,
                        size=size,
                        limit_price=limit_price,
                        trigger_price=trigger_price,
                        tpsl=_ROLE_TPSL[role],
                        cloid_hex=hexid,
                        reduce_only=True,
                    )
            except LiveOrderGateRejected as exc:
                # Raised by the bound §4.1 gate INSIDE place/modify, BEFORE any
                # network I/O — nothing reached the exchange, so nothing can be
                # resting (no orderStatus recovery). Since the protective
                # entrypoint exempts the safe-mode lines (§13.1), only the kill
                # switch (or a base precondition) refuses here. The delay still
                # runs — it ticks the kill switch, so a mid-ladder refresh can
                # reopen the gate — but a pre-send refusal never counts as a
                # FAILED repair: a ladder of nothing-but-these resolves to
                # GATE_BLOCKED (hold, no escalation), not EXHAUSTED.
                self._log_attempt_failed(role, attempt, attempts, existing_oid, exc, now)
                self._maybe_delay(attempt, attempts)
                continue
            except ExchangeThrottledError as exc:
                all_gate_blocked = False
                saw_throttle = True
                # No orderStatus recovery probe here: it would ride the SAME
                # rate limiter that just refused us, so it fails too and buys
                # nothing but another request against the budget. The next sync
                # re-establishes from scratch, and a landed-but-ack-lost order
                # is picked up then (or by §12.3 reconciliation).
                self._log_attempt_failed(role, attempt, attempts, existing_oid, exc, now)
                self._maybe_delay(attempt, attempts, backoff=attempt)
                continue
            except ExchangeError as exc:
                all_gate_blocked = False
                saw_other_wire_failure = True
                # Unknown outcome (§8.3 rule 11): a lost ack / timeout may have left
                # the order LIVE on the exchange. Ask orderStatus BEFORE counting a
                # failure — a landed-but-ack-lost SL/TP must not be blind-resent
                # (the resend hits a duplicate-cloid rejection) and must NOT drive a
                # spurious emergency close of an already-protected position.
                #
                # §18.2: refresh BETWEEN the two. The lane is reached by a wire
                # call that failed — commonly by timing out, i.e. having ridden
                # the full network timeout — and the recovery probe below is a
                # second one. Back to back they are the longest blocking stretch
                # in the ladder, and the rung's own refresh only comes after
                # (2026-07-31 deadline review).
                refresh_across_blocking_work(self._kill_switch, what="SL/TP repair")
                if self._recover_placed_order(
                    role=role,
                    order_id=order_id,
                    logical=logical,
                    hexid=hexid,
                    size=size,
                    side=side,
                    trigger_price=trigger_price,
                    limit_price=limit_price,
                    replaced=existing,
                    now=now,
                ):
                    return _EstablishResult.ESTABLISHED
                self._log_attempt_failed(role, attempt, attempts, existing_oid, exc, now)
                self._maybe_delay(attempt, attempts)
                continue

            all_gate_blocked = False  # an ack came back — this attempt reached the wire
            # §18.2: an ack means that call was ON the network. Refresh here and
            # the invariant over this whole ladder becomes "every wire call is
            # followed by a refresh", which the exception lanes and _maybe_delay
            # cover on their own — but the two ESTABLISHED returns below do NOT
            # reach _maybe_delay, so without this a successful SL place followed
            # by a successful TP place is two round-trips inside one gap, and the
            # duplicate lane below is a place plus an orderStatus probe. Cheap to
            # repeat: tick() reaches the wire only when a refresh is actually due
            # (2026-07-31 deadline review).
            refresh_across_blocking_work(self._kill_switch, what="SL/TP repair")
            if ack.accepted:
                self._persist_placed(
                    role=role,
                    order_id=order_id,
                    logical=logical,
                    hexid=hexid,
                    size=size,
                    side=side,
                    trigger_price=trigger_price,
                    limit_price=limit_price,
                    exchange_order_id=ack.exchange_order_id,
                    exchange_raw_status=ack.status,
                    replaced=existing,
                    now=now,
                )
                return _EstablishResult.ESTABLISHED

            if ack.is_duplicate:
                recovered = self._recover_placed_order(
                    role=role,
                    order_id=order_id,
                    logical=logical,
                    hexid=hexid,
                    size=size,
                    side=side,
                    trigger_price=trigger_price,
                    limit_price=limit_price,
                    replaced=existing,
                    now=now,
                )
                # §18.2: that probe was a REST call, and the branch below RETURNS
                # without passing _maybe_delay — the one exit from this ladder
                # that reaches the wire and then leaves without a refresh. Left
                # uncovered, an SL recovered this way and the TP ``_establish``
                # that sync() runs next share a single gap.
                refresh_across_blocking_work(self._kill_switch, what="SL/TP repair")
                if recovered:
                    # The exchange already knows this cloid from a prior attempt
                    # whose ack we never observed; orderStatus confirms it is
                    # resting (§8.3 rules 2–4) → recovered, no resend.
                    return _EstablishResult.ESTABLISHED

            # An ack that says "no" is the exchange REJECTING the order, which is
            # exactly the evidence a throttle is not.
            saw_other_wire_failure = True
            self._log_attempt_failed(role, attempt, attempts, existing_oid, ack.error, now)
            self._maybe_delay(attempt, attempts)
        if all_gate_blocked:
            return _EstablishResult.GATE_BLOCKED
        if saw_throttle and not saw_other_wire_failure:
            return _EstablishResult.THROTTLED
        return _EstablishResult.EXHAUSTED

    def _log_attempt_failed(
        self,
        role: str,
        attempt: int,
        attempts: int,
        existing_oid,
        error,
        now: datetime,
    ) -> None:
        logger.warning(
            "%s %s attempt %d/%d failed: %s",
            "modify" if existing_oid else "place",
            role,
            attempt,
            attempts,
            error,
        )
        self._record_event(f"{role}_repair_failed", detail=f"attempt {attempt}: {error}", now=now)

    def _recover_placed_order(
        self,
        *,
        role: str,
        order_id: str,
        logical: str,
        hexid: str,
        size: Decimal,
        side: Side,
        trigger_price: Decimal,
        limit_price: Decimal,
        replaced,
        now: datetime,
    ) -> bool:
        """§8.3 rules 3–4: did this cloid actually land? Persist it if so.

        Called after an unknown outcome (an ``ExchangeError``) or a duplicate-cloid
        rejection. Queries orderStatus by cloid: a resting / live order means the
        placement succeeded and only the ack was lost — persist it (the same
        ``orders`` / registry / protection-price writes an accepted ack does) and
        report success, so a benign network blip never forces a spurious emergency
        close. An unknown cloid, or a rejected / canceled order, means no live
        protective order is resting — return False so the caller records a failed
        attempt and retries
        (and, if attempts exhaust, emergency-closes — the safe outcome for a
        genuinely un-placeable stop). Recovery only ever returns True on a POSITIVE
        confirmation; any read/parse failure is unresolved → return False (retry,
        eventually emergency-close is the safe fallback) and never crash the tick.
        """
        try:
            parsed = parse_order_status(self._client.query_order_by_cloid(hexid))
        except Exception:  # noqa: BLE001 — an unresolvable recovery must not crash the tick
            logger.warning(
                "orderStatus recovery for %s cloid %s could not resolve", role, hexid, exc_info=True
            )
            return False
        if parsed is None:
            return False  # the exchange does not know this cloid — nothing landed
        exchange_oid, exchange_status = parsed
        if not is_known_exchange_status(exchange_status):
            # A word this build cannot classify is not a POSITIVE confirmation,
            # and this method's whole contract is that it only returns True on
            # one. Testing "is it terminal?" reads the unknown-word fallback
            # ("open") as proof of life and persists a possibly-dead order as
            # live protection — the same fail-OPEN _row_still_rests carried, and
            # reachable here with no kill-switch firing at all, off nothing but a
            # lost ack (2026-08-01 malformed-response review).
            logger.warning(
                "orderStatus recovery for %s cloid %s answered %r — a word this build "
                "cannot classify, so the placement is NOT confirmed (fail-closed: the "
                "caller retries, and an exhausted ladder emergency-closes)",
                role,
                hexid,
                exchange_status,
            )
            return False
        if local_status_for_exchange_status(exchange_status) in ("canceled", "rejected"):
            # Known to the exchange but no longer a live protective order: rejected
            # (never accepted) or a *Canceled — e.g. reduceOnlyCanceled — that was
            # removed from the book. Report it as a FAILED placement so the caller
            # retries / eventually emergency-closes, never a false "protected" over a
            # stop that is not resting. (Only "rejected" was checked before, so the
            # whole Canceled family slipped through and was persisted as live.)
            return False
        logger.warning(
            "recovered %s order for cloid %s from orderStatus (%s) after a lost ack",
            role,
            hexid,
            exchange_status,
        )
        self._persist_placed(
            role=role,
            order_id=order_id,
            logical=logical,
            hexid=hexid,
            size=size,
            side=side,
            trigger_price=trigger_price,
            limit_price=limit_price,
            exchange_order_id=exchange_oid,
            exchange_raw_status=exchange_status,
            replaced=replaced,
            now=now,
        )
        return True

    def _maybe_delay(self, attempt: int, attempts: int, *, backoff: int = 1) -> None:
        """Close out one ladder rung: sleep if another follows, always refresh.

        ``backoff`` multiplies the configured delay. Retrying a rate limiter on
        a flat cadence mostly spends the budget re-triggering it; the multiplier
        is linear rather than exponential because the whole ladder still has to
        fit inside the tick's timing envelope (§18.2), which the fixed delay was
        chosen against.

        And CLAMPED to that envelope, because the multiplier alone breaks it.
        ``sl_repair_delay_warning`` only ever compares the raw configured delay
        against ``max_tick_gap``, and config bounds neither the delay nor the
        attempt count — so ``delay=20, attempts=8`` would sleep 140s on the last
        rung, past a 120s schedule_cancel, and the dead man's switch fires
        DURING the stop-loss repair (2026-07-31 exit check).

        The refresh is NOT conditional on having slept. It used to be — the
        early return on the final rung skipped it — but the sleep was never the
        only blocking thing here: every rung reaches this after a wire call, and
        the ExchangeError lane after a wire call AND its orderStatus recovery
        probe, two full timeouts back to back. Skipping the last rung left that
        traffic uncovered and then handed control back to ``sync()``, which can
        block again before the tick ends (2026-07-31 deadline review).
        """
        if attempt < attempts:
            delay = float(self._config.sl_repair_retry_delay_seconds)
            self._sleep(min(delay * backoff, max(delay, _MAX_REPAIR_SLEEP_S)))
        refresh_across_blocking_work(self._kill_switch, what="SL/TP repair")

    def _persist_placed(
        self,
        *,
        role: str,
        order_id: str,
        logical: str,
        hexid: str,
        size: Decimal,
        side: Side,
        trigger_price: Decimal,
        limit_price: Decimal,
        exchange_order_id: str | None,
        exchange_raw_status: str | None,
        replaced,
        now: datetime,
    ) -> None:
        """Record an acknowledged (or orderStatus-recovered) SL/TP: registry, orders row, protection state."""
        self._orders_changed = True  # §12.2 rule 6: an exchange order was placed/modified
        # Persist the LOCAL status the exchange actually reported, never a hardcoded
        # "open": an accepted ack can be "filled" (an immediately-fired trigger), and
        # an orderStatus-recovered order carries the exchange's real word. Recording a
        # filled order as "open" would make active_protection_order treat it as live
        # protection and let the no-op guard skip re-arming a stop that is not resting.
        local_status = (
            local_status_for_exchange_status(exchange_raw_status)
            if exchange_raw_status is not None
            else "open"
        )
        remaining = Decimal(0) if local_status == "filled" else size
        # An accepted ack for a LIVE order is the exchange telling us this cloid
        # rests — the same fact ``_row_still_rests`` would spend a full-timeout
        # orderStatus round-trip to learn, only first-hand and free. Seeding it
        # here is what keeps the per-order suspicion from costing a REST read on
        # every re-place for the rest of a process that has seen one firing: a
        # slice plan re-places the SL as the position grows, so without this the
        # tick pays for it every slice — on the very thread whose REST budget
        # this PR is otherwise busy protecting. A terminal status seeds nothing
        # (there is no resting order to vouch for) (2026-08-01 lifecycle review).
        if local_status in _RESTING_ORDER_STATUSES:
            self._confirmed_cloid[role] = hexid
        else:
            self._confirmed_cloid.pop(role, None)
        with self._db.transaction() as conn:
            repo.insert_cloid_mapping(
                conn,
                cloid_logical=logical,
                cloid_hex=hexid,
                run_id=self._run_id,
                symbol=self._coin,
                order_role=role,
                created_at=now,
            )
            repo.insert_order(
                conn,
                order_id=order_id,
                mode="live",
                run_id=self._run_id,
                symbol=self._coin,
                order_role=role,
                side=side.value,
                order_type=_ROLE_ORDER_TYPE[role],
                qty=size,
                status=local_status,
                price=limit_price,
                trigger_price=trigger_price,
                remaining_qty=remaining,
                reduce_only=True,
                cloid_logical=logical,
                cloid_hex=hexid,
                exchange_order_id=exchange_order_id,
                exchange_raw_status=exchange_raw_status,
                is_bot_owned=True,
                active_from=now,
                submitted_at=now,
                acknowledged_at=now,
                timestamp=now,
            )
            # Modify-before-cancel replaces the prior order in place; mark the
            # old row terminal so ``active_protection_order`` never returns two.
            if replaced is not None and replaced["order_id"] != order_id:
                repo.update_order(
                    conn,
                    replaced["order_id"],
                    status="canceled",
                    cancel_reason="replaced_by_protection_modify",
                    canceled_at=now,
                    updated_at=now,
                )
            self._write_protection_price(conn, role, trigger_price, now)
        self._record_event(
            f"{role}_modified" if replaced is not None else f"{role}_placed",
            order_id=order_id,
            cloid_hex=hexid,
            detail=f"trigger={trigger_price} size={size}",
            now=now,
        )

    # -- clearing (position flat / plan-active TP) ---------------------------

    def _clear(self, now: datetime) -> None:
        """§17.1 rule 4: a flat position must carry no resting SL / TP."""
        cleared_any = False
        for role in _SLTP_ROLES:
            cleared_any = self._cancel_role(role, now) or cleared_any
        # Clear the persisted trigger prices whether or not a resting order was
        # found — the position row may hold stale SL/TP after a fill zeroed it.
        if repo.get_position_protection(self._db.conn, self._run_id, self._coin) is not None:
            with self._db.transaction() as conn:
                repo.set_position_protection(
                    conn,
                    self._run_id,
                    self._coin,
                    stop_loss_price=None,
                    take_profit_price=None,
                    updated_at=now,
                )
        if cleared_any:
            self._record_event("protection_cleared", detail="position flat", now=now)

    def _cancel_role(self, role: str, now: datetime) -> bool:
        """Cancel the resting order for ``role`` if one is on the book.

        Returns whether an order was cancelled.
        """
        existing = repo.active_protection_order(self._db.conn, self._run_id, self._coin, role)
        if existing is None or existing["cloid_hex"] is None:
            return False
        try:
            ack = self._client.cancel_by_cloid(coin=self._coin, cloid_hex=existing["cloid_hex"])
        except (ExchangeError, LiveOrderGateRejected) as exc:
            # Fail-soft: a cancel that could not land — or a gate rejection raised
            # inside the client (LiveOrderGateRejected, not an ExchangeError) — is
            # a reconciliation concern (§12.3 orphan / §18.2 shutdown sweep), not a
            # reason to crash the protection pass. Leave the row; log loud.
            logger.warning("cancel of resting %s (%s) failed: %s", role, existing["order_id"], exc)
            return False
        finally:
            # §18.2: ``_clear`` calls this once per §17.1-rule-4 role, so a flat
            # position pays two of these cancels back to back every tick — and a
            # cancel that fails by timing out is precisely the one that spent the
            # whole timeout getting there (2026-07-31 deadline review).
            refresh_across_blocking_work(self._kill_switch, what="protection cancel")
        if not ack.success:
            # A NON-exceptional refusal (cancel_by_cloid returns CancelAck(success=
            # False) without raising — e.g. "already filled" / "unknown oid"): the
            # cancel is NOT confirmed, so do not mark the row canceled. A resting SL
            # that actually FILLED would otherwise be mislabeled canceled, and a
            # later-arriving fill could no longer reconcile against it. Leave the row
            # for §12.3 reconciliation / the §18.2 shutdown sweep to settle.
            logger.warning(
                "cancel of resting %s (%s) refused by exchange: %s",
                role,
                existing["order_id"],
                ack.error,
            )
            return False
        self._orders_changed = True  # §12.2 rule 6: an exchange order was cancelled
        with self._db.transaction() as conn:
            repo.update_order(
                conn,
                existing["order_id"],
                status="canceled",
                cancel_reason="protection_cleared",
                canceled_at=now,
                updated_at=now,
            )
        return True

    # -- helpers --------------------------------------------------------------

    def _protected_limit(self, trigger_price: Decimal, is_buy: bool, *, role: str) -> Decimal:
        """The §9.2 price-protected limit for a triggered reduce-only order.

        A market-like live order is forbidden (§9.2 rule 1), so the trigger
        order carries a limit set past the trigger in the fill direction:
        marketable when it fires, slippage bounded. A BUY (short's SL) fills up
        to ``trigger × (1 + slip)``; a SELL (long's SL) down to
        ``trigger × (1 - slip)``. The band is role-dependent: the stop-loss
        fires only under stress, so its band is floored at
        ``_SL_FIRE_BAND_FLOOR_PCT`` (§9.4 aggressive family); the take-profit
        keeps the routine ``max_slippage_pct``.
        """
        slip = self._slippage
        if role == "stop_loss":
            slip = max(slip, _SL_FIRE_BAND_FLOOR_PCT)
        with localcontext(DECIMAL_CONTEXT):
            limit = trigger_price * (1 + slip) if is_buy else trigger_price * (1 - slip)
        # Round toward the more marketable side so the limit never lands short
        # of the trigger's own tick (up for a buy, down for a sell).
        return round_to_tick(limit, self._tick, up=is_buy)

    def _write_protection_price(
        self, conn, role: str, price: Decimal | None, now: datetime
    ) -> None:
        """Set one role's recorded price, preserving the other's. ``None`` clears."""
        current = repo.get_position_protection(self._db.conn, self._run_id, self._coin)
        sl, tp = (None, None) if current is None else current
        if role == "stop_loss":
            sl = price
        else:
            tp = price
        repo.set_position_protection(
            conn, self._run_id, self._coin, stop_loss_price=sl, take_profit_price=tp, updated_at=now
        )

    def _mint_ids(self, role: str, seq: int) -> tuple[str, str, str]:
        """The (order_id, cloid_logical, cloid_hex) for a protection order's ``seq``.

        Distinct per placement (a modify is a new logical identity, §17.4)
        through the monotonic ``seq`` in the plan segment; all attempts of ONE
        placement share the same ``seq`` (the caller computes it once before the
        retry loop), so a §8.3 resend reuses the same cloid.
        """
        plan_id = f"p{seq}"
        logical = cloid_logical(
            prefix=self._prefix,
            run_id=self._run_id,
            symbol=self._coin,
            output_id="prot",
            plan_id=plan_id,
            leg="na",
            slice_index=0,
            order_role=role,
        )
        hexid = derive_cloid_hex(logical)
        order_id = f"{self._run_id}:{role}:{plan_id}"
        return order_id, logical, hexid

    def _record_event(
        self,
        event_type: str,
        *,
        order_id: str | None = None,
        cloid_hex: str | None = None,
        detail: str | None = None,
        now: datetime,
    ) -> None:
        with self._db.transaction() as conn:
            repo.insert_protection_order_event(
                conn,
                run_id=self._run_id,
                event_type=event_type,
                symbol=self._coin,
                order_id=order_id,
                cloid_hex=cloid_hex,
                detail=detail,
                timestamp=now,
            )
