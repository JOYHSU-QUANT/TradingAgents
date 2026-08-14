"""The ``funding_events`` table — funding settlements and their stored identities."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

from ...domains.perp.enum_guard import check_enum
from ..models import DECIMAL_CONTEXT
from ._base import _dec, _encode, _insert, _iso_utc
from ._vocab import _FUNDING_SOURCES, _FUNDING_STATUSES, _MODES

__all__ = ["get_funding_event", "insert_funding_event", "iter_funding_events", "set_funding_status"]


def _check_funding_identities(
    *,
    position_size: Decimal | None,
    mark_price: Decimal | None,
    signed_position_notional: Decimal | None,
    funding_rate: Decimal | None,
    funding_pnl: Decimal | None,
) -> None:
    """Reject a funding row whose stored derived columns contradict their inputs.

    Only checks identities whose operands are all present — a ``pending`` row
    legitimately carries no notional/rate/pnl yet, and ``set_funding_status``
    passes ``None`` for anything it isn't updating.
    """
    with localcontext(DECIMAL_CONTEXT):  # round exactly like the producing math
        if None not in (position_size, mark_price, signed_position_notional):
            expected = position_size * mark_price
            if signed_position_notional != expected:
                raise ValueError(
                    f"signed_position_notional {signed_position_notional} != "
                    f"position_size * mark_price {expected}"
                )
        if None not in (signed_position_notional, funding_rate, funding_pnl):
            expected = -signed_position_notional * funding_rate
            if funding_pnl != expected:
                raise ValueError(
                    f"funding_pnl {funding_pnl} != "
                    f"-signed_position_notional * funding_rate {expected}"
                )


def _check_posted_settlement_math(
    *,
    mark_price: Decimal | None,
    signed_position_notional: Decimal | None,
    funding_rate: Decimal | None,
    funding_pnl: Decimal | None,
) -> None:
    """The one definition of "a posted funding row is complete".

    ``posted`` means the wallet moved, so all four settlement fields must be
    present. Shared by both write paths (direct posted insert and the
    ``pending -> posted`` transition) so the two can never drift.
    """
    missing = sorted(
        name
        for name, value in {
            "mark_price": mark_price,
            "signed_position_notional": signed_position_notional,
            "funding_rate": funding_rate,
            "funding_pnl": funding_pnl,
        }.items()
        if value is None
    )
    if missing:
        raise ValueError(f"a posted funding event requires its settlement math; missing {missing}")


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

    ``signed_position_notional`` / ``funding_pnl`` are stored derived values and,
    when supplied, must match ``position_size * mark_price`` and
    ``-signed_position_notional * funding_rate`` (same rationale as
    :func:`insert_fill`: replay ignores the stored columns, so a desync would go
    undetected).

    The status implies which fields exist — enforced here so a row can never
    contradict its own status: ``posted`` means the wallet moved, so the full
    settlement math (mark / notional / rate / pnl) must be present; ``pending``
    means only the basis was captured, so the mark is required (the backfill
    must never fabricate one) and the not-yet-learned rate/pnl/notional must be
    absent. A ``posted`` row missing its math would silently satisfy
    ``record_funding``'s already-posted short-circuit and drop the settlement.
    """
    check_enum(status, _FUNDING_STATUSES, name="status")
    check_enum(mode, _MODES, name="mode")
    if source is not None:
        check_enum(source, _FUNDING_SOURCES, name="source")
    if status == "posted":
        _check_posted_settlement_math(
            mark_price=mark_price,
            signed_position_notional=signed_position_notional,
            funding_rate=funding_rate,
            funding_pnl=funding_pnl,
        )
    else:  # pending
        if mark_price is None:
            raise ValueError("a pending funding event must record its settlement mark_price")
        premature = sorted(
            name
            for name, value in {
                "signed_position_notional": signed_position_notional,
                "funding_rate": funding_rate,
                "funding_pnl": funding_pnl,
            }.items()
            if value is not None
        )
        if premature:
            raise ValueError(f"a pending funding event cannot already carry {premature}")
    _check_funding_identities(
        position_size=position_size,
        mark_price=mark_price,
        signed_position_notional=signed_position_notional,
        funding_rate=funding_rate,
        funding_pnl=funding_pnl,
    )
    stamped = recorded_at or datetime.now(timezone.utc)
    _insert(
        conn,
        "funding_events",
        {
            "funding_event_id": funding_event_id,
            "recorded_at": stamped,
            "updated_at": stamped,
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
    updated_at: datetime | None = None,
) -> None:
    """Post a pending funding event — the one legal transition (``pending -> posted``).

    Only the provided fields are written; ``None`` leaves the stored value
    untouched, so posting can fill in the rate/pnl learned from the
    funding-history backfill without clobbering the rest. The state machine is
    enforced here, not left to callers: the row must exist and still be
    ``pending`` (a ``posted`` event is immutable — re-posting or reverting it
    would let the same settlement move the wallet twice, defeating the
    exactly-once guarantee ``record_funding`` builds on this function), and the
    row must come out of the update with its settlement math complete and
    consistent with the *stored* basis (``position_size``, and the stored mark —
    the settlement mark is immutable, so a re-supplied ``mark_price`` must match
    the stored one and can never override it). ``updated_at`` stamps when the transition
    actually happened — ``recorded_at`` keeps the pending-insert time, so a
    live posting and an hours-later backfill stay distinguishable.
    """
    check_enum(status, _FUNDING_STATUSES, name="status")
    if source is not None:
        check_enum(source, _FUNDING_SOURCES, name="source")
    # Caller-supplied identities first (pure, no row needed) so an internally
    # inconsistent update is rejected identically whether or not the row exists.
    _check_funding_identities(
        position_size=None,
        mark_price=mark_price,
        signed_position_notional=signed_position_notional,
        funding_rate=funding_rate,
        funding_pnl=funding_pnl,
    )
    row = get_funding_event(conn, funding_event_id)
    if row is None:
        raise ValueError(f"funding event {funding_event_id!r} does not exist")
    if row["status"] == "posted":
        raise ValueError(f"funding event {funding_event_id!r} is already posted and immutable")
    if status != "posted":
        raise ValueError("set_funding_status only performs the pending -> posted transition")
    # The settlement mark is immutable once record_funding fixed it on the pending
    # row (a pending event requires a mark). A caller may re-supply mark_price, but
    # only matching the stored basis — never override it after the fact, or a
    # position/price change between pending and backfill would silently rewrite
    # settlement history (loop-1 decision #3: backfill uses ONLY the stored
    # pending-row basis). record_funding already never supplies a divergent mark;
    # this makes the guarantee live at the boundary, not just in that convention.
    stored_mark = _dec(row["mark_price"])
    if mark_price is not None and mark_price != stored_mark:
        raise ValueError(
            f"funding event {funding_event_id!r}: supplied mark_price {mark_price} "
            f"differs from the immutable stored settlement mark {stored_mark}"
        )
    effective_mark = stored_mark if mark_price is None else mark_price
    _check_posted_settlement_math(
        mark_price=effective_mark,
        signed_position_notional=signed_position_notional,
        funding_rate=funding_rate,
        funding_pnl=funding_pnl,
    )
    # Re-check with the stored basis folded in: the notional must be the stored
    # size at the effective mark, not whatever basis the caller had in hand.
    _check_funding_identities(
        position_size=Decimal(row["position_size"]),
        mark_price=effective_mark,
        signed_position_notional=signed_position_notional,
        funding_rate=funding_rate,
        funding_pnl=funding_pnl,
    )
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, _iso_utc(updated_at or datetime.now(timezone.utc))]
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
