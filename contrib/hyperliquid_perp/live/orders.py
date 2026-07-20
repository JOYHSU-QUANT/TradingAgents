"""Live order submission: §4.1-gated, §8.3-idempotent, evidence-first.

:class:`LiveOrderSubmitter` is the intended path for every live order this
system places. It wraps the transport (:class:`HyperliquidSignedClient`) with
the persistence protocol the spec demands:

1. The §4.1 gate is checked FIRST — a rejection raises before any evidence is
   written, so a gate-blocked order leaves no phantom rows (§4.1: the caller
   records ``order_created = false``, nothing else).
2. BEFORE the network call, one transaction records intent: the cloid pair in
   ``cloid_registry`` (§8.2 — idempotent for a retry of the same pair), the
   ``orders`` row, and a ``live_order_attempts`` row in status ``submitted``.
   A crash after COMMIT but before the ack leaves durable evidence that an
   order MAY exist on the exchange.
3. The order goes out with its cloid (the client's bound gate re-checks as a
   backstop).
4. AFTER the response, the raw exchange payload is written to a file (its path
   lands in ``raw_exchange_payload_path``, or the column is left alone if the
   file could not be written), and the outcome is recorded on both rows. An
   ACCEPTED ack settles both in ONE transaction; the duplicate and rejected
   acks first settle the attempt row, then consult orderStatus (§8.3 rules
   2–4, 9) — so the orders row is settled in a second transaction, or, on a
   recovery, by the back-fill instead.

§8.3 idempotent retry: a resubmit of the same ``cloid_logical`` re-derives the
same ``cloid_hex``. If ANY prior send attempt exists for that cloid — whatever
its recorded outcome; even an ``acknowledged`` one, since a filled/expired
order's cloid would be ACCEPTED again by the exchange — or the exchange
answers "duplicate", the submitter queries orderStatus by cloid_hex FIRST
(rules 2–3); a found order back-fills the local record instead of resending
(rule 4); only a cloid the exchange confirms it does not know is sent again
(rule 5) — always under the same cloid, never a fresh one (rule 6).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.errors import (
    ExchangeError,
    MalformedResponseError,
    OrderIdempotencyContradiction,
)
from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient, OrderAck
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.cloid import assert_cloid_provenance, cloid_hex as derive_cloid_hex
from ..persistence.db import Database
from ..persistence.ids import live_order_attempt_id
from ..persistence.models import Side
from .order_gate import PROTECTIVE_ORDER_ROLES, RealOrderGate
from .payloads import payload_column, write_raw_payload

__all__ = [
    "LiveOrderSubmitter",
    "SubmitOutcome",
    "SubmitOutcomeKind",
    "local_status_for_exchange_status",
    "parse_order_status",
]

logger = logging.getLogger(__name__)


class SubmitOutcomeKind(str, Enum):
    """The three terminal verdicts of one §8.3 submit call."""

    ACKNOWLEDGED = "acknowledged"
    RECOVERED_EXISTING = "recovered_existing"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SubmitOutcome:
    """What one :meth:`LiveOrderSubmitter.submit_ioc_limit` call achieved.

    ``outcome``:

    - ``acknowledged`` — this call's send was accepted (``ack`` carries
      resting/filled detail);
    - ``recovered_existing`` — the cloid already existed on the exchange in a
      live/filled/canceled state (§8.3 rule 4): the local record was
      back-filled from orderStatus and NOTHING was resent; ``order_status``
      carries the queried payload;
    - ``rejected`` — the exchange refused the order, either on this call's
      ack or as the recovered orderStatus of an earlier send. Rule 5 does not
      permit an automatic resend (the cloid is known to the exchange); the
      caller decides whether a NEW logical order is warranted.

    ``attempt_id`` is None when the outcome was resolved by the pre-send
    orderStatus check — no new round-trip happened, so there is no attempt row
    to point at.

    ``error`` is the one place a rejection reason lives, WHICHEVER path
    produced it: the ack path copies ``ack.error``, the recovery path
    describes the recovered orderStatus. Consumers must not reach into
    ``ack``/``order_status`` for it — those carry the raw evidence and are
    each None on the other path. ``__post_init__`` enforces this contract, so
    an outcome whose evidence fields disagree with its verdict cannot exist.

    ``exchange_raw_status`` is the exchange's VERBATIM status word for whatever
    evidence produced this verdict — ``"resting"`` / ``"filled"`` / ``"error"``
    from an ack, or the exact orderStatus word (``"minTradeNtlRejected"``,
    ``"perpMarginRejected"``, …) from a recovery. It is the same value the
    ``orders.exchange_raw_status`` column carries (§16.1), lifted onto the
    outcome because it is the ONLY non-guessing basis a caller has for telling
    the rejections apart: ``REJECTED`` licenses minting a new logical order
    (§8.3 rule 9), but a permanent rejection (bad tick, below min notional)
    will fail identically forever while a transient one (margin, oracle,
    liquidity) may not. Classify off an EXACT word table, never off ``error``'s
    free text — the wire message is not a versioned contract, and substring
    tests on it are the exact bug class this module refuses everywhere else.
    """

    outcome: SubmitOutcomeKind
    order_id: str
    cloid_logical: str
    cloid_hex: str
    attempt_id: str | None = None
    exchange_order_id: str | None = None
    exchange_raw_status: str | None = None
    error: str | None = None
    ack: OrderAck | None = None
    order_status: Any = None

    def __post_init__(self) -> None:
        # Coerce a raw string the way Side.parse does — loud on a typo.
        object.__setattr__(self, "outcome", SubmitOutcomeKind(self.outcome))
        if self.cloid_hex != derive_cloid_hex(self.cloid_logical):
            raise ValueError(
                f"cloid_hex {self.cloid_hex!r} is not the derivation of "
                f"cloid_logical {self.cloid_logical!r}"
            )
        # Every verdict is reached by observing an exchange answer, so every
        # verdict carries that answer's word. No path may omit it.
        if self.exchange_raw_status is None:
            raise ValueError(
                f"SubmitOutcome({self.outcome.value!r}) must carry the exchange's "
                "verbatim status word (exchange_raw_status)"
            )
        if self.outcome is SubmitOutcomeKind.ACKNOWLEDGED:
            ok = (
                self.ack is not None
                and self.order_status is None
                and self.error is None
                and self.attempt_id is not None
                and self.exchange_order_id is not None
            )
        elif self.outcome is SubmitOutcomeKind.RECOVERED_EXISTING:
            ok = (
                self.order_status is not None
                and self.ack is None
                and self.error is None
                and self.exchange_order_id is not None
            )
        else:  # REJECTED — evidence from exactly one path, reason mandatory.
            ok = self.error is not None and (self.ack is None) != (self.order_status is None)
        if not ok:
            raise ValueError(
                f"SubmitOutcome fields do not satisfy the {self.outcome.value!r} contract"
            )


class LiveOrderSubmitter:
    """Owns the submit path for one run: gate + transport + persistence.

    ``payload_dir`` is where raw exchange responses land (one JSON file per
    round-trip, path recorded on the rows) — mirroring the paper scheduler's
    ``payloads/<run_id>/`` convention for AI payloads.
    """

    def __init__(
        self,
        *,
        client: HyperliquidSignedClient,
        gate: RealOrderGate,
        db: Database,
        run_id: str,
        payload_dir: Path,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._gate = gate
        self._db = db
        self._run_id = run_id
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()

    # ---- raw payload evidence -------------------------------------------

    def _write_raw_payload(self, kind: str, key: str, payload: Any) -> str | None:
        """This run's payload_dir + clock, bound to the shared writer."""
        return write_raw_payload(
            payload_dir=self._payload_dir,
            kind=kind,
            key=key,
            payload=payload,
            now=self._clock.now(),
        )

    def _record_attempt_failure(self, attempt_id: str, exc: BaseException) -> None:
        """Patch the attempt to 'failed' without letting the patch replace ``exc``.

        ``exc`` — why a real order failed on the wire — is the diagnosis. A DB
        error raised while recording it (sqlite BUSY at the worst moment) would
        propagate IN ITS PLACE, so the caller and the audit trail would read
        "database is locked" where the true story was a timeout on a
        possibly-live order. Log the original FIRST (the ordering rule the kill
        switch already follows everywhere), then record; if the record itself
        dies, say so and still let the original be the exception that escapes.

        Losing the patch is safe: the row stays 'submitted', which means exactly
        "outcome unknown" (see ``update_live_order_attempt``) — the same
        conclusion 'failed' leads to. The §8.3 pre-check is status-blind, so the
        retry resolves the order's fate through orderStatus either way.
        """
        logger.warning("live order attempt %s failed on the wire: %s", attempt_id, exc)
        try:
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn, attempt_id, status="failed", error_message=str(exc)
                )
        except Exception:
            logger.exception(
                "could not record live order attempt %s as 'failed'; it stays "
                "'submitted' (outcome unknown) and the §8.3 pre-check resolves it",
                attempt_id,
            )

    # ---- shared row writing ----------------------------------------------

    def _ensure_local_order(
        self,
        conn: sqlite3.Connection,
        *,
        order_id: str,
        coin: str,
        side: str,
        size: Decimal,
        limit_price: Decimal,
        cloid_logical: str,
        cloid_hex: str,
        order_role: str,
        reduce_only: bool,
        output_id: str | None,
        flip_plan_id: str | None,
        flip_leg: str | None,
        parent_order_id: str | None,
        status: str,
        now: datetime,
        submitted_at: datetime | None = None,
        status_reason: str | None = None,
        exchange_order_id: str | None = None,
        exchange_status: str | None = None,
        exchange_raw_status: str | None = None,
        raw_exchange_payload_path: str | None = None,
    ) -> bool:
        """Registry mapping + orders row (insert-if-absent), one shape for both
        the intent transaction and the §8.3 recovery back-fill — so a §16.1
        column (or a provenance link like ``output_id``) can never exist on
        submitted orders but silently miss on recovered ones. Returns True when
        the orders row was inserted by this call.
        """
        repo.insert_cloid_mapping(
            conn,
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            run_id=self._run_id,
            symbol=coin,
            order_role=order_role,
            created_at=now,
        )
        existing = repo.get_order(conn, order_id)
        if existing is not None:
            if existing["cloid_hex"] != cloid_hex:
                # §8.3 leans on order_id↔cloid coherence everywhere; a caller
                # that reuses an order_id under a different cloid pair would
                # send the wire order under the NEW cloid while the local row
                # still carries the old one. The registry catches the reverse
                # slip (same logical, different hex); this catches this one.
                raise ValueError(
                    f"order {order_id!r} already exists with cloid_hex "
                    f"{existing['cloid_hex']!r}, not {cloid_hex!r} — an order_id "
                    "keeps one cloid pair for life (§8.3)"
                )
            return False
        repo.insert_order(
            conn,
            order_id=order_id,
            mode="live",
            run_id=self._run_id,
            symbol=coin,
            order_role=order_role,
            side=side,
            order_type="ioc_limit",
            qty=size,
            status=status,
            status_reason=status_reason,
            price=limit_price,
            remaining_qty=size,
            reduce_only=reduce_only,
            output_id=output_id,
            flip_plan_id=flip_plan_id,
            flip_leg=flip_leg,
            parent_order_id=parent_order_id,
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            submitted_at=submitted_at,
            exchange_order_id=exchange_order_id,
            exchange_status=exchange_status,
            exchange_raw_status=exchange_raw_status,
            raw_exchange_payload_path=raw_exchange_payload_path,
            is_bot_owned=True,
            timestamp=now,
        )
        return True

    # ---- §8.3 submit protocol -------------------------------------------

    def submit_ioc_limit(
        self,
        *,
        order_id: str,
        coin: str,
        side: str,
        size: Decimal,
        limit_price: Decimal,
        cloid_logical: str,
        order_role: str,
        reduce_only: bool = False,
        output_id: str | None = None,
        flip_plan_id: str | None = None,
        flip_leg: str | None = None,
        parent_order_id: str | None = None,
    ) -> SubmitOutcome:
        """Place one IOC limit order under the §8.3 idempotent-retry contract.

        ``order_id`` and ``cloid_logical`` are minted by the caller (the PR 5
        engine derives both deterministically from run/plan/slice), so a retry
        of the same logical order arrives here with the same identifiers —
        which is exactly what makes the protocol idempotent.
        """
        # §4.1 first, before any evidence is written: a gate-blocked order
        # must leave no phantom 'submitted' rows behind (the caller records
        # order_created=false / no_order_reason instead). The client's bound
        # gate re-checks at the wire as a backstop. A protection / de-risking
        # role (the §17.2 emergency close) rides the protective gate so it stays
        # sendable in safe mode; every risk-adding role uses the ordinary gate.
        protective = order_role in PROTECTIVE_ORDER_ROLES
        if protective:
            self._gate.require_protective_order(coin)
        else:
            self._gate.require_order(coin)

        # The cloid and the provenance fields beside it describe the same order
        # twice; both reach the audit trail, and only this check makes them
        # agree. Before the intent transaction, so a contradictory pair can
        # never consume a cloid or leave a row behind.
        assert_cloid_provenance(
            cloid_logical, run_id=self._run_id, symbol=coin, order_role=order_role
        )

        hex_id = derive_cloid_hex(cloid_logical)
        is_buy = Side.parse(side) is Side.BUY

        # One binding for the three §8.3 recovery call sites (pre-check,
        # duplicate ack, rejected ack) — the parameters never vary, only
        # which attempt row the outcome should point at.
        def recover_existing(attempt_id: str | None) -> SubmitOutcome | None:
            return self._try_recover_existing(
                order_id=order_id,
                coin=coin,
                side=side,
                size=size,
                limit_price=limit_price,
                cloid_logical=cloid_logical,
                cloid_hex=hex_id,
                order_role=order_role,
                reduce_only=reduce_only,
                output_id=output_id,
                flip_plan_id=flip_plan_id,
                flip_leg=flip_leg,
                parent_order_id=parent_order_id,
                attempt_id=attempt_id,
            )

        # §8.3 rules 3–5 pre-check: if this cloid was EVER sent before (any
        # prior place attempt, whatever its recorded outcome), ask the
        # exchange first. Even an 'acknowledged' prior send must short-circuit
        # here: the exchange's duplicate rejection only guards OPEN orders, so
        # a filled/expired cloid would be accepted again as a brand-new order.
        if repo.has_place_attempt(self._db.conn, cloid_hex=hex_id):
            # A prior attempt stuck at 'submitted' stays that way — its own
            # ack was never observed and that is its defined terminal state
            # (see update_live_order_attempt); the recovered fate lands on
            # the orders row, which is what PR 4 reconciles against.
            recovered = recover_existing(attempt_id=None)
            if recovered is not None:
                return recovered

        attempt_index = repo.next_live_attempt_index(
            self._db.conn, action="place", cloid_hex=hex_id
        )
        attempt_id = live_order_attempt_id(self._run_id, "place", hex_id, attempt_index)
        now = self._clock.now()

        # Intent transaction: registry + orders row + attempt, all-or-nothing,
        # BEFORE any network traffic.
        with self._db.transaction() as conn:
            inserted = self._ensure_local_order(
                conn,
                order_id=order_id,
                coin=coin,
                side=side,
                size=size,
                limit_price=limit_price,
                cloid_logical=cloid_logical,
                cloid_hex=hex_id,
                order_role=order_role,
                reduce_only=reduce_only,
                output_id=output_id,
                flip_plan_id=flip_plan_id,
                flip_leg=flip_leg,
                parent_order_id=parent_order_id,
                status="submitted",
                now=now,
                submitted_at=now,
            )
            if not inserted:
                # A rule-5 resend: the row survives from the earlier attempt and
                # still carries ITS verdict — 'rejected', with that rejection's
                # reason. Re-stamp it to the intent state, or the send below runs
                # with a TERMINAL local row over a possibly-live order: a crash
                # inside the network window would leave 'rejected' (not in
                # LIVE_ORDER_STATUSES) on an order resting at the exchange, PR 4's
                # reconciliation would skip it as settled, and an engine rebuilding
                # intent from the DB would read "that one was rejected" — which
                # §8.3 rule 9 licenses it to answer by minting a NEW logical order.
                # The pre-check cannot save us there: it only re-derives the SAME
                # cloid, and a fresh cloid never reaches it.
                repo.update_order(
                    conn,
                    order_id,
                    status="submitted",
                    status_reason=None,
                    submitted_at=now,
                    # The exchange-side columns describe the PREVIOUS attempt's
                    # verdict, and this send has no verdict yet. Cleared with the
                    # status they belong to: a crash inside the network window
                    # would otherwise leave a row reading status='submitted' with
                    # exchange_status='rejected' and an acknowledged_at from an
                    # answer to a different send — self-contradictory evidence in
                    # the one place PR 4's reconciliation looks.
                    exchange_status=None,
                    exchange_raw_status=None,
                    acknowledged_at=None,
                    updated_at=now,
                )
            repo.insert_live_order_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=self._run_id,
                action="place",
                symbol=coin,
                attempt_index=attempt_index,
                order_id=order_id,
                cloid_logical=cloid_logical,
                cloid_hex=hex_id,
                side=side,
                qty=size,
                price=limit_price,
                reduce_only=reduce_only,
                order_role=order_role,
                requested_at=now,
            )

        # The network call. ANY failure past this point leaves the attempt row
        # patched 'failed' — a durable "outcome unknown" marker (a timeout may
        # still have delivered the order) that the next retry's pre-check
        # resolves through orderStatus before resending.
        #
        # PR 5 REQUIREMENT: size/price must already be rounded to the coin's
        # lot/tick (szDecimals + px significant figures) before they get here.
        # This layer cannot tell a purely LOCAL validation failure (the SDK's
        # float_to_wire refusing a value that never left the process) from a
        # genuine transport failure — call_sdk wraps both into
        # ExchangeRequestError — so a malformed order is recorded as "outcome
        # unknown" and burns an orderStatus round-trip resolving an order that
        # was never sent. Validating here would need the exchange's perp meta,
        # which is the plan builder's (§9.1) job, not the transport's.
        try:
            ack = self._client.place_ioc_limit(
                coin=coin,
                is_buy=is_buy,
                size=size,
                limit_price=limit_price,
                cloid_hex=hex_id,
                reduce_only=reduce_only,
                protective=protective,
            )
        except Exception as exc:
            # Records 'failed' WITHOUT letting a busy DB replace `exc` — the
            # exchange failure is the diagnosis, not the sqlite error.
            self._record_attempt_failure(attempt_id, exc)
            raise

        raw_path = self._write_raw_payload("order", hex_id, ack.raw)
        ack_at = self._clock.now()

        if ack.is_duplicate:
            # §8.3 rules 2–4: never blind-resend; resolve through orderStatus.
            # Stamped like the rejected patch below: the exchange DID answer
            # this round-trip, so acknowledged_at NULL stays an exact synonym
            # for "no answer observed" (submitted/failed rows only).
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn,
                    attempt_id,
                    status="duplicate",
                    error_message=ack.error,
                    exchange_status=ack.status,
                    acknowledged_at=ack_at,
                    raw_exchange_payload_path=payload_column(raw_path),
                )
            recovered = recover_existing(attempt_id)
            if recovered is not None:
                return recovered
            # The exchange said "duplicate" but its own orderStatus cannot see
            # the cloid — contradictory state; resending (rule 5 requires the
            # previous send be CONFIRMED unsuccessful) would risk a double
            # order. Fail loud for the operator, in a lane a transport-retry
            # `except ExchangeRequestError` cannot swallow.
            raise OrderIdempotencyContradiction(
                f"exchange reported duplicate for cloid {hex_id} but orderStatus "
                f"does not know it — refusing to resend (§8.3 rule 5)"
            )

        if not ack.accepted:
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn,
                    attempt_id,
                    status="rejected",
                    error_message=ack.error,
                    exchange_status=ack.status,
                    acknowledged_at=ack_at,
                    raw_exchange_payload_path=payload_column(raw_path),
                )
            # The duplicate markers are a fast-path, not the authority: the
            # exchange's rejection text is not a versioned contract, and a
            # duplicate rejection the markers fail to match must not become
            # REJECTED — that verdict licenses the caller to mint a NEW
            # logical order, a double order if the original is live. Before
            # any REJECTED verdict, orderStatus (not the wording) gets the
            # last word on rejected-vs-exists.
            recovered = recover_existing(attempt_id)
            if recovered is not None:
                return recovered
            with self._db.transaction() as conn:
                repo.update_order(
                    conn,
                    order_id,
                    status="rejected",
                    status_reason=ack.error,
                    exchange_status="rejected",
                    exchange_raw_status=ack.status,
                    acknowledged_at=ack_at,
                    updated_at=ack_at,
                    raw_exchange_payload_path=payload_column(raw_path),
                )
            return SubmitOutcome(
                outcome=SubmitOutcomeKind.REJECTED,
                order_id=order_id,
                cloid_logical=cloid_logical,
                cloid_hex=hex_id,
                attempt_id=attempt_id,
                exchange_raw_status=ack.status,
                error=ack.error,
                ack=ack,
            )

        # Accepted: ack-time back-fill (§16.1 exchange_order_id /
        # exchange_status / acknowledged_at). Local status maps the wire
        # verdict — a partial IOC fill (totalSz < requested) must not read as
        # fully filled; filled_qty/remaining stay with PR 3's fill ingestion —
        # the exchange fill events are the accounting basis (§15), not this ack.
        # Enumerate the accepted vocabulary rather than letting an `else` stand
        # in for it: OrderAck.accepted is `status in ("resting", "filled")`, and
        # if a future accepted status ever appears there without landing here,
        # an `else: "filled"` would silently record an UNSETTLED order as
        # settled. Same class of bug as the rule-10 guard in
        # _try_recover_existing below — a partial status test driving a safety
        # verdict. Fail loud instead; the ack contract (signed_client.OrderAck)
        # makes this branch unreachable today.
        if ack.status == "resting":
            exchange_status = "open"
        elif ack.status == "filled":
            exchange_status = "filled"
        else:  # pragma: no cover — OrderAck.accepted admits no other status
            raise ExchangeError(
                f"order for cloid {hex_id} was accepted with unhandled status "
                f"{ack.status!r} — refusing to guess whether it is settled"
            )
        local_status = exchange_status
        if ack.status == "filled" and ack.filled_size is not None and ack.filled_size < size:
            local_status = "partially_filled"
        with self._db.transaction() as conn:
            repo.update_live_order_attempt(
                conn,
                attempt_id,
                status="acknowledged",
                exchange_order_id=ack.exchange_order_id,
                exchange_status=ack.status,
                acknowledged_at=ack_at,
                raw_exchange_payload_path=payload_column(raw_path),
            )
            repo.update_order(
                conn,
                order_id,
                status=local_status,
                # An accepted order has no failure reason. Cleared explicitly,
                # not left untouched: a row can legitimately have been patched
                # 'rejected' (with a reason) and then, under rule 5, carry a
                # later successful send — leaving the old text behind would put
                # a rejection reason on a filled order.
                status_reason=None,
                exchange_order_id=ack.exchange_order_id,
                exchange_status=exchange_status,
                exchange_raw_status=ack.status,
                acknowledged_at=ack_at,
                updated_at=ack_at,
                raw_exchange_payload_path=payload_column(raw_path),
            )
        return SubmitOutcome(
            outcome=SubmitOutcomeKind.ACKNOWLEDGED,
            order_id=order_id,
            cloid_logical=cloid_logical,
            cloid_hex=hex_id,
            attempt_id=attempt_id,
            exchange_order_id=ack.exchange_order_id,
            exchange_raw_status=ack.status,
            ack=ack,
        )

    def _try_recover_existing(
        self,
        *,
        order_id: str,
        coin: str,
        side: str,
        size: Decimal,
        limit_price: Decimal,
        cloid_logical: str,
        cloid_hex: str,
        order_role: str,
        reduce_only: bool,
        output_id: str | None,
        flip_plan_id: str | None,
        flip_leg: str | None,
        parent_order_id: str | None,
        attempt_id: str | None,
    ) -> SubmitOutcome | None:
        """§8.3 rules 3–4: query orderStatus by cloid; back-fill if found.

        Returns None when the exchange confirms it does not know the cloid
        (the caller may then send — rule 5's "confirmed unsuccessful"). A
        found order back-fills SQLite and is NOT resent; a found *rejected*
        order is reported as outcome ``rejected`` (the send is confirmed
        unsuccessful, but the cloid is known to the exchange, so rule 5's
        resend condition — cloid_hex 不存在 — still does not hold).

        The back-fill records the CALLER's size/price/side; the exchange's
        view survives only in the raw payload file. That is sound because of
        the §8.3 retry contract: a retry of the same ``cloid_logical`` must
        carry byte-identical parameters (the PR 5 engine derives the cloid
        and the parameters deterministically from the same plan slice), so
        the caller's parameters ARE the exchange order's parameters. An
        engine that recomputed size on retry would break this — the contract
        lives here and in spec §8.3, not in a runtime comparison.

        A recovery INSERT leaves ``submitted_at`` / ``acknowledged_at`` NULL on
        purpose: the true send time is unknown (only ``timestamp`` /
        ``updated_at`` carry the recovery instant). Consumers (PR 4) must read
        NULL as "unknown", never as "not sent".
        """
        status_payload = self._client.query_order_by_cloid(cloid_hex)
        parsed = parse_order_status(status_payload)
        if parsed is None:
            # unknownOid clears rule 5's resend condition — UNLESS the durable
            # record proves the exchange once TOOK this cloid. Then "absent"
            # contradicts local evidence (retention expiry, an Info
            # inconsistency) and a resend would be accepted as a brand-new
            # order. Same fail-loud posture as the duplicate/unknownOid
            # contradiction in submit_ioc_limit.
            #
            # "Took it" means BOTH kinds of durable proof: an acknowledged or
            # duplicate place attempt, AND an orders row already carrying an
            # exchange-supplied oid — the latter being the ONLY trace a
            # successful earlier recovery leaves, since recovery deliberately
            # does not back-patch the attempt row. Reading only the attempt row
            # let "timeout -> recover the resting order -> a later unknownOid"
            # fall through to a resend of a live order. has_exchange_known_cloid
            # owns that definition; do not hand-roll either half back in here.
            if repo.has_exchange_known_cloid(self._db.conn, cloid_hex=cloid_hex):
                raise OrderIdempotencyContradiction(
                    f"orderStatus does not know cloid {cloid_hex}, but durable local "
                    "evidence says it reached the exchange (a prior place attempt was "
                    "acknowledged/duplicate, or the order already carries an "
                    "exchange order id) — refusing to resend (§8.3 rule 5)"
                )
            return None
        exchange_order_id, exchange_status = parsed
        raw_path = self._write_raw_payload("orderStatus", cloid_hex, status_payload)
        now = self._clock.now()
        local_status = local_status_for_exchange_status(exchange_status)
        rejected = local_status == "rejected"
        # One contract on every write path (§16.1): a rejection recorded HERE
        # must carry the same reason a rejection recorded on the ack path does.
        # orderStatus is the COMMON route for a real live rejection (Hyperliquid
        # exposes tickRejected / perpMarginRejected / … there), so leaving this
        # NULL would mean orders.status_reason is normally empty for exactly the
        # rejections an operator most needs to explain. The same string becomes
        # the outcome's `error` below — one reason, one predicate, not two.
        status_reason = f"orderStatus reported {exchange_status!r}" if rejected else None
        with self._db.transaction() as conn:
            # Rule 4: the order exists on the exchange — make SQLite agree.
            inserted = self._ensure_local_order(
                conn,
                order_id=order_id,
                coin=coin,
                side=side,
                size=size,
                limit_price=limit_price,
                cloid_logical=cloid_logical,
                cloid_hex=cloid_hex,
                order_role=order_role,
                reduce_only=reduce_only,
                output_id=output_id,
                flip_plan_id=flip_plan_id,
                flip_leg=flip_leg,
                parent_order_id=parent_order_id,
                status=local_status,
                now=now,
                status_reason=status_reason,
                exchange_order_id=exchange_order_id,
                # §16.1 vocabulary, one contract on every write path:
                # exchange_status = the normalized family (orders.status
                # words), exchange_raw_status = the verbatim wire word.
                exchange_status=local_status,
                exchange_raw_status=exchange_status,
                raw_exchange_payload_path=raw_path,
            )
            if not inserted:
                repo.update_order(
                    conn,
                    order_id,
                    status=local_status,
                    status_reason=status_reason,
                    exchange_order_id=exchange_order_id,
                    exchange_status=local_status,
                    exchange_raw_status=exchange_status,
                    updated_at=now,
                    raw_exchange_payload_path=payload_column(raw_path),
                )
        return SubmitOutcome(
            outcome=(
                SubmitOutcomeKind.REJECTED if rejected else SubmitOutcomeKind.RECOVERED_EXISTING
            ),
            order_id=order_id,
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            attempt_id=attempt_id,
            exchange_order_id=exchange_order_id,
            # The exact word, not a guess from it: the caller classifies a
            # rejection off an exact table, never off `error`'s free text.
            exchange_raw_status=exchange_status,
            # The same string the orders row carries — one reason, not two.
            error=status_reason,
            order_status=status_payload,
        )


# The exchange's order-status vocabulary mapped to the local orders.status
# column. The documented set is enumerated EXACTLY, and it is the ONLY thing
# that classifies a status: a heuristic must never stand in for a known, closed
# vocabulary (iocCancelRejected contains both "cancel" and "reject"; only the
# exact entry can classify it reliably). Anything not in this table is a word
# the exchange gained after it was written — and every guess about such a word
# is unsafe in one direction or another, so we make none. See
# local_status_for_exchange_status.
_EXCHANGE_TO_LOCAL_STATUS = {
    # Live on the book (a fired trigger order is a live order).
    "open": "open",
    "resting": "open",
    "triggered": "open",
    "filled": "filled",
    # Canceled family — was on the book, then removed.
    "canceled": "canceled",
    "marginCanceled": "canceled",
    "vaultWithdrawalCanceled": "canceled",
    "openInterestCapCanceled": "canceled",
    "selfTradeCanceled": "canceled",
    "reduceOnlyCanceled": "canceled",
    "siblingFilledCanceled": "canceled",
    "delistedCanceled": "canceled",
    "liquidatedCanceled": "canceled",
    "scheduledCancel": "canceled",
    # Rejected family — never accepted onto the book.
    "rejected": "rejected",
    "tickRejected": "rejected",
    "minTradeNtlRejected": "rejected",
    "perpMarginRejected": "rejected",
    "reduceOnlyRejected": "rejected",
    "badAloPxRejected": "rejected",
    "iocCancelRejected": "rejected",
    "badTriggerPxRejected": "rejected",
    "marketOrderNoLiquidityRejected": "rejected",
    "positionIncreaseAtOpenInterestCapRejected": "rejected",
    "positionFlipAtOpenInterestCapRejected": "rejected",
    "tooAggressiveAtOpenInterestCapRejected": "rejected",
    "openInterestIncreaseRejected": "rejected",
    "insufficientSpotBalanceRejected": "rejected",
    "oracleRejected": "rejected",
    "perpMaxPositionRejected": "rejected",
}


def local_status_for_exchange_status(exchange_status: str) -> str:
    """Map an exchange status word to the local orders.status vocabulary.

    The exact table is the ONLY authority. A word it does not carry is recorded
    as "open" with a warning and left for PR 4's reconciliation to settle
    against the exchange — we never guess, in any direction, because every
    guess about an unknown word is unsafe in some direction:

    - Guessing **"rejected"** is the expensive one. It becomes
      SubmitOutcomeKind.REJECTED, which under §8.3 rule 9 LICENSES the caller to
      mint a brand-new logical order — so a future word merely CONTAINING
      "reject", attached to an order that is actually resting, mints a second
      cloid for a live position.
    - Guessing a **terminal** status ("canceled"/"filled") silently stops us
      managing an order that may still be live: a word like ``cancelRequested``
      (cancel in flight, order still resting) would be booked as canceled, and a
      later fill would land against a locally-canceled order.

    Both are the same bug class as the §8.3 rule-10 resend guard — a partial
    status test driving a safety verdict — and the table already enumerates
    Hyperliquid's complete documented vocabulary, so a heuristic buys nothing
    on the words that matter and only ever fires where it is least trustworthy.

    Recording an unknown word as "open" is the one conservative reading:
    overstating LIVENESS is recoverable (the order keeps being watched, and
    reconciliation closes it), while overstating rejection mints cloids and
    overstating settlement abandons live orders.
    """
    mapped = _EXCHANGE_TO_LOCAL_STATUS.get(exchange_status)
    if mapped is not None:
        return mapped
    logger.warning(
        "unknown exchange order status %r — recording the order as 'open' until "
        "reconciliation resolves it (guessing from the word would risk either a "
        "double order or an abandoned live one)",
        exchange_status,
    )
    return "open"


def parse_order_status(payload: Any) -> tuple[str, str] | None:
    """(exchange_order_id, status) from an orderStatus payload; None = unknown.

    The Info endpoint answers ``{"status": "unknownOid"}`` for a cloid it has
    never seen, and ``{"status": "order", "order": {"order": {...}, "status":
    ...}}`` for a known one. Only the documented unknown marker returns None:
    rule 5 permits a resend solely when the previous send is CONFIRMED absent,
    so a malformed payload must fail loud, never read as "absent".
    """
    if isinstance(payload, dict) and payload.get("status") == "unknownOid":
        return None
    if (
        isinstance(payload, dict)
        and payload.get("status") == "order"
        and isinstance(payload.get("order"), dict)
    ):
        wrapper = payload["order"]
        inner = wrapper.get("order")
        status = wrapper.get("status")
        if isinstance(inner, dict) and "oid" in inner and isinstance(status, str):
            return (str(inner["oid"]), status)
    raise MalformedResponseError(f"orderStatus payload not recognised: {payload!r}")
