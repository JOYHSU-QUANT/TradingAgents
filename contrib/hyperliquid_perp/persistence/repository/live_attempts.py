"""The ``live_order_attempts`` table (§8.3 / §16.5) — one row per exchange round-trip."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ...domains.perp.enum_guard import check_enum
from ..models import Side
from ._base import _UNSET, _encode, _insert, _Unset
from ._vocab import (
    _LIVE_ATTEMPT_ACTIONS,
    _LIVE_ATTEMPT_STATUSES,
    _ORDER_ROLES,
    EXCHANGE_KNOWN_ATTEMPT_STATUSES,
)

__all__ = [
    "get_live_order_attempt",
    "has_exchange_known_cloid",
    "has_place_attempt",
    "insert_live_order_attempt",
    "iter_live_order_attempts",
    "next_live_attempt_index",
    "update_live_order_attempt",
]


# --------------------------------------------------------------------------
# live_order_attempts (phase3-spec §8.3 / §16.5) — one row per exchange round-trip
# --------------------------------------------------------------------------


def insert_live_order_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    run_id: str,
    action: str,
    symbol: str,
    attempt_index: int,
    status: str = "submitted",
    order_id: str | None = None,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    exchange_order_id: str | None = None,
    side: str | None = None,
    qty: Decimal | None = None,
    price: Decimal | None = None,
    reduce_only: bool | None = None,
    order_role: str | None = None,
    requested_at: datetime | None = None,
) -> None:
    """Record an exchange round-trip BEFORE the network call (status 'submitted').

    A crash between this insert and the outcome patch leaves durable evidence
    that an order MAY exist on the exchange — the state §8.3's query-before-
    resend protocol resolves. The UNIQUE (cloid_hex, action, attempt_index)
    makes a retry a new row rather than an overwrite of that evidence.
    """
    check_enum(action, _LIVE_ATTEMPT_ACTIONS, name="action")
    check_enum(status, _LIVE_ATTEMPT_STATUSES, name="status")
    if order_role is not None:
        check_enum(order_role, _ORDER_ROLES, name="order_role")
    if attempt_index < 0:
        raise ValueError(f"attempt_index must be >= 0, got {attempt_index}")
    if (cloid_logical is None) != (cloid_hex is None):
        raise ValueError(
            "cloid_logical and cloid_hex must be provided together (the §8.2 "
            "two-layer id is one unit)"
        )
    if action == "place":
        if cloid_hex is None:
            raise ValueError("a 'place' attempt must carry its cloid pair (§8.3 rule 1)")
        # The identifiers alone are not evidence. §8.3/§16.5 exist so a recovery
        # or PR 4's reconciliation can compare what we INTENDED to send against
        # what the exchange holds; an attempt row with NULL side/qty/price cannot
        # support either, and it is written before the network call, so it is the
        # only record of the intent if the process dies inside the send window.
        missing = [
            name
            for name, value in (
                ("side", side),
                ("qty", qty),
                ("price", price),
                ("order_role", order_role),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"a 'place' attempt must carry the order parameters it sent; "
                f"missing {', '.join(missing)} (§8.3 / §16.5)"
            )
    if action == "cancel" and exchange_order_id is None:
        raise ValueError("a 'cancel' attempt must carry exchange_order_id")
    if action == "cancel_by_cloid" and cloid_hex is None:
        raise ValueError("a 'cancel_by_cloid' attempt must carry its cloid pair")
    _insert(
        conn,
        "live_order_attempts",
        {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "order_id": order_id,
            "action": action,
            "cloid_logical": cloid_logical,
            "cloid_hex": cloid_hex,
            "exchange_order_id": exchange_order_id,
            "symbol": symbol,
            "side": None if side is None else Side.parse(side).value,
            "qty": qty,
            "price": price,
            "reduce_only": reduce_only,
            "order_role": order_role,
            "attempt_index": attempt_index,
            "status": status,
            "requested_at": requested_at or datetime.now(timezone.utc),
        },
    )


def update_live_order_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    status: str | _Unset = _UNSET,
    exchange_order_id: str | None | _Unset = _UNSET,
    exchange_status: str | None | _Unset = _UNSET,
    error_message: str | None | _Unset = _UNSET,
    # No `None` in this union, deliberately: an explicit None CLEARS the column,
    # and clearing it would delete the pointer to a live order's exchange evidence.
    # Nothing ever wants that, so a caller holding a `str | None` path (a write that
    # may have failed) cannot pass it here without first turning None into UNSET —
    # live.payloads.payload_column does exactly that. Enforced by the type, not by
    # every call site remembering.
    raw_exchange_payload_path: str | _Unset = _UNSET,
    acknowledged_at: datetime | None | _Unset = _UNSET,
) -> None:
    """Patch an attempt with its outcome (same _UNSET convention as update_order).

    An attempt row is patched exactly once, from ``submitted`` to its outcome:
    the trail is append-only evidence (schema §16.5), and the §8.3 pre-send
    check trusts these statuses to decide whether an earlier send's outcome is
    known — rewriting a settled attempt would falsify that record.

    A row that stays ``submitted`` forever is a defined terminal state, not a
    leak: it means the process died inside that send's network window and the
    attempt's own ack was never observed. §8.3 recovery resolves the ORDER's
    fate via orderStatus and records it on the orders row (the authoritative
    surface PR 4 reconciles against); it deliberately does not back-patch the
    dangling attempt, because that send's direct result is genuinely unknown
    (several attempts may exist and any one of them could be the winner).
    """
    if not isinstance(status, _Unset):
        check_enum(status, _LIVE_ATTEMPT_STATUSES, name="status")
    row = get_live_order_attempt(conn, attempt_id)
    if row is None:
        raise ValueError(f"live order attempt {attempt_id!r} does not exist")
    if row["status"] != "submitted":
        raise ValueError(
            f"live order attempt {attempt_id!r} already settled as {row['status']!r} "
            "— the evidence trail is immutable; a retry is a NEW attempt row"
        )
    provided: dict[str, Any] = {}
    for col, val in (
        ("status", status),
        ("exchange_order_id", exchange_order_id),
        ("exchange_status", exchange_status),
        ("error_message", error_message),
        ("raw_exchange_payload_path", raw_exchange_payload_path),
        ("acknowledged_at", acknowledged_at),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    if not provided:
        return
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE live_order_attempts SET {assignments} WHERE attempt_id = ?",
        (*provided.values(), attempt_id),
    )


def get_live_order_attempt(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM live_order_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()


def iter_live_order_attempts(
    conn: sqlite3.Connection, run_id: str, *, cloid_hex: str | None = None
) -> list[sqlite3.Row]:
    """A run's attempts in insertion order, optionally for one cloid."""
    if cloid_hex is None:
        return conn.execute(
            "SELECT * FROM live_order_attempts WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM live_order_attempts WHERE run_id = ? AND cloid_hex = ? ORDER BY rowid",
        (run_id, cloid_hex),
    ).fetchall()


def has_place_attempt(conn: sqlite3.Connection, *, cloid_hex: str) -> bool:
    """§8.3 rules 3–5 pre-check: was this cloid EVER sent, whatever the outcome?

    Any prior place attempt — including one still stuck at 'submitted', and
    including an 'acknowledged' one — forces the caller through orderStatus
    before it may send: the exchange's duplicate rejection guards only OPEN
    orders, so a filled or expired cloid would be accepted again as a brand-new
    order. Deliberately status-blind, which is what makes it total.

    Keyed on the cloid ALONE, with no run filter — one scope for one evidence
    trail, matching next_live_attempt_index and the orders arm of
    has_exchange_known_cloid. A run filter would be redundant today (a
    cloid_logical embeds its run_id, and assert_cloid_provenance enforces it) but
    is not harmless: PR 4's reconciliation asks these predicates about orders it
    found on the EXCHANGE, which may belong to an earlier run, and a run-scoped
    answer of False would read as "never sent" — licensing exactly the resend
    rule 10 exists to forbid.
    """
    row = conn.execute(
        "SELECT 1 FROM live_order_attempts WHERE cloid_hex = ? AND action = 'place' LIMIT 1",
        (cloid_hex,),
    ).fetchone()
    return row is not None


def has_exchange_known_cloid(conn: sqlite3.Connection, *, cloid_hex: str) -> bool:
    """§8.3 rule 10: does durable local evidence prove the exchange took this cloid?

    Such evidence outranks a later "unknownOid" from orderStatus (Info lag,
    retention expiry): the send WAS received, so a resend would be accepted as a
    brand-new order — a double position if the original is live or filled.

    The evidence lives in TWO places, and both must be consulted:

    1. A PLACE attempt in EXCHANGE_KNOWN_ATTEMPT_STATUSES (acknowledged /
       duplicate) — the exchange answered this process directly.
    2. ``orders.exchange_order_id`` non-NULL — the exchange handed us an oid for
       this cloid. This is the ONLY record a successful §8.3 recovery leaves:
       recovery deliberately does not back-patch the attempt row (the orders row
       is PR 4's reconciliation authority), and the pre-check path recovers with
       no attempt row at all. Without this arm, the sequence "send times out
       (attempt 'failed') -> retry recovers the resting order via orderStatus ->
       a LATER retry gets unknownOid" reads as "the exchange never took it", and
       a live order is resent.

    Only an exchange-supplied oid ever reaches that column (the accepted ack and
    the orderStatus recovery); a rejected ack deliberately leaves it NULL, and
    OrderAck forbids an oid on an error status. So it is exact proof of receipt,
    and it does not narrow rule 5's legitimate resend of a cloid the exchange
    truly never took.

    The ``action = 'place'`` filter lives INSIDE this helper on purpose. The kill
    switch writes 'acknowledged' on ``cancel_by_cloid`` rows too, and an
    acknowledged CANCEL proves the exchange saw the cancel, not the place — a
    caller that hand-rolled the status test could drop the action filter and
    silently read one as the other.

    Neither arm is run-scoped, and that is deliberate: proof that the exchange
    took a cloid does not expire when a run does. The orders arm never was
    scoped; the attempts arm no longer is either, so one evidence trail has one
    scope. PR 4 reconciles orders found on the EXCHANGE — which may have been
    placed by an earlier run — and a run-scoped False there would mean "not this
    run" while reading as "never sent".
    """
    placeholders = ", ".join("?" for _ in EXCHANGE_KNOWN_ATTEMPT_STATUSES)
    attempt = conn.execute(
        "SELECT 1 FROM live_order_attempts "
        "WHERE cloid_hex = ? AND action = 'place' "
        f"AND status IN ({placeholders}) "
        "LIMIT 1",
        (cloid_hex, *EXCHANGE_KNOWN_ATTEMPT_STATUSES),
    ).fetchone()
    if attempt is not None:
        return True
    order = conn.execute(
        "SELECT 1 FROM orders WHERE cloid_hex = ? AND exchange_order_id IS NOT NULL LIMIT 1",
        (cloid_hex,),
    ).fetchone()
    return order is not None


def next_live_attempt_index(conn: sqlite3.Connection, *, action: str, cloid_hex: str) -> int:
    """The next free attempt_index for one (cloid, action) — 0 for a first send.

    Derived from the store, not an in-memory counter, and with NO run filter:
    the UNIQUE (cloid_hex, action, attempt_index) evidence trail spans runs —
    a later run's shutdown sweep cancels an earlier run's surviving order —
    so the index namespace must span them too, or the second run re-derives
    an index the constraint already holds and every cancel of that order
    fails on the INSERT before a round-trip is even attempted.
    """
    check_enum(action, _LIVE_ATTEMPT_ACTIONS, name="action")
    row = conn.execute(
        "SELECT MAX(attempt_index) FROM live_order_attempts WHERE action = ? AND cloid_hex = ?",
        (action, cloid_hex),
    ).fetchone()
    return 0 if row[0] is None else row[0] + 1
