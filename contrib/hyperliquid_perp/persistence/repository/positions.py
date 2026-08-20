"""The ``current_positions`` table — the materialized per-symbol position."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ..models import PositionState
from ._base import _dec_or_none, _encode, _iso_utc

__all__ = [
    "get_all_current_positions",
    "get_current_position",
    "get_position_protection",
    "set_position_liquidation_price",
    "set_position_protection",
    "upsert_current_position",
]


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
    position-changing fill preserves any active protection rather than wiping
    it. The SL/TP lifecycle (write *and* read) lands together in PR 3, which
    owns protection management (execution §2–§4).

    ``exchange_liquidation_price`` is treated differently, and the difference is
    load-bearing: it describes a position's DIRECTION, not merely its symbol.
    Carried across a flip (or across a flat and back), it hands the live SL band
    an estimate on the wrong side of the new entry — which
    ``stops.stop_loss_decision`` reads as ``liquidation_too_close`` and answers
    CLOSE_NOW, i.e. a §17.2 emergency close of a position that was never in
    danger, followed by the §13.5 MANUAL safe-mode latch a human must clear. So
    the column survives only a SAME-DIRECTION update; a sign change or a
    flatten clears it and the reconciler's mirror (its one writer,
    :func:`set_position_liquidation_price`) re-establishes it next pass.
    """
    prior = conn.execute(
        "SELECT size FROM current_positions WHERE run_id = ? AND symbol = ?",
        (run_id, position.coin),
    ).fetchone()
    # Decided in Python rather than SQL: ``size`` is stored as TEXT (Decimal
    # round-trip), so a SQLite-side sign test would compare strings.
    prior_size = Decimal(0) if prior is None else Decimal(prior["size"])
    keeps_liquidation = (
        position.size != 0 and prior_size != 0 and (position.size > 0) == (prior_size > 0)
    )
    conn.execute(
        """
        INSERT INTO current_positions
            (run_id, symbol, size, entry_price, realized_pnl, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, symbol) DO UPDATE SET
            size = excluded.size,
            entry_price = excluded.entry_price,
            realized_pnl = excluded.realized_pnl,
            updated_at = excluded.updated_at,
            exchange_liquidation_price = CASE
                WHEN ? THEN exchange_liquidation_price ELSE NULL END
        """,
        (
            run_id,
            position.coin,
            str(position.size),
            None if position.entry_price is None else str(position.entry_price),
            str(position.realized_pnl),
            _iso_utc(updated_at or datetime.now(timezone.utc)),
            1 if keeps_liquidation else 0,
        ),
    )


def _row_to_position(row: sqlite3.Row) -> PositionState:
    return PositionState(
        coin=row["symbol"],
        size=Decimal(row["size"]),
        entry_price=_dec_or_none(row["entry_price"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        liquidation_price=_dec_or_none(row["exchange_liquidation_price"]),
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


def get_position_protection(
    conn: sqlite3.Connection, run_id: str, symbol: str
) -> tuple[Decimal | None, Decimal | None] | None:
    """The persisted ``(stop_loss_price, take_profit_price)`` of a position row.

    Returns ``None`` when no ``current_positions`` row exists at all (as opposed
    to a row whose protection is cleared, which returns ``(None, None)``). This is
    the read side of :func:`set_position_protection` — the engine hydrates its
    live trigger state from here when constructed over an existing run, so a
    restart never silently forgets an active SL/TP (execution §2).
    """
    row = conn.execute(
        "SELECT stop_loss_price, take_profit_price FROM current_positions"
        " WHERE run_id = ? AND symbol = ?",
        (run_id, symbol),
    ).fetchone()
    if row is None:
        return None
    return _dec_or_none(row["stop_loss_price"]), _dec_or_none(row["take_profit_price"])


def set_position_protection(
    conn: sqlite3.Connection,
    run_id: str,
    symbol: str,
    *,
    stop_loss_price: Decimal | None,
    take_profit_price: Decimal | None,
    updated_at: datetime | None = None,
) -> None:
    """Set (or clear, with ``None``) a position's SL/TP prices (execution §2–§4).

    ``upsert_current_position`` deliberately leaves these columns alone on a fill
    (it preserves active protection); this is the one writer that moves them, so
    the protection lifecycle stays explicit. The position row must already exist.
    """
    cur = conn.execute(
        """
        UPDATE current_positions
        SET stop_loss_price = ?, take_profit_price = ?, updated_at = ?
        WHERE run_id = ? AND symbol = ?
        """,
        (
            _encode(stop_loss_price),
            _encode(take_profit_price),
            _iso_utc(updated_at or datetime.now(timezone.utc)),
            run_id,
            symbol,
        ),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"no current_positions row for run {run_id!r} symbol {symbol!r} to protect"
        )


def set_position_liquidation_price(
    conn: sqlite3.Connection,
    run_id: str,
    symbol: str,
    value: Decimal | None,
    *,
    updated_at: datetime | None = None,
) -> None:
    """Mirror (or clear, with ``None``) the exchange-reported liquidation price.

    The live reconciler is the one writer: each pass it copies the clearinghouse
    ``liquidationPx`` for the run's coin onto the position row (``None`` when the
    exchange reports flat, or when the two views disagree on direction, so an
    estimate is only ever attributed to the position it actually describes).
    ``upsert_current_position`` clears the column on a direction change or a
    flatten — the second half of that same invariant, for the ticks between
    reconciler passes. Unlike :func:`set_position_protection`, a missing row is a
    NO-OP, not an error: the exchange can report a position the local books do
    not have yet — that mismatch is the reconciler's own §12.3 case lane, not
    this writer's job.
    """
    conn.execute(
        """
        UPDATE current_positions
        SET exchange_liquidation_price = ?, updated_at = ?
        WHERE run_id = ? AND symbol = ?
        """,
        (
            _encode(value),
            _iso_utc(updated_at or datetime.now(timezone.utc)),
            run_id,
            symbol,
        ),
    )
