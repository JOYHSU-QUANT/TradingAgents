"""The ``safe_mode_events`` table (phase3-spec §13.6) — safe-mode history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ...common.enum_guard import check_enum
from ._base import _insert
from ._vocab import SAFE_MODE_EVENT_TYPES, SAFE_MODE_TYPES

__all__ = ["has_safe_mode_reason_event", "insert_safe_mode_event", "iter_safe_mode_events"]


# --------------------------------------------------------------------------
# safe_mode_events (phase3-spec §13.6) — safe-mode history; current state
# lives on scheduler_state (§16.6)
# --------------------------------------------------------------------------


def insert_safe_mode_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    safe_mode_type: str | None = None,
    reason: str | None = None,
    released_by: str | None = None,
    detail: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Append one §13.6 safe-mode history row (state changes are auditable).

    An entered/escalated/reason-added event must name the type and reason it
    moved to (or observed); a released event must name who released it
    (``"auto_recovery"`` or the CLI operator string) — a release row with no
    author would defeat the §13.6 audit trail the CLI subcommand exists to
    leave.
    """
    check_enum(event_type, SAFE_MODE_EVENT_TYPES, name="event_type")
    if event_type in (
        "safe_mode_entered",
        "safe_mode_escalated",
        "safe_mode_reason_added",
    ) and (safe_mode_type is None or reason is None):
        raise ValueError(f"{event_type} requires safe_mode_type and reason")
    if event_type == "safe_mode_released" and not released_by:
        raise ValueError("safe_mode_released requires released_by (§13.6 audit trail)")
    if safe_mode_type is not None:
        check_enum(safe_mode_type, SAFE_MODE_TYPES, name="safe_mode_type")
    _insert(
        conn,
        "safe_mode_events",
        {
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "event_type": event_type,
            "safe_mode_type": safe_mode_type,
            "reason": reason,
            "released_by": released_by,
            "detail": detail,
        },
    )


def iter_safe_mode_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """A run's safe-mode history in insertion order."""
    return conn.execute(
        "SELECT * FROM safe_mode_events WHERE run_id = ? ORDER BY event_id",
        (run_id,),
    ).fetchall()


def has_safe_mode_reason_event(
    conn: sqlite3.Connection, run_id: str, *, reason: str, since_iso: str
) -> bool:
    """Whether this episode already carries a history row for ``reason``.

    The idempotence key for ``safe_mode_reason_added`` (decided 2026-07-17):
    an episode is bounded below by the current state's ``entered_at``, and a
    reason already named by ANY entered/escalated/reason-added row inside it
    needs no second row — the same mismatch re-observed every pass must not
    spam the history, exactly the discipline ``enter()`` keeps for the first
    reason. The comparison is lexicographic on our own ISO-8601 UTC stamps
    (single writer, single format), the same convention the store uses
    elsewhere.
    """
    return (
        conn.execute(
            "SELECT 1 FROM safe_mode_events WHERE run_id = ? AND reason = ? "
            "AND timestamp >= ? AND event_type IN "
            "('safe_mode_entered', 'safe_mode_escalated', 'safe_mode_reason_added') "
            "LIMIT 1",
            (run_id, reason, since_iso),
        ).fetchone()
        is not None
    )
