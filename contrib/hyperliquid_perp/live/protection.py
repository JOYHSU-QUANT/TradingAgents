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
from ..exchanges.hyperliquid.errors import ExchangeError
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
from .order_gate import LiveOrderGateRejected, RealOrderGate
from .orders import local_status_for_exchange_status, parse_order_status

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


class _EstablishResult(Enum):
    """How one §17.4 place/modify ladder ended (internal to the manager)."""

    ESTABLISHED = "established"  # the order is acknowledged resting on the book
    EXHAUSTED = "exhausted"  # real wire failures burned every repair attempt
    # Every failure was a PRE-SEND §4.1 gate refusal (nothing ever transmitted).
    # With the protective entrypoint exempting the safe-mode lines (§13.1), in
    # practice this means the kill switch: escalating to an emergency close is
    # futile (the same gate blocks it) — hold and retry next sync instead.
    GATE_BLOCKED = "gate_blocked"


class ProtectionOutcome(str, Enum):
    """What the §17 sync established for the current position."""

    FLAT = "flat"  # position is flat; any resting SL/TP was cancelled
    PROTECTED = "protected"  # SL (and TP when no plan runs) are on the book
    DEGRADED = "degraded"  # §17.3: SL up but TP could not be (re)established
    NEEDS_EMERGENCY_CLOSE = "needs_emergency_close"  # §17.2: no safe SL — engine must close
    # The wire gate refused every SL attempt PRE-SEND (kill switch): no SL rests,
    # but escalating is futile (the same gate blocks the close) — the gate line
    # stays up, the sync retries next tick, §12.3 SL-missing is the standing net.
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

    @property
    def orders_changed_last_sync(self) -> bool:
        """Whether the last :meth:`sync` changed any exchange order (§12.2 rule 6)."""
        return self._orders_changed

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
        if sl_result is _EstablishResult.GATE_BLOCKED:
            # Every attempt was refused PRE-SEND by the §4.1 wire gate (in
            # practice: the kill switch — the protective entrypoint exempts the
            # safe-mode lines). Nothing failed ON the exchange, and the same
            # gate would refuse the emergency close too, so escalating would be
            # a futile close storm. Keep the failure line up (blocks new risk),
            # retry next sync once the gate reopens; §12.3's SL-missing check
            # remains the standing net for the unprotected window.
            self._gate.unresolved_protection_failure = True
            # §17.4 is modify-before-cancel, so a gate-refused MODIFY leaves the
            # previous SL resting on the exchange at a stale trigger — protected,
            # just not at the band we now want. A gate-refused CREATE leaves
            # nothing. Both reach here, and the acceptance validator has to tell
            # them apart: counting the first as unprotected seconds fails an
            # otherwise-healthy 30-cycle run on one kill-switch refresh blip.
            # The still-resting order's id IS the distinction, so record it —
            # ``order_id`` present ⇒ an SL is still on the book.
            resting = repo.active_protection_order(
                self._db.conn, self._run_id, self._coin, "stop_loss"
            )
            # COVERAGE, not mere existence. §17.1 rule 1 is a coverage rule, and
            # the reconciler's own predicate (_has_valid_sl) demands reduce-only
            # + closing side + qty >= position. A refused MODIFY is most often a
            # RESIZE — a later slice filled and the resting SL now under-covers
            # — so stamping the order_id on the strength of "a row exists" would
            # tell the §20.3 validator "protected" while part of the position
            # carries no stop at all. Compare on the same floored basis
            # _establish uses, or step rounding re-opens the window spuriously.
            with localcontext(DECIMAL_CONTEXT):
                needed = floor_to_step(abs(position.size), self._qty_step)
            closing_side = (Side.BUY if position.size < 0 else Side.SELL).value
            covers = (
                resting is not None
                and resting["side"] == closing_side
                and Decimal(resting["qty"]) >= needed
            )
            shortfall = (
                ""
                if resting is None
                else f" (resting {resting['side']} {resting['qty']} vs {needed} needed)"
            )
            self._record_event(
                "stop_loss_repair_blocked",
                # Only a COVERING order suppresses the unprotected window.
                order_id=resting["order_id"] if covers else None,
                cloid_hex=resting["cloid_hex"] if covers else None,
                detail=(
                    "wire gate refused every SL attempt pre-send (kill switch); retrying "
                    + (
                        "next sync — the previous SL still rests and covers the position"
                        if covers
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
        # A no-op: the resting order already covers this size at this trigger.
        if (
            existing is not None
            and existing_oid is not None
            and existing["trigger_price"] is not None
            and Decimal(existing["trigger_price"]) == trigger_price
            and existing["qty"] is not None
            and Decimal(existing["qty"]) == size
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
            except ExchangeError as exc:
                all_gate_blocked = False
                # Unknown outcome (§8.3 rule 11): a lost ack / timeout may have left
                # the order LIVE on the exchange. Ask orderStatus BEFORE counting a
                # failure — a landed-but-ack-lost SL/TP must not be blind-resent
                # (the resend hits a duplicate-cloid rejection) and must NOT drive a
                # spurious emergency close of an already-protected position.
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

            if ack.is_duplicate and self._recover_placed_order(
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
                # The exchange already knows this cloid from a prior attempt whose
                # ack we never observed; orderStatus confirms it is resting (§8.3
                # rules 2–4) → recovered, no resend.
                return _EstablishResult.ESTABLISHED

            self._log_attempt_failed(role, attempt, attempts, existing_oid, ack.error, now)
            self._maybe_delay(attempt, attempts)
        return _EstablishResult.GATE_BLOCKED if all_gate_blocked else _EstablishResult.EXHAUSTED

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

    def _maybe_delay(self, attempt: int, attempts: int) -> None:
        if attempt >= attempts:
            return
        self._sleep(float(self._config.sl_repair_retry_delay_seconds))
        # §18.2: this delay blocked the single-threaded tick — refresh the dead
        # man's switch across it (as the startup sweep does) so a repair episode
        # never stretches the kill-switch refresh cadence toward ``max_tick_gap``
        # and self-trips the switch when the system is least healthy. Guarded so a
        # refresh miss never aborts the repair out from under the position.
        if self._kill_switch is not None:
            try:
                self._kill_switch.tick()
            except Exception:  # noqa: BLE001 — a refresh miss must not abort the repair
                logger.warning("kill-switch refresh during SL/TP repair failed", exc_info=True)

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
