"""The ``runs`` and ``run_seed_positions`` tables — run genesis records."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ...domains.perp.enum_guard import check_enum
from ..models import PositionState
from ._base import _dec, _insert
from ._vocab import _MODES

__all__ = ["get_run", "get_run_seed_positions", "insert_run", "insert_run_seed_position"]


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    mode: str,
    initial_balance_usdc: Decimal,
    schema_version: int,
    config_json: str | None = None,
    created_at: datetime | None = None,
) -> None:
    check_enum(mode, _MODES, name="mode")
    _insert(
        conn,
        "runs",
        {
            "run_id": run_id,
            "mode": mode,
            "created_at": (created_at or datetime.now(timezone.utc)),
            "initial_balance_usdc": initial_balance_usdc,
            "config_json": config_json,
            "schema_version": schema_version,
        },
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def insert_run_seed_position(
    conn: sqlite3.Connection, run_id: str, position: PositionState
) -> None:
    """Record one seed position as applied at run creation (the replay genesis).

    Append-once alongside ``insert_run``: replay reads its starting positions from
    here (plus ``runs.initial_balance_usdc``) instead of trusting the caller's
    current config, so a YAML edited after run creation cannot shift the baseline.
    """
    _insert(
        conn,
        "run_seed_positions",
        {
            "run_id": run_id,
            "symbol": position.coin,
            "size": position.size,
            "entry_price": position.entry_price,
            "realized_pnl": position.realized_pnl,
        },
    )


def get_run_seed_positions(conn: sqlite3.Connection, run_id: str) -> list[PositionState]:
    rows = conn.execute(
        "SELECT * FROM run_seed_positions WHERE run_id = ? ORDER BY symbol", (run_id,)
    ).fetchall()
    return [
        PositionState(
            coin=r["symbol"],
            size=Decimal(r["size"]),
            entry_price=_dec(r["entry_price"]),
            realized_pnl=Decimal(r["realized_pnl"]),
        )
        for r in rows
    ]
