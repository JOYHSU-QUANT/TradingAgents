"""The ``orders`` table (phase2-data §8) — typed insert + patch update."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ...common.enum_guard import check_enum
from ..models import Side
from ._base import _UNSET, _encode, _insert, _iso_utc, _Unset
from ._vocab import (
    _EXCHANGE_STATUS_FAMILIES,
    _FLIP_LEGS,
    _MODES,
    _ORDER_ROLES,
    _ORDER_STATUSES,
    _ORDER_TYPES,
    LIVE_ORDER_STATUSES,
    RESTING_ORDER_STATUSES,
)

__all__ = [
    "active_protection_order",
    "count_orders_by_role",
    "get_order",
    "get_order_by_cloid_hex",
    "get_order_by_exchange_order_id",
    "insert_order",
    "iter_open_live_orders",
    "iter_orders",
    "max_engine_seq",
    "update_order",
]


# --------------------------------------------------------------------------
# orders (phase2-data §8) — typed insert + patch update, first used by PR3
# --------------------------------------------------------------------------


def insert_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    mode: str,
    run_id: str,
    symbol: str,
    order_role: str,
    side: str,
    order_type: str,
    qty: Decimal,
    status: str,
    output_id: str | None = None,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
    parent_order_id: str | None = None,
    flip_plan_id: str | None = None,
    flip_leg: str | None = None,
    price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    filled_qty: Decimal = Decimal(0),
    remaining_qty: Decimal | None = None,
    status_reason: str | None = None,
    reduce_only: bool = False,
    active_from: datetime | None = None,
    timestamp: datetime | None = None,
    updated_at: datetime | None = None,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    exchange_status: str | None = None,
    exchange_raw_status: str | None = None,
    submitted_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    canceled_at: datetime | None = None,
    cancel_reason: str | None = None,
    is_bot_owned: bool | None = None,
    raw_exchange_payload_path: str | None = None,
) -> None:
    """Insert one order row (phase2-data §8; live columns phase3-spec §16.1).

    Enums validated at the write boundary. A live order writes the cloid pair
    (``client_order_id`` is deprecated — old paper rows only, no new writers);
    the pair travels together or not at all so no order can be queryable on the
    wire (§8.3 rule 7 uses cloid_hex) but unreadable in the audit trail.
    """
    check_enum(mode, _MODES, name="mode")
    check_enum(order_role, _ORDER_ROLES, name="order_role")
    check_enum(order_type, _ORDER_TYPES, name="type")
    check_enum(status, _ORDER_STATUSES, name="status")
    if exchange_status is not None:
        check_enum(exchange_status, _EXCHANGE_STATUS_FAMILIES, name="exchange_status")
    if flip_leg is not None:
        check_enum(flip_leg, _FLIP_LEGS, name="flip_leg")
    if (cloid_logical is None) != (cloid_hex is None):
        raise ValueError(
            "cloid_logical and cloid_hex must be provided together (the §8.2 "
            "two-layer id is one unit)"
        )
    now = timestamp or datetime.now(timezone.utc)
    _insert(
        conn,
        "orders",
        {
            "order_id": order_id,
            "timestamp": now,
            "mode": mode,
            "run_id": run_id,
            "output_id": output_id,
            "exchange_order_id": exchange_order_id,
            "client_order_id": client_order_id,
            "parent_order_id": parent_order_id,
            "flip_plan_id": flip_plan_id,
            "flip_leg": flip_leg,
            "symbol": symbol,
            "order_role": order_role,
            "side": Side.parse(side).value,
            "type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "qty": qty,
            "filled_qty": filled_qty,
            "remaining_qty": remaining_qty,
            "status": status,
            "status_reason": status_reason,
            "reduce_only": reduce_only,
            "active_from": active_from,
            "updated_at": updated_at or now,
            "cloid_logical": cloid_logical,
            "cloid_hex": cloid_hex,
            "exchange_status": exchange_status,
            "exchange_raw_status": exchange_raw_status,
            "submitted_at": submitted_at,
            "acknowledged_at": acknowledged_at,
            "canceled_at": canceled_at,
            "cancel_reason": cancel_reason,
            "is_bot_owned": is_bot_owned,
            "raw_exchange_payload_path": raw_exchange_payload_path,
        },
    )


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()


def active_protection_order(
    conn: sqlite3.Connection, run_id: str, symbol: str, order_role: str
) -> sqlite3.Row | None:
    """The resting (non-terminal) SL or TP order for a position, if any (§17).

    The §17 protection manager owns at most one live order per protection role
    at a time (one SL, one TP), so the modify-before-cancel path needs to find
    the current one to update or cancel it. Non-terminal = still on the book
    (``submitted`` before the ack, ``open`` / ``partially_filled`` after);
    a filled / canceled / rejected protection order is history. Returns the
    most recent match so a just-replaced order never shadows its replacement.

    The status set is :data:`RESTING_ORDER_STATUSES`, not a literal, so a caller
    that has to ask the SAME question of a non-local source (protection.py
    checking orderStatus when the rows are suspect) cannot drift from it.
    """
    placeholders = ", ".join("?" * len(RESTING_ORDER_STATUSES))
    return conn.execute(  # noqa: S608 — placeholders are '?' * a module constant
        "SELECT * FROM orders WHERE run_id = ? AND symbol = ? AND order_role = ? "
        f"AND status IN ({placeholders}) "
        "ORDER BY timestamp DESC, rowid DESC LIMIT 1",
        (run_id, symbol, order_role, *RESTING_ORDER_STATUSES),
    ).fetchone()


def count_orders_by_role(
    conn: sqlite3.Connection, run_id: str, symbol: str, order_role: str
) -> int:
    """How many orders of ``order_role`` a run has ever written for ``symbol``.

    The §17 protection manager derives each new SL/TP order's cloid sequence
    from this monotonic count, so successive protection orders get distinct
    cloids (a modify is a new logical identity, §17.4) while a retry — which
    writes no row until it succeeds — reuses the same count and therefore the
    same cloid (§8.3 idempotent resend).
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM orders WHERE run_id = ? AND symbol = ? AND order_role = ?",
            (run_id, symbol, order_role),
        ).fetchone()[0]
    )


def get_order_by_cloid_hex(conn: sqlite3.Connection, cloid_hex: str) -> sqlite3.Row | None:
    """The local order row an exchange-echoed cloid belongs to (§19.3 back-link).

    cloid_hex is unique per logical order by construction (one registry row per
    hex, one orders row per logical order), so at most one row can match; fail
    loud if the store ever contradicts that instead of picking one silently.
    """
    rows = conn.execute("SELECT * FROM orders WHERE cloid_hex = ?", (cloid_hex,)).fetchall()
    if len(rows) > 1:
        ids = ", ".join(r["order_id"] for r in rows)
        raise ValueError(f"cloid_hex {cloid_hex!r} maps to {len(rows)} orders ({ids})")
    return rows[0] if rows else None


def get_order_by_exchange_order_id(
    conn: sqlite3.Connection, exchange_order_id: str
) -> sqlite3.Row | None:
    """The local order row an exchange ``oid`` belongs to, or ``None`` (§14 mapping).

    A live fill carries the exchange order id (``oid``) it settled, not our
    ``order_id``; this is how the fill ingester resolves the fill to the bot
    order that produced it (and thence its cloid / roles). The exchange assigns
    one oid per order, so at most one row can match — fail loud if the store ever
    contradicts that, matching :func:`get_order_by_cloid_hex`. No run filter: an
    oid is globally unique at the exchange, and a later run may ingest a fill for
    an order an earlier run placed.
    """
    rows = conn.execute(
        "SELECT * FROM orders WHERE exchange_order_id = ?", (exchange_order_id,)
    ).fetchall()
    if len(rows) > 1:
        ids = ", ".join(r["order_id"] for r in rows)
        raise ValueError(
            f"exchange_order_id {exchange_order_id!r} maps to {len(rows)} orders ({ids})"
        )
    return rows[0] if rows else None


def max_engine_seq(conn: sqlite3.Connection, run_id: str) -> int:
    """The highest ``<run_id>:<tag>:<n>`` suffix already persisted for a run.

    The paper engine mints its order/plan/flip ids from a monotonic in-memory
    counter; an engine constructed over an existing run must resume *above* the
    persisted maximum or its first insert dies on (at best) a PRIMARY KEY
    collision. Scans ``orders.order_id``, ``execution_plans.plan_id`` and the
    ``flip_plan_id`` columns; ids not shaped like the engine's (no numeric tail
    after the run prefix) are ignored. Returns 0 for a fresh run.
    """
    prefix = f"{run_id}:"
    best = 0
    for sql in (
        "SELECT order_id FROM orders WHERE run_id = ?",
        "SELECT flip_plan_id FROM orders WHERE run_id = ? AND flip_plan_id IS NOT NULL",
        "SELECT plan_id FROM execution_plans WHERE run_id = ?",
        "SELECT flip_plan_id FROM execution_plans WHERE run_id = ? AND flip_plan_id IS NOT NULL",
    ):
        for (ident,) in conn.execute(sql, (run_id,)):
            if not ident.startswith(prefix):
                continue
            tail = ident.rsplit(":", 1)[-1]
            if tail.isdigit():
                best = max(best, int(tail))
    return best


def update_order(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    filled_qty: Decimal | _Unset = _UNSET,
    remaining_qty: Decimal | None | _Unset = _UNSET,
    status: str | _Unset = _UNSET,
    status_reason: str | None | _Unset = _UNSET,
    exchange_order_id: str | None | _Unset = _UNSET,
    exchange_status: str | None | _Unset = _UNSET,
    exchange_raw_status: str | None | _Unset = _UNSET,
    submitted_at: datetime | None | _Unset = _UNSET,
    acknowledged_at: datetime | None | _Unset = _UNSET,
    canceled_at: datetime | None | _Unset = _UNSET,
    cancel_reason: str | None | _Unset = _UNSET,
    is_bot_owned: bool | None | _Unset = _UNSET,
    # No `None` in this union, deliberately: an explicit None CLEARS the column,
    # and clearing it would delete the pointer to a live order's exchange evidence.
    # Nothing ever wants that, so a caller holding a `str | None` path (a write that
    # may have failed) cannot pass it here without first turning None into UNSET —
    # live.payloads.payload_column does exactly that. Enforced by the type, not by
    # every call site remembering.
    raw_exchange_payload_path: str | _Unset = _UNSET,
    updated_at: datetime | None = None,
) -> None:
    """Patch-update an order's mutable columns; always stamp ``updated_at``.

    Only supplied columns change (an omitted keyword is left untouched, an explicit
    ``None`` clears — same convention as :func:`upsert_scheduler_state`). The row
    must exist (an order is always inserted before it is updated). The cloid pair
    is deliberately NOT patchable: it is identity, fixed at insert (§8.3 rule 6 —
    a logical order never changes cloid).
    """
    if not isinstance(status, _Unset):
        check_enum(status, _ORDER_STATUSES, name="status")
    if not isinstance(exchange_status, _Unset) and exchange_status is not None:
        check_enum(exchange_status, _EXCHANGE_STATUS_FAMILIES, name="exchange_status")
    if get_order(conn, order_id) is None:
        raise ValueError(f"order {order_id!r} does not exist")
    provided: dict[str, Any] = {}
    for col, val in (
        ("filled_qty", filled_qty),
        ("remaining_qty", remaining_qty),
        ("status", status),
        ("status_reason", status_reason),
        ("exchange_order_id", exchange_order_id),
        ("exchange_status", exchange_status),
        ("exchange_raw_status", exchange_raw_status),
        ("submitted_at", submitted_at),
        ("acknowledged_at", acknowledged_at),
        ("canceled_at", canceled_at),
        ("cancel_reason", cancel_reason),
        ("is_bot_owned", is_bot_owned),
        ("raw_exchange_payload_path", raw_exchange_payload_path),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    provided["updated_at"] = _iso_utc(updated_at or datetime.now(timezone.utc))
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE orders SET {assignments} WHERE order_id = ?",
        (*provided.values(), order_id),
    )


def iter_orders(
    conn: sqlite3.Connection, run_id: str, *, statuses: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """A run's orders in insertion order, optionally filtered by status."""
    if statuses is None:
        return conn.execute(
            "SELECT * FROM orders WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    for status in statuses:
        check_enum(status, _ORDER_STATUSES, name="status")
    placeholders = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM orders WHERE run_id = ? AND status IN ({placeholders}) ORDER BY rowid",
        (run_id, *statuses),
    ).fetchall()


def iter_open_live_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every LIVE order this store still believes is non-terminal — any run.

    The §18.2 rule-6 disarm cross-check reads this. The shutdown sweep's notion
    of "clean" otherwise rests on one exchange read, and an empty open-orders
    answer cannot be told apart from an Info view that has not caught up with an
    order placed a second ago — so the local row is the durable counter-evidence,
    exactly as it is for §8.3 rule 10 (has_exchange_known_cloid).

    No run filter, for the same reason next_live_attempt_index has none: a later
    run's shutdown sweep is responsible for an earlier run's surviving orders.
    Paper rows never carry a cloid_hex, so they cannot appear here.
    """
    placeholders = ", ".join("?" for _ in LIVE_ORDER_STATUSES)
    return conn.execute(
        "SELECT * FROM orders WHERE mode = 'live' AND cloid_hex IS NOT NULL "
        f"AND status IN ({placeholders}) ORDER BY rowid",
        LIVE_ORDER_STATUSES,
    ).fetchall()
