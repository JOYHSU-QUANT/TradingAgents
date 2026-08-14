"""The ``current_account_state`` table — the materialized account ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ..models import AccountLedger
from ._base import _iso_utc

__all__ = [
    "get_current_account_state",
    "require_current_account_state",
    "upsert_current_account_state",
]


# --------------------------------------------------------------------------
# current_account_state (materialized account ledger)
# --------------------------------------------------------------------------


def upsert_current_account_state(
    conn: sqlite3.Connection,
    run_id: str,
    ledger: AccountLedger,
    *,
    updated_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO current_account_state
            (run_id, wallet_balance, realized_pnl, total_fees, net_funding_pnl, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            wallet_balance = excluded.wallet_balance,
            realized_pnl = excluded.realized_pnl,
            total_fees = excluded.total_fees,
            net_funding_pnl = excluded.net_funding_pnl,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            str(ledger.wallet_balance),
            str(ledger.realized_pnl),
            str(ledger.total_fees),
            str(ledger.net_funding_pnl),
            _iso_utc(updated_at or datetime.now(timezone.utc)),
        ),
    )


def get_current_account_state(conn: sqlite3.Connection, run_id: str) -> AccountLedger | None:
    row = conn.execute("SELECT * FROM current_account_state WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return AccountLedger(
        wallet_balance=Decimal(row["wallet_balance"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        total_fees=Decimal(row["total_fees"]),
        net_funding_pnl=Decimal(row["net_funding_pnl"]),
    )


def require_current_account_state(conn: sqlite3.Connection, run_id: str) -> AccountLedger:
    """The run's current ledger — fail loud when ``initialize_run`` never ran.

    The one definition of the check, because every poster that folds a delta into
    the ledger (paper ``apply_fill``/``record_funding``, live ``apply_live_fill``/
    ``backfill_fill_fee``) must agree that a missing ledger is an impossible state
    to refuse, never a zero to default to.
    """
    ledger = get_current_account_state(conn, run_id)
    if ledger is None:
        raise ValueError(f"run {run_id!r} has no account state; call initialize_run first")
    return ledger
