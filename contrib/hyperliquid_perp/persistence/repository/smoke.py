"""The ``live_smoke_tests`` table (phase3-spec §20.2) — the smoke-test result log."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ...domains.perp.enum_guard import check_enum
from ._base import _insert
from ._vocab import LIVE_SMOKE_TEST_STATUSES

__all__ = ["insert_smoke_test_result", "iter_smoke_test_results", "latest_smoke_test_results"]


# --------------------------------------------------------------------------
# live_smoke_tests (phase3-spec §20.2) — PR 6 testnet smoke-test result log
# --------------------------------------------------------------------------


def insert_smoke_test_result(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    test_number: int,
    test_key: str,
    test_name: str,
    status: str,
    network: str | None = None,
    dry_run: bool = False,
    detail: str | None = None,
    error_message: str | None = None,
    executed_at: datetime | None = None,
) -> None:
    """Append one §20.2 smoke-test result row (append-only audit).

    A re-run after a fix writes a NEW row rather than overwriting the earlier
    failure — the gate (:func:`latest_smoke_test_results`) reads the latest
    ``result_id`` per ``test_key``, so the history stays intact while the
    verdict follows the freshest attempt. ``dry_run`` marks a wiring check that
    placed no orders; such rows never satisfy the cycle-entry gate.
    """
    check_enum(status, LIVE_SMOKE_TEST_STATUSES, name="status")
    # A dry-run row placed no orders (it is a wiring check), so its only honest
    # verdict is "skipped". The gate (latest_smoke_test_results) already excludes
    # dry-run rows regardless of status, but a dry_run=1/status='passed' row could
    # still mislead a future direct reader — reject the contradiction at the write
    # boundary rather than relying on every reader to filter it out.
    if dry_run and status != "skipped":
        raise ValueError(
            f"a dry-run smoke row placed no orders and must be 'skipped', got {status!r}"
        )
    _insert(
        conn,
        "live_smoke_tests",
        {
            "run_id": run_id,
            "test_number": test_number,
            "test_key": test_key,
            "test_name": test_name,
            "status": status,
            "network": network,
            "dry_run": dry_run,
            "detail": detail,
            "error_message": error_message,
            "executed_at": executed_at or datetime.now(timezone.utc),
        },
    )


def iter_smoke_test_results(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """A run's full smoke-test history in execution (insertion) order."""
    return conn.execute(
        "SELECT * FROM live_smoke_tests WHERE run_id = ? ORDER BY result_id",
        (run_id,),
    ).fetchall()


def latest_smoke_test_results(
    conn: sqlite3.Connection, run_id: str, *, include_dry_run: bool = False
) -> dict[str, sqlite3.Row]:
    """``{test_key: latest row}`` — the freshest result per key.

    ``include_dry_run=False`` (the default, the gate's view) considers only
    real-connection rows: a dry-run wiring check must never stand in for a
    passing test in the §20.2 cycle-entry gate. The "latest" is the row with the
    greatest ``result_id`` (append-only, monotonic), so a re-run supersedes its
    predecessor without deleting the audit trail.
    """
    latest: dict[str, sqlite3.Row] = {}
    sql = "SELECT * FROM live_smoke_tests WHERE run_id = ?"
    if not include_dry_run:
        sql += " AND dry_run = 0"
    sql += " ORDER BY result_id"
    for row in conn.execute(sql, (run_id,)):
        # Ascending result_id: the last write for a key wins.
        latest[row["test_key"]] = row
    return latest
