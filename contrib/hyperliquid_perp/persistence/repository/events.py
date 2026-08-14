"""Kill-switch, protection-order and accounting-adjustment event logs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ...domains.perp.enum_guard import check_enum
from ._base import _insert
from ._vocab import (
    ACCOUNTING_ADJUSTMENT_TYPES,
    KILL_SWITCH_EVENT_TYPES,
    PROTECTION_ORDER_EVENT_TYPES,
)

__all__ = [
    "get_accounting_adjustment_event",
    "insert_accounting_adjustment_event",
    "insert_kill_switch_event",
    "insert_protection_order_event",
    "iter_accounting_adjustment_events",
    "iter_kill_switch_events",
    "iter_protection_order_events",
]


# --------------------------------------------------------------------------
# kill_switch_events (phase3-spec §18.5)
# --------------------------------------------------------------------------


def insert_kill_switch_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    detail: str | None = None,
    error_message: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    check_enum(event_type, KILL_SWITCH_EVENT_TYPES, name="event_type")
    _insert(
        conn,
        "kill_switch_events",
        {
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "event_type": event_type,
            "detail": detail,
            "error_message": error_message,
        },
    )


def iter_kill_switch_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM kill_switch_events WHERE run_id = ? ORDER BY event_id", (run_id,)
    ).fetchall()


# --------------------------------------------------------------------------
# protection_order_events (phase3-spec §17 / §16.5) — PR 5 SL/TP lifecycle
# --------------------------------------------------------------------------


def insert_protection_order_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    symbol: str,
    order_id: str | None = None,
    cloid_hex: str | None = None,
    detail: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Append one §17 SL/TP protection-lifecycle event (audit trail).

    ``order_id`` / ``cloid_hex`` name the protection order the event is about
    when there is one (a placement / modify / repair failure); the escalation
    and clear events may carry only the reason in ``detail``.
    """
    check_enum(event_type, PROTECTION_ORDER_EVENT_TYPES, name="event_type")
    _insert(
        conn,
        "protection_order_events",
        {
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "event_type": event_type,
            "symbol": symbol,
            "order_id": order_id,
            "cloid_hex": cloid_hex,
            "detail": detail,
        },
    )


def iter_protection_order_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM protection_order_events WHERE run_id = ? ORDER BY event_id", (run_id,)
    ).fetchall()


# --------------------------------------------------------------------------
# accounting_adjustment_events (phase3-spec §15 / §16.5)
# --------------------------------------------------------------------------


def insert_accounting_adjustment_event(
    conn: sqlite3.Connection,
    *,
    adjustment_id: str,
    run_id: str,
    adjustment_type: str,
    target_table: str,
    target_id: str,
    field: str,
    old_value: Decimal | None = None,
    new_value: Decimal | None = None,
    reason: str | None = None,
    source: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Record one §15 accounting correction. Raises ``IntegrityError`` on a duplicate id.

    The deterministic ``adjustment_id`` (see :mod:`..ids`) makes a backfill
    exactly-once: a reconciliation job that re-learns the same fee/funding
    re-derives the same id and the PRIMARY KEY rejects the second write, so the
    correction never posts to the wallet twice (the atomicity is the caller's —
    the ledger delta and this insert share one transaction).

    ``old_value`` / ``new_value`` are the correction's before/after amounts; live
    replay folds ``new_value - old_value`` into the ledger per ``adjustment_type``
    (fee reduces the wallet, funding / realized_pnl move it by their sign), so the
    pair is load-bearing, not just descriptive.

    The target trio is REQUIRED: every §15 correction amends one recorded row.
    A NULL ``target_id`` would be folded into the replayed ledger by
    ``_fold_adjustments`` (which iterates every event) yet be invisible to the
    per-fill fee chain (``iter_accounting_adjustment_events(target_id=...)``) —
    the two reads would silently disagree about the same correction.
    """
    check_enum(adjustment_type, ACCOUNTING_ADJUSTMENT_TYPES, name="adjustment_type")
    if not target_table or not target_id or not field:
        raise ValueError(
            "an accounting adjustment must name its target_table, target_id and field "
            "(§15 corrections amend one recorded row)"
        )
    _insert(
        conn,
        "accounting_adjustment_events",
        {
            "adjustment_id": adjustment_id,
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "adjustment_type": adjustment_type,
            "target_table": target_table,
            "target_id": target_id,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "source": source,
        },
    )


def get_accounting_adjustment_event(
    conn: sqlite3.Connection, adjustment_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounting_adjustment_events WHERE adjustment_id = ?", (adjustment_id,)
    ).fetchone()


def iter_accounting_adjustment_events(
    conn: sqlite3.Connection, run_id: str, *, target_id: str | None = None
) -> list[sqlite3.Row]:
    """A run's accounting adjustments in insertion order, optionally for one target."""
    if target_id is None:
        return conn.execute(
            "SELECT * FROM accounting_adjustment_events WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM accounting_adjustment_events WHERE run_id = ? AND target_id = ? "
        "ORDER BY rowid",
        (run_id, target_id),
    ).fetchall()
