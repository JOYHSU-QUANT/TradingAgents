"""Typed insert / update / query helpers over the Phase 2 SQLite schema.

Every write runs inside a caller-supplied transaction (``with db.transaction()
as conn: ...``). The repository never opens its own transaction, so the
accounting layer can post a fill *and* update ``current_positions`` /
``current_account_state`` in one atomic unit (phase2-data §1.2: the materialized
current-state tables must update in the same transaction as the event that
changed them).

Values cross this seam as native Python types (``Decimal``, ``datetime``,
``bool``) and are encoded to the schema's storage form here — Decimals to TEXT
(no REAL float), datetimes to ISO-8601 UTC, bools to ``0`` / ``1`` — so callers
never hand-serialize. Reads decode the mutable ``current_*`` rows back into the
:mod:`.models` dataclasses; append-only event rows are returned as ``sqlite3.Row``
for the reporting / replay consumers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .models import AccountLedger, PositionState

__all__ = [
    "get_all_current_positions",
    "get_current_account_state",
    "get_current_position",
    "get_run",
    "get_scheduler_state",
    "insert_account_snapshot",
    "insert_ai_input",
    "insert_ai_output",
    "insert_decision_attempt",
    "insert_execution_plan",
    "insert_fill",
    "insert_funding_event",
    "insert_order",
    "insert_position_snapshot",
    "insert_run",
    "iter_fills",
    "iter_funding_events",
    "get_funding_event",
    "set_funding_status",
    "upsert_current_account_state",
    "upsert_current_position",
    "upsert_scheduler_state",
]


def _iso_utc(value: datetime) -> str:
    """Canonical ISO-8601 UTC string for a datetime; rejects naive datetimes.

    All stored timestamps must be UTC and comparable as strings: the funding
    dedup key ``(run_id, symbol, funding_timestamp)`` and the deterministic
    ``funding_event_id`` both derive from the stored/serialized timestamp, so a
    naive-vs-aware or non-UTC-offset representation of the *same* instant would
    produce different keys and let a settlement post twice. Normalising here — and
    refusing a naive datetime outright, as the audit log does — keeps every path
    on one canonical form.
    """
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware (UTC) before storage")
    return value.astimezone(timezone.utc).isoformat()


def _encode(value: Any) -> Any:
    """Map a native value to its SQLite storage form (see module docstring)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso_utc(value)
    return value


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    """Encoded ``INSERT INTO table (cols) VALUES (?, ...)`` — the one write path.

    Column names come from ``values`` keys (all module-internal, never user
    input), so the interpolated identifier list is not an injection surface;
    every *value* is bound as a parameter.
    """
    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    columns = ", ".join(cols)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        [_encode(values[c]) for c in cols],
    )


def _dec(value: Any) -> Decimal | None:
    """Decode a stored TEXT value back to Decimal; ``None`` stays ``None``."""
    return None if value is None else Decimal(value)


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


# --------------------------------------------------------------------------
# current_positions (materialized per-symbol position)
# --------------------------------------------------------------------------


def upsert_current_position(
    conn: sqlite3.Connection,
    run_id: str,
    position: PositionState,
    *,
    updated_at: datetime | None = None,
) -> None:
    """Upsert a symbol's materialized position (size / entry / realized PnL).

    Deliberately does **not** touch ``stop_loss_price`` / ``take_profit_price``:
    they default NULL on first insert and are left untouched on conflict, so a
    position-changing fill preserves any active protection rather than wiping it.
    The SL/TP lifecycle (write *and* read) lands together in PR 3, which owns
    protection management (execution §2–§4).
    """
    conn.execute(
        """
        INSERT INTO current_positions
            (run_id, symbol, size, entry_price, realized_pnl, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, symbol) DO UPDATE SET
            size = excluded.size,
            entry_price = excluded.entry_price,
            realized_pnl = excluded.realized_pnl,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            position.coin,
            str(position.size),
            None if position.entry_price is None else str(position.entry_price),
            str(position.realized_pnl),
            _iso_utc(updated_at or datetime.now(timezone.utc)),
        ),
    )


def _row_to_position(row: sqlite3.Row) -> PositionState:
    return PositionState(
        coin=row["symbol"],
        size=Decimal(row["size"]),
        entry_price=_dec(row["entry_price"]),
        realized_pnl=Decimal(row["realized_pnl"]),
    )


def get_current_position(
    conn: sqlite3.Connection, run_id: str, symbol: str
) -> PositionState | None:
    row = conn.execute(
        "SELECT * FROM current_positions WHERE run_id = ? AND symbol = ?",
        (run_id, symbol),
    ).fetchone()
    return None if row is None else _row_to_position(row)


def get_all_current_positions(conn: sqlite3.Connection, run_id: str) -> list[PositionState]:
    rows = conn.execute(
        "SELECT * FROM current_positions WHERE run_id = ? ORDER BY symbol", (run_id,)
    ).fetchall()
    return [_row_to_position(r) for r in rows]


# --------------------------------------------------------------------------
# fills
# --------------------------------------------------------------------------


def insert_fill(
    conn: sqlite3.Connection,
    *,
    fill_id: str,
    mode: str,
    run_id: str,
    order_id: str,
    symbol: str,
    side: str,
    fill_qty: Decimal,
    fill_price: Decimal,
    fill_notional: Decimal,
    fee: Decimal,
    fee_rate: Decimal,
    realized_pnl_delta: Decimal,
    liquidity_type: str = "simulated",
    slice_id: str | None = None,
    plan_id: str | None = None,
    flip_leg: str | None = None,
    slice_index: int | None = None,
    exchange_fill_id: str | None = None,
    exchange_order_id: str | None = None,
    fill_reason: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Insert one fill. Raises ``sqlite3.IntegrityError`` on a duplicate ``slice_id``.

    The UNIQUE constraint on ``slice_id`` is the exactly-once guard: a retried or
    restarted TWAP slice re-derives the same id (see :mod:`.ids`) and the insert
    is rejected instead of double-posting. ``paper_market`` / SL / TP fills pass
    ``slice_id=None`` (NULLs are distinct under UNIQUE).
    """
    _insert(
        conn,
        "fills",
        {
            "fill_id": fill_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "mode": mode,
            "run_id": run_id,
            "order_id": order_id,
            "slice_id": slice_id,
            "plan_id": plan_id,
            "flip_leg": flip_leg,
            "slice_index": slice_index,
            "exchange_fill_id": exchange_fill_id,
            "exchange_order_id": exchange_order_id,
            "symbol": symbol,
            "side": side,
            "fill_qty": fill_qty,
            "fill_price": fill_price,
            "fill_notional": fill_notional,
            "fee": fee,
            "fee_rate": fee_rate,
            "realized_pnl_delta": realized_pnl_delta,
            "liquidity_type": liquidity_type,
            "fill_reason": fill_reason,
        },
    )


def iter_fills(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """All fills for a run in insertion (chronological) order — the replay input."""
    return conn.execute("SELECT * FROM fills WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()


# --------------------------------------------------------------------------
# funding_events
# --------------------------------------------------------------------------


def insert_funding_event(
    conn: sqlite3.Connection,
    *,
    funding_event_id: str,
    mode: str,
    run_id: str,
    symbol: str,
    funding_timestamp: datetime,
    position_size: Decimal,
    status: str,
    mark_price: Decimal | None = None,
    signed_position_notional: Decimal | None = None,
    funding_rate: Decimal | None = None,
    funding_pnl: Decimal | None = None,
    source: str | None = None,
    recorded_at: datetime | None = None,
) -> None:
    """Insert a funding event. Raises ``sqlite3.IntegrityError`` on a duplicate key.

    The UNIQUE ``(run_id, symbol, funding_timestamp)`` (and the deterministic
    ``funding_event_id``) make funding exactly-once: a retry inserting the same
    settlement is rejected. A ``pending`` row (rate unavailable) is later moved to
    ``posted`` via :func:`set_funding_status`, which is where the wallet posting
    happens — never at insert.
    """
    _insert(
        conn,
        "funding_events",
        {
            "funding_event_id": funding_event_id,
            "recorded_at": recorded_at or datetime.now(timezone.utc),
            "funding_timestamp": funding_timestamp,
            "mode": mode,
            "run_id": run_id,
            "symbol": symbol,
            "position_size": position_size,
            "mark_price": mark_price,
            "signed_position_notional": signed_position_notional,
            "funding_rate": funding_rate,
            "funding_pnl": funding_pnl,
            "status": status,
            "source": source,
        },
    )


def get_funding_event(conn: sqlite3.Connection, funding_event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM funding_events WHERE funding_event_id = ?", (funding_event_id,)
    ).fetchone()


def set_funding_status(
    conn: sqlite3.Connection,
    funding_event_id: str,
    *,
    status: str,
    funding_rate: Decimal | None = None,
    funding_pnl: Decimal | None = None,
    signed_position_notional: Decimal | None = None,
    mark_price: Decimal | None = None,
    source: str | None = None,
) -> None:
    """Update a funding event's status and (on posting) its computed amounts.

    Only the provided fields are written; ``None`` leaves the stored value
    untouched, so moving ``pending -> posted`` can fill in the rate/pnl learned
    from the funding-history backfill without clobbering the rest.
    """
    sets = ["status = ?"]
    params: list[Any] = [status]
    for col, val in (
        ("funding_rate", funding_rate),
        ("funding_pnl", funding_pnl),
        ("signed_position_notional", signed_position_notional),
        ("mark_price", mark_price),
        ("source", source),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(_encode(val))
    params.append(funding_event_id)
    conn.execute(
        f"UPDATE funding_events SET {', '.join(sets)} WHERE funding_event_id = ?",
        params,
    )


def iter_funding_events(
    conn: sqlite3.Connection, run_id: str, *, status: str | None = None
) -> list[sqlite3.Row]:
    """Funding events for a run, optionally filtered by status, in insertion order."""
    if status is None:
        return conn.execute(
            "SELECT * FROM funding_events WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM funding_events WHERE run_id = ? AND status = ? ORDER BY rowid",
        (run_id, status),
    ).fetchall()


# --------------------------------------------------------------------------
# snapshots / ai / orders / attempts / plans / scheduler — thin typed inserts
# --------------------------------------------------------------------------


def insert_account_snapshot(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "account_snapshots", fields)


def insert_position_snapshot(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "position_snapshots", fields)


def insert_ai_input(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "ai_inputs", fields)


def insert_ai_output(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "ai_outputs", fields)


def insert_order(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "orders", fields)


def insert_decision_attempt(conn: sqlite3.Connection, **fields: Any) -> None:
    """Insert a decision attempt. Raises on a duplicate ``(run_id, scheduled_at)``.

    The deterministic ``decision_attempt_id`` plus the UNIQUE ``(run_id,
    scheduled_at)`` keep one scheduled cycle to one attempt row across restarts
    (phase2-spec §3.1).
    """
    _insert(conn, "decision_attempts", fields)


def insert_execution_plan(conn: sqlite3.Connection, **fields: Any) -> None:
    _insert(conn, "execution_plans", fields)


def upsert_scheduler_state(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    last_decision_at: datetime | None = None,
    next_decision_at: datetime | None = None,
    last_input_id: str | None = None,
    last_output_id: str | None = None,
    current_attempt_id: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO scheduler_state
            (run_id, last_decision_at, next_decision_at, last_input_id,
             last_output_id, current_attempt_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            last_decision_at = excluded.last_decision_at,
            next_decision_at = excluded.next_decision_at,
            last_input_id = excluded.last_input_id,
            last_output_id = excluded.last_output_id,
            current_attempt_id = excluded.current_attempt_id,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            _encode(last_decision_at),
            _encode(next_decision_at),
            last_input_id,
            last_output_id,
            current_attempt_id,
            _iso_utc(updated_at or datetime.now(timezone.utc)),
        ),
    )


def get_scheduler_state(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scheduler_state WHERE run_id = ?", (run_id,)).fetchone()
