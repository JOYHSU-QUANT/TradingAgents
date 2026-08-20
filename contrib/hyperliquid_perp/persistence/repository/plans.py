"""The ``execution_plans`` table (phase2-data §1.2) — typed insert + patch update."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ...common.enum_guard import check_enum
from ._base import _UNSET, _encode, _insert, _iso_utc, _Unset
from ._vocab import _FLIP_LEGS, _PLAN_STATUSES

__all__ = [
    "get_execution_plan",
    "insert_execution_plan",
    "iter_execution_plans",
    "update_execution_plan",
]


# --------------------------------------------------------------------------
# execution_plans (phase2-data §1.2) — typed insert + patch update
# --------------------------------------------------------------------------


def insert_execution_plan(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    run_id: str,
    symbol: str,
    status: str,
    created_at: datetime,
    output_id: str | None = None,
    flip_plan_id: str | None = None,
    flip_leg: str | None = None,
    deadline_at: datetime | None = None,
    planned_slices: int | None = None,
    total_qty: Decimal | None = None,
    remaining_qty: Decimal | None = None,
    residual_qty: Decimal | None = None,
    rounding_residual_qty: Decimal | None = None,
    status_reason: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    """Insert one execution-plan row (phase2-data §1.2 internal table)."""
    check_enum(status, _PLAN_STATUSES, name="status")
    if flip_leg is not None:
        check_enum(flip_leg, _FLIP_LEGS, name="flip_leg")
    _insert(
        conn,
        "execution_plans",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "output_id": output_id,
            "flip_plan_id": flip_plan_id,
            "flip_leg": flip_leg,
            "symbol": symbol,
            "status": status,
            "created_at": created_at,
            "deadline_at": deadline_at,
            "planned_slices": planned_slices,
            "total_qty": total_qty,
            "remaining_qty": remaining_qty,
            "residual_qty": residual_qty,
            "rounding_residual_qty": rounding_residual_qty,
            "status_reason": status_reason,
            "updated_at": updated_at or created_at,
        },
    )


def get_execution_plan(conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM execution_plans WHERE plan_id = ?", (plan_id,)).fetchone()


def iter_execution_plans(
    conn: sqlite3.Connection, run_id: str, *, statuses: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """A run's execution plans in insertion order, optionally filtered by status."""
    if statuses is None:
        return conn.execute(
            "SELECT * FROM execution_plans WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    for status in statuses:
        check_enum(status, _PLAN_STATUSES, name="status")
    placeholders = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM execution_plans WHERE run_id = ? AND status IN ({placeholders})"
        " ORDER BY rowid",
        (run_id, *statuses),
    ).fetchall()


def update_execution_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    status: str | _Unset = _UNSET,
    remaining_qty: Decimal | None | _Unset = _UNSET,
    residual_qty: Decimal | None | _Unset = _UNSET,
    status_reason: str | None | _Unset = _UNSET,
    updated_at: datetime | None = None,
) -> None:
    """Patch-update a plan's mutable columns; always stamp ``updated_at``."""
    if not isinstance(status, _Unset):
        check_enum(status, _PLAN_STATUSES, name="status")
    if get_execution_plan(conn, plan_id) is None:
        raise ValueError(f"execution plan {plan_id!r} does not exist")
    provided: dict[str, Any] = {}
    for col, val in (
        ("status", status),
        ("remaining_qty", remaining_qty),
        ("residual_qty", residual_qty),
        ("status_reason", status_reason),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    provided["updated_at"] = _iso_utc(updated_at or datetime.now(timezone.utc))
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE execution_plans SET {assignments} WHERE plan_id = ?",
        (*provided.values(), plan_id),
    )
