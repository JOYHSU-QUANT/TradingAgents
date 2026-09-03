"""Decision-cycle audit tables — snapshots, ``ai_inputs`` / ``ai_outputs``, attempts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, NamedTuple

from ...common.enum_guard import check_enum
from ._base import _UNSET, _encode, _insert, _iso_utc, _Unset
from ._vocab import _ATTEMPT_STATUSES, _MODES, ERROR_TYPES

__all__ = [
    "PromptRegime",
    "find_in_progress_attempt",
    "get_decision_attempt",
    "insert_account_snapshot",
    "insert_ai_input",
    "insert_ai_output",
    "insert_decision_attempt",
    "insert_position_snapshot",
    "ai_inputs_without_format_fingerprint",
    "prompt_regime_counts",
    "stamp_ai_input_format_fingerprint",
    "update_decision_attempt",
]


# --------------------------------------------------------------------------
# snapshots / ai_inputs / ai_outputs — thin typed inserts; decision_attempts below
# --------------------------------------------------------------------------


def _check_mode(fields: dict[str, Any]) -> None:
    """Vocabulary-check ``fields["mode"]`` when present, like the fill/order writers.

    An absent ``mode`` still falls through to the column's NOT NULL constraint
    (``sqlite3.IntegrityError``), same as before the check existed — this guard
    only closes the silent-persistence lane: an out-of-vocabulary mode would be
    written verbatim (``_vocab._MODES``'s fail-loud-on-typo rule never fires),
    and a reader that splits these tables by mode would then drop or misfile
    the row.
    """
    if "mode" in fields:
        check_enum(fields["mode"], _MODES, name="mode")


def insert_account_snapshot(conn: sqlite3.Connection, **fields: Any) -> None:
    _check_mode(fields)
    _insert(conn, "account_snapshots", fields)


def insert_position_snapshot(conn: sqlite3.Connection, **fields: Any) -> None:
    _check_mode(fields)
    _insert(conn, "position_snapshots", fields)


def insert_ai_input(conn: sqlite3.Connection, **fields: Any) -> None:
    _check_mode(fields)
    _insert(conn, "ai_inputs", fields)


def insert_ai_output(conn: sqlite3.Connection, **fields: Any) -> None:
    _check_mode(fields)
    _insert(conn, "ai_outputs", fields)


def insert_decision_attempt(conn: sqlite3.Connection, **fields: Any) -> None:
    """Insert a decision attempt. Raises on a duplicate ``(run_id, scheduled_at)``.

    The deterministic ``decision_attempt_id`` plus the UNIQUE ``(run_id,
    scheduled_at)`` keep one scheduled cycle to one attempt row across restarts
    (phase2-spec §3.1).
    """
    _check_mode(fields)
    if "status" in fields:
        check_enum(fields["status"], _ATTEMPT_STATUSES, name="status")
    if fields.get("error_type") is not None:
        check_enum(fields["error_type"], ERROR_TYPES, name="error_type")
    _insert(conn, "decision_attempts", fields)


def get_decision_attempt(conn: sqlite3.Connection, decision_attempt_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM decision_attempts WHERE decision_attempt_id = ?",
        (decision_attempt_id,),
    ).fetchone()


class PromptRegime(NamedTuple):
    """One bucket of the prompt segmentation: the three keys and its cycle count.

    ``None`` in a key is a row written before the column existed (v10 for the
    shape, v11 for the fingerprint) — "unknown", not "none of that kind".
    """

    prompt_version: str | None
    context_shape: str | None
    format_fingerprint: str | None
    cycles: int


def prompt_regime_counts(
    conn: sqlite3.Connection, run_id: str, *, statuses: Iterable[str]
) -> tuple[PromptRegime, ...]:
    """The run's cycles split by ``(prompt_version, context_shape, format_fingerprint)``.

    One row per bucket, ordered by the bucket's FIRST cycle, so a run that
    straddled a prompt-regime boundary reads as before / after (issue #129).
    Counted over ``decision_attempts`` in ``statuses`` — the caller passes the
    same set its ``cycle_count`` uses, so the buckets sum to that number —
    through the attempt's ``input_id``, i.e. the try that reached the terminal
    status: one count per CYCLE, never one per retried try, and the keys are
    the ones the decided-on prompt carried. A status the vocabulary rejects
    fails here rather than matching nothing.
    """
    statuses = tuple(statuses)
    for status in statuses:
        check_enum(status, _ATTEMPT_STATUSES, name="status")
    placeholders = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        "SELECT i.prompt_version, i.context_shape, i.format_fingerprint, COUNT(*) AS cycles"
        " FROM decision_attempts a JOIN ai_inputs i ON i.input_id = a.input_id"
        f" WHERE a.run_id = ? AND a.status IN ({placeholders})"
        " GROUP BY i.prompt_version, i.context_shape, i.format_fingerprint"
        " ORDER BY MIN(a.scheduled_at)",
        (run_id, *statuses),
    ).fetchall()
    return tuple(
        PromptRegime(
            row["prompt_version"], row["context_shape"], row["format_fingerprint"], row["cycles"]
        )
        for row in rows
    )


def ai_inputs_without_format_fingerprint(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """The run's ``ai_inputs`` rows written before the v11 column existed, oldest first.

    What the offline backfill (``persistence.backfill``) works from: the row
    id, the payload path and digest it needs to prove the file it reads is
    the artifact the row describes, and the other two segmentation keys so
    it can tell a pre-v10 row (no shape either) from a pre-v11 one. A list,
    not a cursor: the caller reads files between this and its write
    transaction on the same connection.
    """
    return conn.execute(
        "SELECT input_id, input_payload_path, input_payload_hash, prompt_version, context_shape"
        " FROM ai_inputs WHERE run_id = ? AND format_fingerprint IS NULL"
        " ORDER BY timestamp, rowid",
        (run_id,),
    ).fetchall()


def stamp_ai_input_format_fingerprint(
    conn: sqlite3.Connection, input_id: str, fingerprint: str
) -> int:
    """Fill ONE row's ``NULL`` ``format_fingerprint``; returns rows written (0 or 1).

    The predicate is the contract: only a ``NULL`` cell (a value the daemon
    stamped is never replaced by a recomputation), and only on a row that
    already carries the other two keys — the triple is one set
    (``DecisionInput``), and a fingerprint on a shapeless pre-v10 row would
    be a half-stamped bucket the daemon never writes. Rules and counts:
    ``persistence.backfill``.
    """
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(f"format_fingerprint must be a non-empty string, got {fingerprint!r}")
    cursor = conn.execute(
        "UPDATE ai_inputs SET format_fingerprint = ?"
        " WHERE input_id = ? AND format_fingerprint IS NULL"
        " AND prompt_version IS NOT NULL AND context_shape IS NOT NULL",
        (fingerprint, input_id),
    )
    return cursor.rowcount


def find_in_progress_attempt(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """The run's single non-terminal attempt, or ``None``.

    The scheduler keeps at most one attempt ``in_progress`` (a new cycle is only
    scheduled once the previous attempt reached a terminal status); two live rows
    would mean the state machine broke, so fail loud rather than pick one.
    """
    rows = conn.execute(
        "SELECT * FROM decision_attempts WHERE run_id = ? AND status = 'in_progress'"
        " ORDER BY rowid",
        (run_id,),
    ).fetchall()
    if len(rows) > 1:
        ids = ", ".join(r["decision_attempt_id"] for r in rows)
        raise ValueError(f"run {run_id!r} has {len(rows)} in-progress attempts ({ids})")
    return rows[0] if rows else None


def update_decision_attempt(
    conn: sqlite3.Connection,
    decision_attempt_id: str,
    *,
    input_id: str | None | _Unset = _UNSET,
    output_id: str | None | _Unset = _UNSET,
    attempt_count: int | _Unset = _UNSET,
    first_attempt_at: datetime | _Unset = _UNSET,
    last_attempt_at: datetime | _Unset = _UNSET,
    status: str | _Unset = _UNSET,
    error_type: str | None | _Unset = _UNSET,
    error_message: str | None | _Unset = _UNSET,
    next_decision_at: datetime | None | _Unset = _UNSET,
    pending_raw_response: str | None | _Unset = _UNSET,
    timestamp: datetime | None = None,
) -> None:
    """Patch-update one attempt row; always stamp ``timestamp`` (last state change).

    Same convention as :func:`update_order`: an omitted keyword leaves the column
    untouched, an explicit ``None`` clears it. A terminal row is immutable —
    phase2-spec §3.1's exactly-once retry accounting depends on ``api_failed`` /
    ``completed`` / ``invalid_output`` never being reopened or re-counted.
    """
    if not isinstance(status, _Unset):
        check_enum(status, _ATTEMPT_STATUSES, name="status")
    if not isinstance(error_type, _Unset) and error_type is not None:
        check_enum(error_type, ERROR_TYPES, name="error_type")
    row = get_decision_attempt(conn, decision_attempt_id)
    if row is None:
        raise ValueError(f"decision attempt {decision_attempt_id!r} does not exist")
    if row["status"] != "in_progress":
        raise ValueError(
            f"decision attempt {decision_attempt_id!r} is terminal ({row['status']}) and immutable"
        )
    provided: dict[str, Any] = {}
    for col, val in (
        ("input_id", input_id),
        ("output_id", output_id),
        ("attempt_count", attempt_count),
        ("first_attempt_at", first_attempt_at),
        ("last_attempt_at", last_attempt_at),
        ("status", status),
        ("error_type", error_type),
        ("error_message", error_message),
        ("next_decision_at", next_decision_at),
        ("pending_raw_response", pending_raw_response),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    provided["timestamp"] = _iso_utc(timestamp or datetime.now(timezone.utc))
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE decision_attempts SET {assignments} WHERE decision_attempt_id = ?",
        (*provided.values(), decision_attempt_id),
    )
