"""The ``exchange_reconciliation_events`` table (phase3-spec §12.3 / §16.5)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ...common.enum_guard import check_enum
from ._base import _insert
from ._vocab import _RECONCILIATION_TRIGGERS, RECONCILIATION_CASE_TYPES

__all__ = [
    "get_exchange_reconciliation_case",
    "has_exchange_reconciliation_case",
    "insert_exchange_reconciliation_event",
    "iter_exchange_reconciliation_events",
    "iter_unresolved_fill_sightings",
    "set_reconciliation_action",
    "stamp_reconciliation_action_if_unset",
]


# --------------------------------------------------------------------------
# exchange_reconciliation_events (phase3-spec §12.3 / §16.5)
# --------------------------------------------------------------------------


def insert_exchange_reconciliation_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    trigger: str,
    case_type: str,
    symbol: str | None = None,
    local_value: str | None = None,
    exchange_value: str | None = None,
    action_taken: str | None = None,
    detail: str | None = None,
    timestamp: datetime | None = None,
) -> bool:
    """Record one §12.3 reconciliation case sighting; ``True`` iff a row was written.

    Once per FACT rather than once per sighting, and the guard lives HERE at the
    write boundary: an unmapped/malformed fill is re-observed by every backfill
    pass over its window, and a writer that forgot the check-then-insert dance
    would bury the backlog in repeats of itself — so no writer gets to forget it.
    A re-sighting of an already-recorded ``(run_id, case_type, exchange_value)``
    writes nothing and returns ``False``. Rows with no ``exchange_value`` (a PR 4
    sweep case with no per-fill key) are not deduped — each is its own event.

    ``action_taken`` stays NULL from the PR 3 ingest writers (no ingest path acts
    on a case); the PR 4 sweep passes it when the disposition is known at write
    time (an orphan it back-filled, a stuck row it settled), and stamps it later
    through :func:`set_reconciliation_action` otherwise.
    """
    check_enum(case_type, RECONCILIATION_CASE_TYPES, name="case_type")
    check_enum(trigger, _RECONCILIATION_TRIGGERS, name="trigger")
    if exchange_value is not None and has_exchange_reconciliation_case(
        conn, run_id, case_type=case_type, exchange_value=exchange_value
    ):
        return False
    _insert(
        conn,
        "exchange_reconciliation_events",
        {
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "trigger": trigger,
            "case_type": case_type,
            "symbol": symbol,
            "local_value": local_value,
            "exchange_value": exchange_value,
            "action_taken": action_taken,
            "detail": detail,
        },
    )
    return True


def has_exchange_reconciliation_case(
    conn: sqlite3.Connection, run_id: str, *, case_type: str, exchange_value: str
) -> bool:
    """Whether this exact case was already recorded (the once-per-fact guard).

    Keyed on ``(run_id, case_type, exchange_value)`` — the fill key for an
    unmapped fill, the malformed evidence key for one that would not parse, the
    key + drift digest for a money-drift sighting. Resolution does NOT delete or
    flip these rows (they are a log); staying recorded after the fill is booked
    is what keeps a later re-sighting from re-inserting.
    """
    return (
        get_exchange_reconciliation_case(
            conn, run_id, case_type=case_type, exchange_value=exchange_value
        )
        is not None
    )


def get_exchange_reconciliation_case(
    conn: sqlite3.Connection, run_id: str, *, case_type: str, exchange_value: str
) -> sqlite3.Row | None:
    """The earliest recorded row for one deduped fact, or ``None``.

    The lookup side of the once-per-fact guard: when a later pass RESOLVES a
    fact whose sighting was already recorded (the dedupe swallows the fresh
    insert), the reconciler stamps the disposition onto THIS row via
    :func:`set_reconciliation_action` instead of losing it — otherwise the
    backlog would permanently show as unresolved a case that was in fact
    settled on a retry.
    """
    return conn.execute(
        "SELECT * FROM exchange_reconciliation_events "
        "WHERE run_id = ? AND case_type = ? AND exchange_value = ? "
        "ORDER BY event_id LIMIT 1",
        (run_id, case_type, exchange_value),
    ).fetchone()


def iter_exchange_reconciliation_events(
    conn: sqlite3.Connection, run_id: str, *, case_type: str | None = None
) -> list[sqlite3.Row]:
    """A run's §12.3 case rows in insertion order, optionally one case type."""
    if case_type is None:
        return conn.execute(
            "SELECT * FROM exchange_reconciliation_events WHERE run_id = ? ORDER BY event_id",
            (run_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM exchange_reconciliation_events WHERE run_id = ? AND case_type = ? "
        "ORDER BY event_id",
        (run_id, case_type),
    ).fetchall()


def set_reconciliation_action(conn: sqlite3.Connection, event_id: int, action_taken: str) -> None:
    """Stamp a case row's disposition (the §12.3 v10/v11 ``action_taken`` writer).

    Case rows are a log — resolution never rewrites ``case_type`` or the
    observed values, it only records what was DONE about the sighting (PR 4's
    sweep marking a malformed payload inspected, a drift audited, an orphan
    order cancelled). The row must exist; a second stamp overwrites the first
    (the disposition can be revised, the observation cannot).
    """
    if not action_taken or not action_taken.strip():
        raise ValueError("action_taken must be a non-empty string")
    cur = conn.execute(
        "UPDATE exchange_reconciliation_events SET action_taken = ? WHERE event_id = ?",
        (action_taken, event_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"exchange_reconciliation_events row {event_id!r} does not exist")


def stamp_reconciliation_action_if_unset(
    conn: sqlite3.Connection, event_id: int, action_taken: str
) -> bool:
    """Stamp a case's disposition ONLY while it has none; True if this call stamped it.

    The append-only-in-spirit variant of :func:`set_reconciliation_action`, for
    the operator surface. ``safe-mode --stamp-case`` takes no run lease, so its
    "is it already disposed of?" check and its write would otherwise straddle a
    window in which the daemon's own reconciliation pass stamps the machine
    disposition (what the system actually DID about the sighting) — and the
    operator's UPDATE would erase it with no error and no audit row. Putting the
    NULL test in the UPDATE's own WHERE makes check and write one atomic step,
    so the loser of the race is told instead of silently winning
    (2026-07-30 concurrency review).

    The daemon keeps :func:`set_reconciliation_action`: revising a disposition
    is legitimate there, and it already does its read, its test, and its write
    inside one transaction.
    """
    if not action_taken or not action_taken.strip():
        raise ValueError("action_taken must be a non-empty string")
    cur = conn.execute(
        "UPDATE exchange_reconciliation_events SET action_taken = ? "
        "WHERE event_id = ? AND action_taken IS NULL",
        (action_taken, event_id),
    )
    if cur.rowcount == 1:
        return True
    exists = conn.execute(
        "SELECT 1 FROM exchange_reconciliation_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if exists is None:
        raise ValueError(f"exchange_reconciliation_events row {event_id!r} does not exist")
    return False


def iter_unresolved_fill_sightings(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """``fill_unmapped`` sightings whose §14.2 key STILL has no fills row.

    The §12.3 (v10/v11) discovery list: for an unmapped sighting the
    ``exchange_value`` IS the dedupe key, so an anti-join against
    ``fills.exchange_fill_key`` yields exactly the fills the exchange reported
    that the ledger still lacks — "resolved" is the join starting to hit
    (re-ingest after a §8.3 recovery books the fill), never a flag on the row.
    Only ``fill_unmapped`` joins this way: a malformed sighting's key never
    matches a fills row by construction, and drift sightings describe fills
    that are ALREADY booked — both are excluded here and handled by the
    sweep's ``action_taken`` marking instead.
    """
    return conn.execute(
        "SELECT * FROM exchange_reconciliation_events e "
        "WHERE e.run_id = ? AND e.case_type = 'fill_unmapped' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM fills f WHERE f.exchange_fill_key = e.exchange_value"
        ") ORDER BY e.event_id",
        (run_id,),
    ).fetchall()
