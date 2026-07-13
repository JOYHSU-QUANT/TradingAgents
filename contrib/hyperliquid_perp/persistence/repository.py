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
from decimal import Decimal, localcontext
from typing import Any

from ..domains.perp.enum_guard import check_enum
from .cloid import LIVE_ORDER_ROLES
from .ids import _canonical_instant
from .models import DECIMAL_CONTEXT, AccountLedger, PositionState, Side

__all__ = [
    "KILL_SWITCH_EVENT_TYPES",
    "find_in_progress_attempt",
    "get_all_current_positions",
    "get_cloid_by_hex",
    "get_cloid_by_logical",
    "get_current_account_state",
    "get_current_position",
    "get_decision_attempt",
    "get_execution_plan",
    "get_funding_event",
    "get_live_order_attempt",
    "get_order",
    "get_order_by_cloid_hex",
    "get_position_protection",
    "get_run",
    "get_run_seed_positions",
    "get_scheduler_state",
    "insert_account_snapshot",
    "insert_ai_input",
    "insert_ai_output",
    "insert_cloid_mapping",
    "insert_decision_attempt",
    "insert_execution_plan",
    "insert_fill",
    "insert_funding_event",
    "insert_kill_switch_event",
    "insert_live_order_attempt",
    "insert_order",
    "insert_position_snapshot",
    "insert_run",
    "insert_run_seed_position",
    "iter_execution_plans",
    "iter_fills",
    "iter_funding_events",
    "iter_kill_switch_events",
    "iter_live_order_attempts",
    "iter_orders",
    "max_engine_seq",
    "next_live_attempt_index",
    "set_funding_status",
    "set_position_protection",
    "update_decision_attempt",
    "update_execution_plan",
    "update_live_order_attempt",
    "update_order",
    "upsert_current_account_state",
    "upsert_current_position",
    "upsert_scheduler_state",
]

# Enumerable storage values validated at the write boundary (fail loud on a typo
# rather than persisting it). ``Side`` carries the fill/order direction; these
# small sets cover the columns that don't warrant a full enum yet (their typed
# writers land in PR3).
_MODES = frozenset({"paper", "live"})
_LIQUIDITY_TYPES = frozenset({"maker", "taker", "simulated"})
_FLIP_LEGS = frozenset({"open", "close"})
_FUNDING_STATUSES = frozenset({"pending", "posted"})
# phase2-data §10 funding provenance vocabulary.
_FUNDING_SOURCES = frozenset(
    {"live_public_data", "funding_history_backfill", "exchange_user_funding"}
)
# The §6.2 retry vocabulary plus the scheduler's restart-interrupted marker.
_ERROR_TYPES = frozenset({"timeout", "rate_limit", "connection", "server_error", "interrupted"})
# scheduler_state CSV-export breadcrumb states (schema v3).
_EXPORT_STATUSES = frozenset({"ok", "failed"})
# Replay breadcrumb vocabulary: "mismatch" = replay ran and contradicted the
# materialized state; "failed" = replay itself raised (books unverifiable).
_REPLAY_STATUSES = frozenset({"ok", "mismatch", "failed"})
# Config-drift-on-resume breadcrumb states (schema v5): "drift" = the resumed
# run's risk/decision/paper_trading blocks differ from the genesis record.
_CONFIG_DRIFT_STATUSES = frozenset({"ok", "drift"})
# live_order_attempts vocabulary (schema v6, phase3-spec §8.3): which exchange
# action the round-trip was, and where it ended. "submitted" is written BEFORE
# the network call — a row stuck there means the outcome is unknown and the
# duplicate-retry protocol must query the exchange before resending.
_LIVE_ATTEMPT_ACTIONS = frozenset({"place", "cancel", "cancel_by_cloid"})
_LIVE_ATTEMPT_STATUSES = frozenset({"submitted", "acknowledged", "rejected", "duplicate", "failed"})
# §18.5: the complete kill-switch event vocabulary. Public: PR 6's acceptance
# metrics read them. The PR 2 kill switch manager writes seven of the nine;
# kill_switch_cancel_triggered / emergency_kill_switch_triggered belong to the
# §18.3 emergency-trigger state machine, which is NOT in PR 2 — no rows with
# those types exist yet.
KILL_SWITCH_EVENT_TYPES = frozenset(
    {
        "kill_switch_armed",
        "kill_switch_refreshed",
        "kill_switch_refresh_failed",
        "kill_switch_cancel_triggered",
        "kill_switch_disarmed",
        "kill_switch_disarm_failed",
        "emergency_kill_switch_triggered",
        "shutdown_cancel_orders_started",
        "shutdown_cancel_orders_completed",
    }
)


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


def _iso_utc(value: datetime) -> str:
    """Canonical ISO-8601 UTC string for a datetime; rejects naive datetimes.

    All stored timestamps must be UTC and comparable as strings: the funding
    dedup key ``(run_id, symbol, funding_timestamp)`` and the deterministic
    ``funding_event_id`` both derive from the stored/serialized timestamp, so a
    naive-vs-aware or non-UTC-offset representation of the *same* instant would
    produce different keys and let a settlement post twice. Delegates to the one
    canonicalization rule in :mod:`.ids` so storage and id derivation can never
    drift apart.
    """
    return _canonical_instant(value, name="datetime value")


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


class _Unset:
    """Sentinel type: a keyword the caller did not supply (vs an explicit ``None``)."""


# Defined near the top so every patch-style writer below (orders, execution
# plans, scheduler state) can use it as a default argument value — defaults are
# bound at function-definition (import) time, so the sentinel must exist first.
_UNSET = _Unset()


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
    return _dec(row["stop_loss_price"]), _dec(row["take_profit_price"])


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

    ``fill_notional`` and ``fee`` are stored derived values and must equal
    ``abs(fill_qty * fill_price)`` and ``fill_notional * fee_rate``: replay
    recomputes them from the raw fields and ignores these columns, so a desynced
    value would never surface in the replay consistency check — reject it here
    instead. (``realized_pnl_delta`` depends on prior position state and cannot
    be re-derived row-locally; it stays the caller's responsibility.)
    """
    side = Side.parse(side)
    check_enum(liquidity_type, _LIQUIDITY_TYPES, name="liquidity_type")
    if flip_leg is not None:
        check_enum(flip_leg, _FLIP_LEGS, name="flip_leg")
    check_enum(mode, _MODES, name="mode")
    with localcontext(DECIMAL_CONTEXT):  # round exactly like the producing math
        expected_notional = abs(fill_qty * fill_price)
        if fill_notional != expected_notional:
            raise ValueError(
                f"fill_notional {fill_notional} != |fill_qty * fill_price| {expected_notional}"
            )
        expected_fee = fill_notional * fee_rate
        if fee != expected_fee:
            raise ValueError(f"fee {fee} != fill_notional * fee_rate {expected_fee}")
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
            "side": side.value,
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


_ATTEMPT_STATUSES = frozenset({"in_progress", "completed", "api_failed", "invalid_output"})
# The live/terminal split of each status vocabulary, defined HERE next to the
# canonical sets so a future status addition updates both in one place — a
# hand-copied subset in a consumer (reconcile's cancel sweep, the validator's
# cycle counting) would silently miss the new member (check_enum can validate
# membership, never completeness).
TERMINAL_ATTEMPT_STATUSES: tuple[str, ...] = ("completed", "api_failed", "invalid_output")
LIVE_PLAN_STATUSES: tuple[str, ...] = ("active", "paused_market_data")
# "submitted" belongs here: a pre-ack live order is non-terminal — it may be
# resting on the exchange — so the restart cancel sweep and PR 4's
# reconciliation must see it, never skip it as already-settled.
LIVE_ORDER_STATUSES: tuple[str, ...] = (
    "pending_market_data",
    "submitted",
    "open",
    "partially_filled",
)


def insert_decision_attempt(conn: sqlite3.Connection, **fields: Any) -> None:
    """Insert a decision attempt. Raises on a duplicate ``(run_id, scheduled_at)``.

    The deterministic ``decision_attempt_id`` plus the UNIQUE ``(run_id,
    scheduled_at)`` keep one scheduled cycle to one attempt row across restarts
    (phase2-spec §3.1).
    """
    if "status" in fields:
        check_enum(fields["status"], _ATTEMPT_STATUSES, name="status")
    if fields.get("error_type") is not None:
        check_enum(fields["error_type"], _ERROR_TYPES, name="error_type")
    _insert(conn, "decision_attempts", fields)


def get_decision_attempt(conn: sqlite3.Connection, decision_attempt_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM decision_attempts WHERE decision_attempt_id = ?",
        (decision_attempt_id,),
    ).fetchone()


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
        check_enum(error_type, _ERROR_TYPES, name="error_type")
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


# --------------------------------------------------------------------------
# orders (phase2-data §8) — typed insert + patch update, first used by PR3
# --------------------------------------------------------------------------

# §8.1: the live roles are a superset of the four Phase 2 paper roles (close /
# emergency_close / cleanup_cancel are live-only vocabulary). One shared
# frozenset (defined next to the cloid derivation that also stamps roles) so
# the id layer and the write boundary can never accept different vocabularies.
_ORDER_ROLES = LIVE_ORDER_ROLES
# ``ioc_limit`` is the live wire type: every §9 slice (and §9.4 close /
# emergency close) is an IOC limit order; the four paper_* / trigger types are
# the Phase 2 simulation vocabulary.
_ORDER_TYPES = frozenset(
    {"paper_market", "paper_twap_slice", "stop_market", "take_market", "ioc_limit"}
)
# "submitted" is the live-only pre-ack state (phase3-spec §8.3): the order row
# is written before the network call and patched once the exchange answers.
_ORDER_STATUSES = frozenset(
    {
        "pending_market_data",
        "submitted",
        "open",
        "partially_filled",
        "filled",
        "canceled",
        "rejected",
    }
)
# Execution-plan lifecycle: ``active`` / ``paused_market_data`` are live; the rest
# are terminal (execution §1.1 / §1.3 / §4.1).
_PLAN_STATUSES = frozenset(
    {
        "active",
        "paused_market_data",
        "completed",
        "canceled",
        "canceled_restart",
        "expired",
        "failed",
        "flip_incomplete",
        "rejected",
        "residual",
    }
)

# The live/terminal splits above must stay subsets of their canonical sets —
# checked at import so a renamed status can't leave a stale split member behind.
for _split, _whole, _name in (
    (TERMINAL_ATTEMPT_STATUSES, _ATTEMPT_STATUSES, "TERMINAL_ATTEMPT_STATUSES"),
    (LIVE_PLAN_STATUSES, _PLAN_STATUSES, "LIVE_PLAN_STATUSES"),
    (LIVE_ORDER_STATUSES, _ORDER_STATUSES, "LIVE_ORDER_STATUSES"),
):
    _extra = set(_split) - _whole
    if _extra:
        raise ValueError(f"{_name} contains statuses outside its vocabulary: {sorted(_extra)}")
del _split, _whole, _name, _extra


def insert_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    mode: str,
    run_id: str,
    symbol: str,
    order_role: str,
    side: str,
    order_type: str,
    qty: Decimal,
    status: str,
    output_id: str | None = None,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
    parent_order_id: str | None = None,
    flip_plan_id: str | None = None,
    flip_leg: str | None = None,
    price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    filled_qty: Decimal = Decimal(0),
    remaining_qty: Decimal | None = None,
    status_reason: str | None = None,
    reduce_only: bool = False,
    active_from: datetime | None = None,
    timestamp: datetime | None = None,
    updated_at: datetime | None = None,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    exchange_status: str | None = None,
    exchange_raw_status: str | None = None,
    submitted_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    canceled_at: datetime | None = None,
    cancel_reason: str | None = None,
    is_bot_owned: bool | None = None,
    raw_exchange_payload_path: str | None = None,
) -> None:
    """Insert one order row (phase2-data §8; live columns phase3-spec §16.1).

    Enums validated at the write boundary. A live order writes the cloid pair
    (``client_order_id`` is deprecated — old paper rows only, no new writers);
    the pair travels together or not at all so no order can be queryable on the
    wire (§8.3 rule 7 uses cloid_hex) but unreadable in the audit trail.
    """
    check_enum(mode, _MODES, name="mode")
    check_enum(order_role, _ORDER_ROLES, name="order_role")
    check_enum(order_type, _ORDER_TYPES, name="type")
    check_enum(status, _ORDER_STATUSES, name="status")
    if flip_leg is not None:
        check_enum(flip_leg, _FLIP_LEGS, name="flip_leg")
    if (cloid_logical is None) != (cloid_hex is None):
        raise ValueError(
            "cloid_logical and cloid_hex must be provided together (the §8.2 "
            "two-layer id is one unit)"
        )
    now = timestamp or datetime.now(timezone.utc)
    _insert(
        conn,
        "orders",
        {
            "order_id": order_id,
            "timestamp": now,
            "mode": mode,
            "run_id": run_id,
            "output_id": output_id,
            "exchange_order_id": exchange_order_id,
            "client_order_id": client_order_id,
            "parent_order_id": parent_order_id,
            "flip_plan_id": flip_plan_id,
            "flip_leg": flip_leg,
            "symbol": symbol,
            "order_role": order_role,
            "side": Side.parse(side).value,
            "type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "qty": qty,
            "filled_qty": filled_qty,
            "remaining_qty": remaining_qty,
            "status": status,
            "status_reason": status_reason,
            "reduce_only": reduce_only,
            "active_from": active_from,
            "updated_at": updated_at or now,
            "cloid_logical": cloid_logical,
            "cloid_hex": cloid_hex,
            "exchange_status": exchange_status,
            "exchange_raw_status": exchange_raw_status,
            "submitted_at": submitted_at,
            "acknowledged_at": acknowledged_at,
            "canceled_at": canceled_at,
            "cancel_reason": cancel_reason,
            "is_bot_owned": is_bot_owned,
            "raw_exchange_payload_path": raw_exchange_payload_path,
        },
    )


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()


def get_order_by_cloid_hex(conn: sqlite3.Connection, cloid_hex: str) -> sqlite3.Row | None:
    """The local order row an exchange-echoed cloid belongs to (§19.3 back-link).

    cloid_hex is unique per logical order by construction (one registry row per
    hex, one orders row per logical order), so at most one row can match; fail
    loud if the store ever contradicts that instead of picking one silently.
    """
    rows = conn.execute("SELECT * FROM orders WHERE cloid_hex = ?", (cloid_hex,)).fetchall()
    if len(rows) > 1:
        ids = ", ".join(r["order_id"] for r in rows)
        raise ValueError(f"cloid_hex {cloid_hex!r} maps to {len(rows)} orders ({ids})")
    return rows[0] if rows else None


def max_engine_seq(conn: sqlite3.Connection, run_id: str) -> int:
    """The highest ``<run_id>:<tag>:<n>`` suffix already persisted for a run.

    The paper engine mints its order/plan/flip ids from a monotonic in-memory
    counter; an engine constructed over an existing run must resume *above* the
    persisted maximum or its first insert dies on (at best) a PRIMARY KEY
    collision. Scans ``orders.order_id``, ``execution_plans.plan_id`` and the
    ``flip_plan_id`` columns; ids not shaped like the engine's (no numeric tail
    after the run prefix) are ignored. Returns 0 for a fresh run.
    """
    prefix = f"{run_id}:"
    best = 0
    for sql in (
        "SELECT order_id FROM orders WHERE run_id = ?",
        "SELECT flip_plan_id FROM orders WHERE run_id = ? AND flip_plan_id IS NOT NULL",
        "SELECT plan_id FROM execution_plans WHERE run_id = ?",
        "SELECT flip_plan_id FROM execution_plans WHERE run_id = ? AND flip_plan_id IS NOT NULL",
    ):
        for (ident,) in conn.execute(sql, (run_id,)):
            if not ident.startswith(prefix):
                continue
            tail = ident.rsplit(":", 1)[-1]
            if tail.isdigit():
                best = max(best, int(tail))
    return best


def update_order(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    filled_qty: Decimal | _Unset = _UNSET,
    remaining_qty: Decimal | None | _Unset = _UNSET,
    status: str | _Unset = _UNSET,
    status_reason: str | None | _Unset = _UNSET,
    exchange_order_id: str | None | _Unset = _UNSET,
    exchange_status: str | None | _Unset = _UNSET,
    exchange_raw_status: str | None | _Unset = _UNSET,
    submitted_at: datetime | None | _Unset = _UNSET,
    acknowledged_at: datetime | None | _Unset = _UNSET,
    canceled_at: datetime | None | _Unset = _UNSET,
    cancel_reason: str | None | _Unset = _UNSET,
    is_bot_owned: bool | None | _Unset = _UNSET,
    raw_exchange_payload_path: str | None | _Unset = _UNSET,
    updated_at: datetime | None = None,
) -> None:
    """Patch-update an order's mutable columns; always stamp ``updated_at``.

    Only supplied columns change (an omitted keyword is left untouched, an explicit
    ``None`` clears — same convention as :func:`upsert_scheduler_state`). The row
    must exist (an order is always inserted before it is updated). The cloid pair
    is deliberately NOT patchable: it is identity, fixed at insert (§8.3 rule 6 —
    a logical order never changes cloid).
    """
    if not isinstance(status, _Unset):
        check_enum(status, _ORDER_STATUSES, name="status")
    if get_order(conn, order_id) is None:
        raise ValueError(f"order {order_id!r} does not exist")
    provided: dict[str, Any] = {}
    for col, val in (
        ("filled_qty", filled_qty),
        ("remaining_qty", remaining_qty),
        ("status", status),
        ("status_reason", status_reason),
        ("exchange_order_id", exchange_order_id),
        ("exchange_status", exchange_status),
        ("exchange_raw_status", exchange_raw_status),
        ("submitted_at", submitted_at),
        ("acknowledged_at", acknowledged_at),
        ("canceled_at", canceled_at),
        ("cancel_reason", cancel_reason),
        ("is_bot_owned", is_bot_owned),
        ("raw_exchange_payload_path", raw_exchange_payload_path),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    provided["updated_at"] = _iso_utc(updated_at or datetime.now(timezone.utc))
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE orders SET {assignments} WHERE order_id = ?",
        (*provided.values(), order_id),
    )


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


def iter_orders(
    conn: sqlite3.Connection, run_id: str, *, statuses: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """A run's orders in insertion order, optionally filtered by status."""
    if statuses is None:
        return conn.execute(
            "SELECT * FROM orders WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    for status in statuses:
        check_enum(status, _ORDER_STATUSES, name="status")
    placeholders = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM orders WHERE run_id = ? AND status IN ({placeholders}) ORDER BY rowid",
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
    updated_at: datetime | None = None,
) -> None:
    """Patch-style upsert: only the columns actually supplied change.

    An omitted keyword leaves the stored value untouched (NULL on a fresh row);
    an explicit ``None`` clears the column. Full-row replace semantics would
    force a caller advancing just ``next_decision_at`` to re-supply every other
    field — one forgotten keyword and the crash-recovery breadcrumbs
    (``last_input_id`` / ``current_attempt_id``) this table exists to keep
    would be silently NULLed. ``updated_at`` is always stamped.
    """
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


# --------------------------------------------------------------------------
# cloid_registry (phase3-spec §8.2 / §19.3) — the bot-owned lookup table
# --------------------------------------------------------------------------


def insert_cloid_mapping(
    conn: sqlite3.Connection,
    *,
    cloid_logical: str,
    cloid_hex: str,
    run_id: str,
    symbol: str,
    order_role: str,
    created_at: datetime | None = None,
) -> None:
    """Register one logical↔wire id pair; idempotent for the exact same pair.

    §8.3 rule 1: a retry reuses the same cloid, and the registry write happens
    before every send attempt — so re-registering an identical mapping is a
    no-op, while the same hex arriving with a DIFFERENT logical id (or vice
    versa) is a real corruption of the bot-owned lookup and fails loud.
    """
    check_enum(order_role, _ORDER_ROLES, name="order_role")
    existing = get_cloid_by_hex(conn, cloid_hex)
    if existing is not None:
        # A retry legitimately re-registers the exact same pair; the same hex
        # with a different logical id would re-point the §19.3 lookup.
        if existing["cloid_logical"] != cloid_logical:
            raise ValueError(
                f"cloid_registry conflict: ({cloid_logical!r}, {cloid_hex!r}) collides "
                f"with existing ({existing['cloid_logical']!r}, {existing['cloid_hex']!r})"
            )
        return
    try:
        _insert(
            conn,
            "cloid_registry",
            {
                "cloid_hex": cloid_hex,
                "cloid_logical": cloid_logical,
                "run_id": run_id,
                "symbol": symbol,
                "order_role": order_role,
                "created_at": created_at or datetime.now(timezone.utc),
            },
        )
    except sqlite3.IntegrityError:
        # The hex was free, so this hit the cloid_logical UNIQUE index: the
        # same logical id already maps to a DIFFERENT hex. Name the collision
        # instead of leaking the raw constraint error.
        row = get_cloid_by_logical(conn, cloid_logical)
        existing_pair = (
            "<row vanished mid-transaction>"
            if row is None
            else f"({row['cloid_logical']!r}, {row['cloid_hex']!r})"
        )
        raise ValueError(
            f"cloid_registry conflict: ({cloid_logical!r}, {cloid_hex!r}) collides "
            f"with existing {existing_pair}"
        ) from None


def get_cloid_by_hex(conn: sqlite3.Connection, cloid_hex: str) -> sqlite3.Row | None:
    """§19.3 reverse lookup: the one way to decide an exchange order is bot-owned."""
    return conn.execute("SELECT * FROM cloid_registry WHERE cloid_hex = ?", (cloid_hex,)).fetchone()


def get_cloid_by_logical(conn: sqlite3.Connection, cloid_logical: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cloid_registry WHERE cloid_logical = ?", (cloid_logical,)
    ).fetchone()


# --------------------------------------------------------------------------
# live_order_attempts (phase3-spec §8.3 / §16.5) — one row per exchange round-trip
# --------------------------------------------------------------------------


def insert_live_order_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    run_id: str,
    action: str,
    symbol: str,
    attempt_index: int,
    status: str = "submitted",
    order_id: str | None = None,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    exchange_order_id: str | None = None,
    side: str | None = None,
    qty: Decimal | None = None,
    price: Decimal | None = None,
    reduce_only: bool | None = None,
    order_role: str | None = None,
    requested_at: datetime | None = None,
) -> None:
    """Record an exchange round-trip BEFORE the network call (status 'submitted').

    A crash between this insert and the outcome patch leaves durable evidence
    that an order MAY exist on the exchange — the state §8.3's query-before-
    resend protocol resolves. The UNIQUE (cloid_hex, action, attempt_index)
    makes a retry a new row rather than an overwrite of that evidence.
    """
    check_enum(action, _LIVE_ATTEMPT_ACTIONS, name="action")
    check_enum(status, _LIVE_ATTEMPT_STATUSES, name="status")
    if order_role is not None:
        check_enum(order_role, _ORDER_ROLES, name="order_role")
    if attempt_index < 0:
        raise ValueError(f"attempt_index must be >= 0, got {attempt_index}")
    if (cloid_logical is None) != (cloid_hex is None):
        raise ValueError(
            "cloid_logical and cloid_hex must be provided together (the §8.2 "
            "two-layer id is one unit)"
        )
    if action == "place" and cloid_hex is None:
        raise ValueError("a 'place' attempt must carry its cloid pair (§8.3 rule 1)")
    if action == "cancel" and exchange_order_id is None:
        raise ValueError("a 'cancel' attempt must carry exchange_order_id")
    if action == "cancel_by_cloid" and cloid_hex is None:
        raise ValueError("a 'cancel_by_cloid' attempt must carry its cloid pair")
    _insert(
        conn,
        "live_order_attempts",
        {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "order_id": order_id,
            "action": action,
            "cloid_logical": cloid_logical,
            "cloid_hex": cloid_hex,
            "exchange_order_id": exchange_order_id,
            "symbol": symbol,
            "side": None if side is None else Side.parse(side).value,
            "qty": qty,
            "price": price,
            "reduce_only": reduce_only,
            "order_role": order_role,
            "attempt_index": attempt_index,
            "status": status,
            "requested_at": requested_at or datetime.now(timezone.utc),
        },
    )


def update_live_order_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    status: str | _Unset = _UNSET,
    exchange_order_id: str | None | _Unset = _UNSET,
    exchange_status: str | None | _Unset = _UNSET,
    error_message: str | None | _Unset = _UNSET,
    raw_exchange_payload_path: str | None | _Unset = _UNSET,
    acknowledged_at: datetime | None | _Unset = _UNSET,
) -> None:
    """Patch an attempt with its outcome (same _UNSET convention as update_order).

    An attempt row is patched exactly once, from ``submitted`` to its outcome:
    the trail is append-only evidence (schema §16.5), and the §8.3 pre-send
    check trusts these statuses to decide whether an earlier send's outcome is
    known — rewriting a settled attempt would falsify that record.

    A row that stays ``submitted`` forever is a defined terminal state, not a
    leak: it means the process died inside that send's network window and the
    attempt's own ack was never observed. §8.3 recovery resolves the ORDER's
    fate via orderStatus and records it on the orders row (the authoritative
    surface PR 4 reconciles against); it deliberately does not back-patch the
    dangling attempt, because that send's direct result is genuinely unknown
    (several attempts may exist and any one of them could be the winner).
    """
    if not isinstance(status, _Unset):
        check_enum(status, _LIVE_ATTEMPT_STATUSES, name="status")
    row = get_live_order_attempt(conn, attempt_id)
    if row is None:
        raise ValueError(f"live order attempt {attempt_id!r} does not exist")
    if row["status"] != "submitted":
        raise ValueError(
            f"live order attempt {attempt_id!r} already settled as {row['status']!r} "
            "— the evidence trail is immutable; a retry is a NEW attempt row"
        )
    provided: dict[str, Any] = {}
    for col, val in (
        ("status", status),
        ("exchange_order_id", exchange_order_id),
        ("exchange_status", exchange_status),
        ("error_message", error_message),
        ("raw_exchange_payload_path", raw_exchange_payload_path),
        ("acknowledged_at", acknowledged_at),
    ):
        if not isinstance(val, _Unset):
            provided[col] = _encode(val)
    if not provided:
        return
    assignments = ", ".join(f"{col} = ?" for col in provided)
    conn.execute(
        f"UPDATE live_order_attempts SET {assignments} WHERE attempt_id = ?",
        (*provided.values(), attempt_id),
    )


def get_live_order_attempt(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM live_order_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()


def iter_live_order_attempts(
    conn: sqlite3.Connection, run_id: str, *, cloid_hex: str | None = None
) -> list[sqlite3.Row]:
    """A run's attempts in insertion order, optionally for one cloid."""
    if cloid_hex is None:
        return conn.execute(
            "SELECT * FROM live_order_attempts WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM live_order_attempts WHERE run_id = ? AND cloid_hex = ? ORDER BY rowid",
        (run_id, cloid_hex),
    ).fetchall()


def next_live_attempt_index(conn: sqlite3.Connection, *, action: str, cloid_hex: str) -> int:
    """The next free attempt_index for one (cloid, action) — 0 for a first send.

    Derived from the store, not an in-memory counter, and with NO run filter:
    the UNIQUE (cloid_hex, action, attempt_index) evidence trail spans runs —
    a later run's shutdown sweep cancels an earlier run's surviving order —
    so the index namespace must span them too, or the second run re-derives
    an index the constraint already holds and every cancel of that order
    fails on the INSERT before a round-trip is even attempted.
    """
    check_enum(action, _LIVE_ATTEMPT_ACTIONS, name="action")
    row = conn.execute(
        "SELECT MAX(attempt_index) FROM live_order_attempts WHERE action = ? AND cloid_hex = ?",
        (action, cloid_hex),
    ).fetchone()
    return 0 if row[0] is None else row[0] + 1


# --------------------------------------------------------------------------
# kill_switch_events (phase3-spec §18.5)
# --------------------------------------------------------------------------


def insert_kill_switch_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_type: str,
    detail: str | None = None,
    error_message: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    check_enum(event_type, KILL_SWITCH_EVENT_TYPES, name="event_type")
    _insert(
        conn,
        "kill_switch_events",
        {
            "run_id": run_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "event_type": event_type,
            "detail": detail,
            "error_message": error_message,
        },
    )


def iter_kill_switch_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM kill_switch_events WHERE run_id = ? ORDER BY event_id", (run_id,)
    ).fetchall()
