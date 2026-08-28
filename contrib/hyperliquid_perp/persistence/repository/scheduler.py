"""The ``scheduler_state`` table — patch-style upsert, breadcrumbs and the run lease."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ...common.enum_guard import check_enum
from ._base import _UNSET, _encode, _iso_utc, _Unset
from ._vocab import (
    _CONFIG_DRIFT_STATUSES,
    _EXPORT_STATUSES,
    _REPLAY_STATUSES,
    SAFE_MODE_TYPES,
)

__all__ = ["get_scheduler_state", "iter_other_run_leases", "upsert_scheduler_state"]


def upsert_scheduler_state(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    last_decision_at: datetime | None | _Unset = _UNSET,
    next_decision_at: datetime | None | _Unset = _UNSET,
    last_input_id: str | None | _Unset = _UNSET,
    last_output_id: str | None | _Unset = _UNSET,
    current_attempt_id: str | None | _Unset = _UNSET,
    lock_pid: int | None | _Unset = _UNSET,
    lock_heartbeat_at: datetime | None | _Unset = _UNSET,
    last_export_status: str | None | _Unset = _UNSET,
    last_export_error: str | None | _Unset = _UNSET,
    last_export_at: datetime | None | _Unset = _UNSET,
    last_replay_status: str | None | _Unset = _UNSET,
    last_replay_error: str | None | _Unset = _UNSET,
    last_replay_at: datetime | None | _Unset = _UNSET,
    last_config_drift_status: str | None | _Unset = _UNSET,
    last_config_drift_error: str | None | _Unset = _UNSET,
    last_config_drift_at: datetime | None | _Unset = _UNSET,
    safe_mode_type: str | None | _Unset = _UNSET,
    safe_mode_reason: str | None | _Unset = _UNSET,
    safe_mode_entered_at: datetime | None | _Unset = _UNSET,
    day_start_equity: Decimal | None | _Unset = _UNSET,
    day_start_date: str | None | _Unset = _UNSET,
    consecutive_loss_count: int | None | _Unset = _UNSET,
    last_settlement_wallet_balance: Decimal | None | _Unset = _UNSET,
    updated_at: datetime | None = None,
) -> None:
    """Patch-style upsert: only the columns actually supplied change.

    An omitted keyword leaves the stored value untouched (NULL on a fresh row);
    an explicit ``None`` clears the column. Full-row replace semantics would
    force a caller advancing just ``next_decision_at`` to re-supply every other
    field — one forgotten keyword and the crash-recovery breadcrumbs
    (``last_input_id`` / ``current_attempt_id``) this table exists to keep
    would be silently NULLed. ``updated_at`` is always stamped.

    The §16.6 safe-mode trio moves as ONE unit: entering safe mode without a
    reason (or clearing the type while a reason lingers) would leave the §13.6
    current-state record self-contradictory across a restart, so a caller that
    touches any of the three must supply all three, all set or all ``None``.
    """
    safe_mode_trio = (safe_mode_type, safe_mode_reason, safe_mode_entered_at)
    provided_trio = [v for v in safe_mode_trio if not isinstance(v, _Unset)]
    if provided_trio and len(provided_trio) != 3:
        raise ValueError(
            "safe_mode_type / safe_mode_reason / safe_mode_entered_at must be "
            "supplied together (the §16.6 current state is one fact)"
        )
    if provided_trio:
        set_count = sum(1 for v in provided_trio if v is not None)
        if set_count not in (0, 3):
            raise ValueError(
                "safe_mode_type / safe_mode_reason / safe_mode_entered_at must be "
                "all set (entering) or all None (clearing), got a partial state"
            )
        if safe_mode_type is not None and not isinstance(safe_mode_type, _Unset):
            check_enum(safe_mode_type, SAFE_MODE_TYPES, name="safe_mode_type")
    # §10.3 daily-loss baseline: the equity and the UTC date it was captured on
    # are one fact (the day's starting point). Writing one without the other
    # would leave a baseline that either has no equity or no day to compare
    # against — same one-unit discipline as the safe-mode trio above.
    day_pair = (day_start_equity, day_start_date)
    provided_pair = [v for v in day_pair if not isinstance(v, _Unset)]
    if provided_pair and len(provided_pair) != 2:
        raise ValueError(
            "day_start_equity / day_start_date must be supplied together (the "
            "§10.3 daily-loss baseline is one fact)"
        )
    if not isinstance(last_export_status, _Unset) and last_export_status is not None:
        check_enum(last_export_status, _EXPORT_STATUSES, name="last_export_status")
    if not isinstance(last_replay_status, _Unset) and last_replay_status is not None:
        check_enum(last_replay_status, _REPLAY_STATUSES, name="last_replay_status")
    if not isinstance(last_config_drift_status, _Unset) and last_config_drift_status is not None:
        check_enum(
            last_config_drift_status, _CONFIG_DRIFT_STATUSES, name="last_config_drift_status"
        )
    provided: dict[str, Any] = {}
    if not isinstance(last_decision_at, _Unset):
        provided["last_decision_at"] = _encode(last_decision_at)
    if not isinstance(next_decision_at, _Unset):
        provided["next_decision_at"] = _encode(next_decision_at)
    if not isinstance(last_input_id, _Unset):
        provided["last_input_id"] = _encode(last_input_id)
    if not isinstance(last_output_id, _Unset):
        provided["last_output_id"] = _encode(last_output_id)
    if not isinstance(current_attempt_id, _Unset):
        provided["current_attempt_id"] = _encode(current_attempt_id)
    if not isinstance(lock_pid, _Unset):
        provided["lock_pid"] = _encode(lock_pid)
    if not isinstance(lock_heartbeat_at, _Unset):
        provided["lock_heartbeat_at"] = _encode(lock_heartbeat_at)
    if not isinstance(last_export_status, _Unset):
        provided["last_export_status"] = _encode(last_export_status)
    if not isinstance(last_export_error, _Unset):
        provided["last_export_error"] = _encode(last_export_error)
    if not isinstance(last_export_at, _Unset):
        provided["last_export_at"] = _encode(last_export_at)
    if not isinstance(last_replay_status, _Unset):
        provided["last_replay_status"] = _encode(last_replay_status)
    if not isinstance(last_replay_error, _Unset):
        provided["last_replay_error"] = _encode(last_replay_error)
    if not isinstance(last_replay_at, _Unset):
        provided["last_replay_at"] = _encode(last_replay_at)
    if not isinstance(last_config_drift_status, _Unset):
        provided["last_config_drift_status"] = _encode(last_config_drift_status)
    if not isinstance(last_config_drift_error, _Unset):
        provided["last_config_drift_error"] = _encode(last_config_drift_error)
    if not isinstance(last_config_drift_at, _Unset):
        provided["last_config_drift_at"] = _encode(last_config_drift_at)
    if not isinstance(safe_mode_type, _Unset):
        provided["safe_mode_type"] = _encode(safe_mode_type)
    if not isinstance(safe_mode_reason, _Unset):
        provided["safe_mode_reason"] = _encode(safe_mode_reason)
    if not isinstance(safe_mode_entered_at, _Unset):
        provided["safe_mode_entered_at"] = _encode(safe_mode_entered_at)
    if not isinstance(day_start_equity, _Unset):
        provided["day_start_equity"] = _encode(day_start_equity)
    if not isinstance(day_start_date, _Unset):
        provided["day_start_date"] = _encode(day_start_date)
    if not isinstance(consecutive_loss_count, _Unset):
        provided["consecutive_loss_count"] = _encode(consecutive_loss_count)
    if not isinstance(last_settlement_wallet_balance, _Unset):
        provided["last_settlement_wallet_balance"] = _encode(last_settlement_wallet_balance)
    provided["updated_at"] = _iso_utc(updated_at or datetime.now(timezone.utc))

    # Column names come from the fixed keyword list above, never caller data.
    columns = ["run_id", *provided]
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{col} = excluded.{col}" for col in provided)
    conn.execute(
        f"INSERT INTO scheduler_state ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(run_id) DO UPDATE SET {assignments}",
        (run_id, *provided.values()),
    )


def get_scheduler_state(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scheduler_state WHERE run_id = ?", (run_id,)).fetchone()


def iter_other_run_leases(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Lease rows for every OTHER run in this store that records a holder.

    The run lease is keyed on ``run_id``, but the actions it protects are not:
    the kill switch, ``updateLeverage`` and the §19.3 stale-order sweep are all
    per-WALLET, and two runs in one store share a wallet. Callers use this to
    refuse before doing wallet-wide work while a sibling run is live — and,
    since issue #129, before migrating the store, which is per-FILE and so
    reaches every sibling regardless of network or mode. Freshness is left to
    the caller (it owns the clock and ``LOCK_STALE_SECONDS``) (2026-07-30
    concurrency review).
    """
    return conn.execute(
        "SELECT run_id, lock_pid, lock_heartbeat_at FROM scheduler_state "
        "WHERE run_id != ? AND lock_pid IS NOT NULL AND lock_heartbeat_at IS NOT NULL",
        (run_id,),
    ).fetchall()
