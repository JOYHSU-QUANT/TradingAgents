"""The ``fills`` table — simulated paper fills and exchange-sourced live fills."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from ...domains.perp.enum_guard import check_enum
from ..models import DECIMAL_CONTEXT, Side
from ._base import _insert
from ._vocab import _FLIP_LEGS, _LIQUIDITY_TYPES, _MODES, LIVE_LIQUIDITY_ROLES

__all__ = [
    "get_fill",
    "get_fill_by_exchange_key",
    "insert_fill",
    "insert_live_fill",
    "iter_fills",
    "iter_live_fills",
    "last_live_fill_time",
    "newest_live_fill_order_key",
    "posted_exchange_fee",
    "require_live_fill_basis",
]


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
    restarted TWAP slice re-derives the same id (see :mod:`..ids`) and the insert
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


def iter_fills(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    chronological: bool = False,
    symbol: str | None = None,
) -> list[sqlite3.Row]:
    """A run's fills (optionally one ``symbol``'s) — in insertion order, or in
    EXCHANGE-time order for live replay.

    Paper fills keep ``rowid``: they are simulated, insertion IS execution order, and
    they carry no ``exchange_fill_time`` to sort on.

    ``chronological`` orders by the exchange's clock instead of ours, which live
    replay needs because insertion order is NOT trade order there: live fills arrive
    from two racing sources, and a reconnect backfill posts fills that happened
    BEFORE the ones the socket just delivered. The realized money is order-independent
    (it comes per-fill from the exchange's ``closedPnl``), but the position's
    weighted-average entry price is not — so replay must fold live fills in the order
    the exchange executed them, which is the same order the ingester applies them in
    (``LiveFillProcessor.ingest_message`` sorts on these very columns). The two
    orderings MUST stay identical: if they drifted, the materialized books and the
    replayed books would disagree on entry price, and each would be internally
    consistent, so no check could see it.
    """
    where = "run_id = ?"
    params: list[object] = [run_id]
    if symbol is not None:
        # Filtered in SQL, not by the caller: the position re-fold needs ONE symbol's
        # fills, and on a reconnect every backfilled fill is out of order, so this runs
        # once per fill inside a write transaction. Pulling the whole run's fills back
        # each time just to drop most of them holds the DB lock for no reason.
        where += " AND symbol = ?"
        params.append(symbol)
    order = "exchange_fill_time, exchange_fill_key, rowid" if chronological else "rowid"
    return conn.execute(f"SELECT * FROM fills WHERE {where} ORDER BY {order}", params).fetchall()


def last_live_fill_time(conn: sqlite3.Connection, run_id: str) -> datetime | None:
    """The exchange timestamp of this run's newest booked live fill, or ``None``.

    A STARTUP-only backfill floor: at startup nothing newer has been booked yet, so the
    newest fill really is where the gap begins. It is NOT a general backfill cursor —
    once the run books anything newer (a reconnect drains HL's isSnapshot batch before
    the backfill runs), this MAX jumps forward and the window would collapse back onto
    the lookback, silently skipping the outage. The gap's start is a fact only the
    caller holds; see ``FillBackfiller.backfill``.
    """
    row = conn.execute(
        "SELECT MAX(exchange_fill_time) AS last_at FROM fills "
        "WHERE run_id = ? AND mode = 'live' AND exchange_fill_time IS NOT NULL",
        (run_id,),
    ).fetchone()
    if row is None or row["last_at"] is None:
        return None
    return datetime.fromisoformat(row["last_at"])


def newest_live_fill_order_key(
    conn: sqlite3.Connection, run_id: str, symbol: str
) -> tuple[datetime, str] | None:
    """The fold position of the newest booked fill for one symbol, or ``None``.

    The ordering key — ``(exchange_fill_time, exchange_fill_key)`` — not just the
    timestamp, because that pair IS the order live fills are folded in (see
    :func:`iter_fills`), and the out-of-order test in ``apply_live_fill`` has to ask
    the same question the fold answers.

    A timestamp alone is not enough. Two fills can land in the SAME millisecond, and
    the fold breaks that tie on the key: a later-arriving fill whose key sorts BEFORE
    the booked one belongs earlier in the fold even though its timestamp is not older.
    Compared on time alone it would look in-order, be folded incrementally on top, and
    the materialized position would then disagree with the replayed one — the very
    divergence the re-fold exists to prevent.
    """
    row = conn.execute(
        "SELECT exchange_fill_time, exchange_fill_key FROM fills "
        "WHERE run_id = ? AND mode = 'live' AND symbol = ? AND exchange_fill_time IS NOT NULL "
        "ORDER BY exchange_fill_time DESC, exchange_fill_key DESC LIMIT 1",
        (run_id, symbol),
    ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["exchange_fill_time"]), row["exchange_fill_key"]


def posted_exchange_fee(exchange_fee: Decimal | None) -> Decimal:
    """The fee AMOUNT POSTED to the books for a live fill: the exchange's, or 0 (§15.1).

    A fill can arrive with no usable fee (absent, or denominated in a non-USDC token),
    and it is still posted — with the fee PENDING, which books ``0`` now and corrects
    it later through an ``accounting_adjustment_events`` row.

    The one definition of that "0", because every consumer must agree on it: the
    LEDGER delta (``live.fills.apply_live_fill``), the stored ``fills.fee`` column
    (and its derived ``fee_rate``) in :func:`insert_live_fill`, the ``old_value`` a
    later correction folds from (``live.fills._effective_fee``, for
    ``backfill_fill_fee``), the out-of-order position re-fold
    (``live.fills._rebuild_position``), and the §5 replay's live fold
    (``paper.accounting.replay_within``). Were the pending
    placeholder ever to change — say to an ESTIMATED fee — a copy left behind would
    have the backfill book ``new - 0`` against a ledger that had already taken the
    estimate, double-charging the fee; and replay would reproduce the double-charge
    exactly, so the books would be consistently, invisibly wrong.
    """
    return Decimal(0) if exchange_fee is None else exchange_fee


def require_live_fill_basis(fill: sqlite3.Row) -> Decimal:
    """A live fill's exchange ``closedPnl`` — the §15 realized basis — or fail loud.

    ``insert_live_fill`` always records ``exchange_closed_pnl`` (closedPnl arrives
    with the fill; only the fee can be pending), so a NULL here means a fill was
    written into a live run through a non-live path — folding it against the paper
    fee model would silently misstate the books. Refuse rather than guess.

    The one definition of the check, because the two live folds must agree on it:
    the out-of-order position re-fold (``live.fills._rebuild_position``) and the §5
    replay's live fold (``paper.accounting.replay_within``). Were one to grow a
    fallback the other lacks, materialized and replayed state would diverge on
    exactly the row the check exists to catch.
    """
    value = fill["exchange_closed_pnl"]
    if value is None:
        raise ValueError(
            f"live fill {fill['fill_id']!r} has no exchange_closed_pnl — it was not "
            "recorded through insert_live_fill; the live accounting basis is missing"
        )
    return Decimal(value)


def get_fill(conn: sqlite3.Connection, fill_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()


def get_fill_by_exchange_key(
    conn: sqlite3.Connection, exchange_fill_key: str
) -> sqlite3.Row | None:
    """The live fill row a §14.2 dedupe key belongs to, or ``None``.

    ``exchange_fill_key`` is UNIQUE (schema v6), so at most one row matches — the
    exactly-once guard :func:`insert_live_fill` leans on. A caller uses this to
    tell "already applied" (skip) from "new" without depending on catching the
    IntegrityError, which is the same fact but only observable mid-transaction.
    """
    return conn.execute(
        "SELECT * FROM fills WHERE exchange_fill_key = ?", (exchange_fill_key,)
    ).fetchone()


def insert_live_fill(
    conn: sqlite3.Connection,
    *,
    fill_id: str,
    run_id: str,
    order_id: str,
    symbol: str,
    side: str,
    fill_qty: Decimal,
    fill_price: Decimal,
    fill_notional: Decimal,
    exchange_fill_key: str,
    exchange_fill_time: datetime,
    exchange_closed_pnl: Decimal,
    liquidity_role: str,
    exchange_fee: Decimal | None = None,
    exchange_fill_id: str,
    exchange_order_id: str,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    plan_id: str | None = None,
    flip_leg: str | None = None,
    slice_index: int | None = None,
    fill_reason: str | None = None,
    raw_exchange_payload_path: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    """Insert one live (exchange-sourced) fill. Raises ``IntegrityError`` on a duplicate key.

    The UNIQUE ``exchange_fill_key`` (§14.2 — the exchange's ``tid``) is the
    §14.3-rule-1 exactly-once guard: the SAME exchange fill arriving again from any of
    the three sources (WS userFills, REST userFillsByTime, orderStatus) is rejected here
    instead of double-posting. (§14.2's composite fallback is deliberately NOT
    implemented and never will be — it can collide two genuinely distinct fills and
    silently drop one; see ``ids.exchange_fill_key``. Do not reintroduce it from this
    docstring.)

    Unlike :func:`insert_fill` (the paper simulation), a live fill's money is
    exchange-authoritative (§15): ``realized_pnl_delta`` is the exchange's
    ``closedPnl`` and the posted ``fee`` is the exchange's ``fee`` — NOT the
    paper ``fill_notional * fee_rate`` model, so the ``fee == notional * rate``
    identity :func:`insert_fill` enforces deliberately does NOT apply here (the
    effective ``fee_rate`` is stored derived-for-display only). ``fill_notional``
    is still checked against ``|qty * price|`` — that identity is pure and holds
    for any fill.

    ``exchange_fee`` is ``None`` when the fill arrived without a fee (§15.1 rule
    2 — fee pending): the row records it AS-RECORDED and the posted ``fee`` column
    is ``0``. The later correction is an ``accounting_adjustment_events`` row
    (never an overwrite of this immutable fill), which live replay folds — so the
    recorded fill and its corrections together are the single accounting basis.
    """
    side = Side.parse(side)
    check_enum(liquidity_role, LIVE_LIQUIDITY_ROLES, name="liquidity_role")
    if flip_leg is not None:
        check_enum(flip_leg, _FLIP_LEGS, name="flip_leg")
    if not exchange_fill_key:
        raise ValueError("a live fill must carry a non-empty exchange_fill_key (§14.2)")
    if not exchange_fill_id or not exchange_order_id:
        # Present on every live fill by construction (ExchangeFill.parse requires
        # tid and oid, and an unmapped-oid fill is never inserted) — so a NULL here
        # is caller error, and it would silently break what assumes them on every
        # live row: the evidence keys, §15.1 rule-8 redelivery verification, and
        # the §12.3 drift recorders.
        raise ValueError(
            "a live fill must carry its exchange_fill_id (tid) and exchange_order_id (oid)"
        )
    if exchange_fill_time is None:
        # Not merely a missing datum: (exchange_fill_time, exchange_fill_key) is the
        # §14.3 fold/ordering key. A NULL time sorts before genesis in chronological
        # replay yet is filtered out of newest_live_fill_order_key/last_live_fill_time
        # (both IS NOT NULL) — the fill folds first in one world and is invisible to
        # out-of-order detection and the startup floor in the other, an undetectable
        # materialized-vs-replay divergence. The ingest path always has it
        # (ExchangeFill.parse requires ``time``), so a None here is caller error.
        raise ValueError("a live fill must carry its exchange_fill_time (§14.3 ordering key)")
    if (cloid_logical is None) != (cloid_hex is None):
        raise ValueError(
            "cloid_logical and cloid_hex must be provided together (the §8.2 "
            "two-layer id is one unit)"
        )
    with localcontext(DECIMAL_CONTEXT):  # round exactly like the producing math
        expected_notional = abs(fill_qty * fill_price)
        if fill_notional != expected_notional:
            raise ValueError(
                f"fill_notional {fill_notional} != |fill_qty * fill_price| {expected_notional}"
            )
        # The posted fee is the exchange's fee, or 0 while it is pending (§15.1) —
        # one definition, shared with the ledger delta and the correction fold.
        # fee_rate is display-only for a live fill (the exchange fee is not a
        # modelled rate) — derived so the CSV export column is populated, never
        # re-derived by replay.
        posted_fee = posted_exchange_fee(exchange_fee)
        fee_rate = Decimal(0) if fill_notional == 0 else posted_fee / fill_notional
    _insert(
        conn,
        "fills",
        {
            "fill_id": fill_id,
            "timestamp": timestamp or datetime.now(timezone.utc),
            "mode": "live",
            "run_id": run_id,
            "order_id": order_id,
            "slice_id": None,
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
            "fee": posted_fee,
            "fee_rate": fee_rate,
            "realized_pnl_delta": exchange_closed_pnl,
            "liquidity_type": liquidity_role,
            "fill_reason": fill_reason,
            "exchange_fill_key": exchange_fill_key,
            "cloid_logical": cloid_logical,
            "cloid_hex": cloid_hex,
            "liquidity_role": liquidity_role,
            # AS-RECORDED: None means the fee was pending at ingest — kept as-is
            # (the correction is an adjustment event, never an overwrite).
            "exchange_fee": exchange_fee,
            "exchange_closed_pnl": exchange_closed_pnl,
            "exchange_fill_time": exchange_fill_time,
            "raw_exchange_payload_path": raw_exchange_payload_path,
        },
    )


def iter_live_fills(
    conn: sqlite3.Connection, run_id: str, *, pending_fee_only: bool = False
) -> list[sqlite3.Row]:
    """A run's live fills in insertion order; optionally only fee-pending ones.

    ``pending_fee_only`` selects the §15.1-rule-3 backlog a reconciliation job
    must clear: fills whose fee was pending at ingest (``exchange_fee`` NULL) AND
    have no fee correction yet. The recorded fill is immutable, so a backfilled
    fee lives only in an ``accounting_adjustment_events`` row (§15.1 rule 4, never
    an overwrite) — without the NOT EXISTS clause an already-corrected fill would
    reappear as pending forever and the job would re-post it every pass (the
    adjustment id dedupe blocks the double-post, but the churn is real).
    ``mode = 'live'`` guards against a mixed-mode store, though a run is
    single-mode by construction.
    """
    if pending_fee_only:
        return conn.execute(
            "SELECT * FROM fills f WHERE f.run_id = ? AND f.mode = 'live' "
            "AND f.exchange_fill_key IS NOT NULL AND f.exchange_fee IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM accounting_adjustment_events a "
            "  WHERE a.run_id = f.run_id AND a.target_table = 'fills' "
            "  AND a.target_id = f.fill_id AND a.adjustment_type = 'fee'"
            ") ORDER BY f.rowid",
            (run_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM fills WHERE run_id = ? AND mode = 'live' "
        "AND exchange_fill_key IS NOT NULL ORDER BY rowid",
        (run_id,),
    ).fetchall()
