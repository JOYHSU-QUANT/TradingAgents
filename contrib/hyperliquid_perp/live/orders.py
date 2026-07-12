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
4. AFTER the response, one transaction records the outcome on both rows, and
   the raw exchange payload is written to a file whose path lands in
   ``raw_exchange_payload_path``.

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

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.errors import ExchangeError, MalformedResponseError
from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient, OrderAck
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.cloid import cloid_hex as derive_cloid_hex
from ..persistence.db import Database
from ..persistence.ids import live_order_attempt_id
from ..persistence.models import Side
from .order_gate import RealOrderGate

__all__ = ["LiveOrderSubmitter", "SubmitOutcome"]

logger = logging.getLogger(__name__)


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
    """

    outcome: str
    order_id: str
    cloid_logical: str
    cloid_hex: str
    attempt_id: str | None = None
    exchange_order_id: str | None = None
    ack: OrderAck | None = None
    order_status: Any = None


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
        """Persist one raw exchange response; the path goes on the DB rows.

        Failure to write evidence must not turn a successful exchange action
        into an exception (the order is already live) — warn and record NULL,
        same posture as the CSV-export breadcrumbs.
        """
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%S_%fZ")
        path = self._payload_dir / f"{kind}-{key}-{stamp}.json"
        try:
            self._payload_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, default=str, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to write raw exchange payload %s: %s", path, exc)
            return None
        return str(path)

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
        if repo.get_order(conn, order_id) is not None:
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
        # gate re-checks at the wire as a backstop.
        self._gate.require_order(coin)

        hex_id = derive_cloid_hex(cloid_logical)
        is_buy = Side.parse(side) is Side.BUY

        # §8.3 rules 3–5 pre-check: if this cloid was EVER sent before (any
        # prior place attempt, whatever its recorded outcome), ask the
        # exchange first. Even an 'acknowledged' prior send must short-circuit
        # here: the exchange's duplicate rejection only guards OPEN orders, so
        # a filled/expired cloid would be accepted again as a brand-new order.
        prior = repo.iter_live_order_attempts(self._db.conn, self._run_id, cloid_hex=hex_id)
        if any(row["action"] == "place" for row in prior):
            recovered = self._try_recover_existing(
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
                attempt_id=None,
            )
            if recovered is not None:
                return recovered

        attempt_index = repo.next_live_attempt_index(
            self._db.conn, self._run_id, action="place", cloid_hex=hex_id
        )
        attempt_id = live_order_attempt_id(self._run_id, "place", hex_id, attempt_index)
        now = self._clock.now()

        # Intent transaction: registry + orders row + attempt, all-or-nothing,
        # BEFORE any network traffic.
        with self._db.transaction() as conn:
            self._ensure_local_order(
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
        try:
            ack = self._client.place_ioc_limit(
                coin=coin,
                is_buy=is_buy,
                size=size,
                limit_price=limit_price,
                cloid_hex=hex_id,
                reduce_only=reduce_only,
            )
        except Exception as exc:
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn, attempt_id, status="failed", error_message=str(exc)
                )
            raise

        raw_path = self._write_raw_payload("order", hex_id, ack.raw)
        ack_at = self._clock.now()

        if ack.is_duplicate:
            # §8.3 rules 2–4: never blind-resend; resolve through orderStatus.
            with self._db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn,
                    attempt_id,
                    status="duplicate",
                    error_message=ack.error,
                    raw_exchange_payload_path=raw_path,
                )
            recovered = self._try_recover_existing(
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
            if recovered is not None:
                return recovered
            # The exchange said "duplicate" but its own orderStatus cannot see
            # the cloid — contradictory state; resending (rule 5 requires the
            # previous send be CONFIRMED unsuccessful) would risk a double
            # order. Fail loud for the operator.
            raise ExchangeError(
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
                    exchange_status="rejected",
                    raw_exchange_payload_path=raw_path,
                    acknowledged_at=ack_at,
                )
                repo.update_order(
                    conn,
                    order_id,
                    status="rejected",
                    status_reason=ack.error,
                    exchange_status="rejected",
                    raw_exchange_payload_path=raw_path,
                    acknowledged_at=ack_at,
                    updated_at=ack_at,
                )
            return SubmitOutcome(
                outcome="rejected",
                order_id=order_id,
                cloid_logical=cloid_logical,
                cloid_hex=hex_id,
                attempt_id=attempt_id,
                ack=ack,
            )

        # Accepted: ack-time back-fill (§16.1 exchange_order_id /
        # exchange_status / acknowledged_at). Local status maps the wire
        # verdict; filled_qty/remaining stay with PR 3's fill ingestion — the
        # exchange fill events are the accounting basis (§15), not this ack.
        local_status = "open" if ack.status == "resting" else "filled"
        with self._db.transaction() as conn:
            repo.update_live_order_attempt(
                conn,
                attempt_id,
                status="acknowledged",
                exchange_order_id=ack.exchange_order_id,
                exchange_status=ack.status,
                raw_exchange_payload_path=raw_path,
                acknowledged_at=ack_at,
            )
            repo.update_order(
                conn,
                order_id,
                status=local_status,
                exchange_order_id=ack.exchange_order_id,
                exchange_status=ack.status,
                raw_exchange_payload_path=raw_path,
                acknowledged_at=ack_at,
                updated_at=ack_at,
            )
        return SubmitOutcome(
            outcome="acknowledged",
            order_id=order_id,
            cloid_logical=cloid_logical,
            cloid_hex=hex_id,
            attempt_id=attempt_id,
            exchange_order_id=ack.exchange_order_id,
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
        """
        status_payload = self._client.query_order_by_cloid(cloid_hex)
        parsed = _parse_order_status(status_payload)
        if parsed is None:
            return None
        exchange_order_id, exchange_status = parsed
        raw_path = self._write_raw_payload("orderStatus", cloid_hex, status_payload)
        now = self._clock.now()
        local_status = _local_status_for_exchange_status(exchange_status)
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
                exchange_order_id=exchange_order_id,
                exchange_status=exchange_status,
                exchange_raw_status=exchange_status,
                raw_exchange_payload_path=raw_path,
            )
            if not inserted:
                repo.update_order(
                    conn,
                    order_id,
                    status=local_status,
                    exchange_order_id=exchange_order_id,
                    exchange_status=exchange_status,
                    exchange_raw_status=exchange_status,
                    raw_exchange_payload_path=raw_path,
                    updated_at=now,
                )
        return SubmitOutcome(
            outcome="rejected" if local_status == "rejected" else "recovered_existing",
            order_id=order_id,
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            attempt_id=attempt_id,
            exchange_order_id=exchange_order_id,
            order_status=status_payload,
        )


# The exchange's order-status vocabulary mapped to the local orders.status
# column. Exact names first; the rest classify by family — Hyperliquid has
# many terminal variants (scheduledCancel, liquidatedCanceled, tickRejected,
# minTradeNtlRejected, ...) and a new one must never be mistaken for a live
# order. Truly unknown statuses map to 'open' WITH a warning: overstating
# liveness is the conservative direction (an 'open' order keeps being watched
# and reconciled — PR 4 owns the exhaustive treatment), whereas guessing
# terminal would silently stop managing a possibly-live order.
_EXCHANGE_TO_LOCAL_STATUS = {
    "open": "open",
    "resting": "open",
    "filled": "filled",
    "canceled": "canceled",
    "rejected": "rejected",
}


def _local_status_for_exchange_status(exchange_status: str) -> str:
    mapped = _EXCHANGE_TO_LOCAL_STATUS.get(exchange_status)
    if mapped is not None:
        return mapped
    lowered = exchange_status.lower()
    if "cancel" in lowered:
        return "canceled"
    if "reject" in lowered:
        return "rejected"
    if "filled" in lowered:
        return "filled"
    logger.warning(
        "unknown exchange order status %r — recording the order as 'open' "
        "until reconciliation resolves it",
        exchange_status,
    )
    return "open"


def _parse_order_status(payload: Any) -> tuple[str, str] | None:
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
