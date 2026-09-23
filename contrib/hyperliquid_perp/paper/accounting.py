"""Paper accounting: simulated fill posting and funding exactly-once.

Transactional posting over :class:`~..persistence.db.Database`, on the paper
lane's modelled money (a ``fee_rate`` and the fill's own price):

- :func:`post_fill` writes the fill *and* the updated ``current_positions`` /
  ``current_account_state`` in one transaction (phase2-data §1); a duplicate
  ``slice_id`` aborts the whole unit. :func:`apply_fill` is the same unit on a
  caller-owned transaction, for the engine's per-tick bundle.
- :func:`record_funding` posts a funding settlement to the wallet exactly once
  (phase2-data §10 / execution §6.5): the wallet moves only on the
  ``pending -> posted`` (or first ``posted``) transition, never on a retry.

The fill math, the account formulas, the run genesis and the replay that
checks these writes live in :mod:`..runtime.accounting`, which both lanes
share (refactor plan v2, T1-d).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from ..common.enum_guard import check_enum
from ..domains.perp.margin import unrealized_pnl
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import funding_event_id
from ..persistence.models import DECIMAL_CONTEXT, AccountLedger, PositionState, Side
from ..runtime.accounting import FillEffect, compute_fill_effect, funding_pnl

__all__ = [
    "FundingResult",
    "apply_fill",
    "post_fill",
    "record_funding",
]

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Transactional posting
# --------------------------------------------------------------------------


def post_fill(
    db: Database,
    *,
    run_id: str,
    mode: str,
    fill_id: str,
    order_id: str,
    symbol: str,
    side: Side | str,
    qty: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    liquidity_type: str = "simulated",
    slice_id: str | None = None,
    plan_id: str | None = None,
    flip_leg: str | None = None,
    slice_index: int | None = None,
    fill_reason: str | None = None,
    timestamp: datetime | None = None,
) -> FillEffect:
    """Post one simulated fill atomically (phase2-data §1): fill + position + ledger.

    Reads the current position and ledger, computes the effect (pure), then writes
    the fill row and both materialized ``current_*`` rows in one transaction. A
    duplicate ``slice_id`` raises ``sqlite3.IntegrityError`` and rolls the whole
    unit back, so a retried slice can never double-post.

    Solvency is deliberately not enforced (pre-trade checks are the PR3 engine's
    job, and driving an account into liquidation is a scenario the paper model
    must be able to express) — but a fill that leaves the account insolvent at
    its own price is warned about, so it never happens silently.
    """
    now = timestamp or _utcnow()
    with db.transaction() as conn:
        return apply_fill(
            conn,
            run_id=run_id,
            mode=mode,
            fill_id=fill_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            fee_rate=fee_rate,
            liquidity_type=liquidity_type,
            slice_id=slice_id,
            plan_id=plan_id,
            flip_leg=flip_leg,
            slice_index=slice_index,
            fill_reason=fill_reason,
            timestamp=now,
        )


def apply_fill(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    mode: str,
    fill_id: str,
    order_id: str,
    symbol: str,
    side: Side | str,
    qty: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    liquidity_type: str = "simulated",
    slice_id: str | None = None,
    plan_id: str | None = None,
    flip_leg: str | None = None,
    slice_index: int | None = None,
    fill_reason: str | None = None,
    timestamp: datetime | None = None,
) -> FillEffect:
    """Write one fill + its position/ledger updates on a **caller-owned** transaction.

    The transactional core of :func:`post_fill`, exposed so the PR3 execution
    engine can bundle the *same fill's* order / plan / protection writes into the
    one ``db.transaction()`` (phase2-execution §5.3 / PR3 requirement 8: every
    change caused by one fill commits atomically). ``post_fill`` is the standalone
    single-fill entry point that opens its own transaction around this; the engine
    calls this directly inside its tick transaction. Callers must already be inside
    a ``db.transaction()`` — enforced below: a connection still in autocommit would
    silently commit each write separately, losing exactly the atomicity this
    function exists to provide.
    """
    if not conn.in_transaction:
        raise ValueError(
            "apply_fill must run inside an open transaction (db.transaction()); "
            "an autocommit connection would break the fill's atomicity contract"
        )
    side = Side.parse(side)  # parse once; compute/insert below accept the enum as-is
    now = timestamp or _utcnow()
    position = repo.get_current_position(conn, run_id, symbol) or PositionState.flat(symbol)
    ledger = repo.require_current_account_state(conn, run_id)

    effect = compute_fill_effect(position, side=side, qty=qty, price=price, fee_rate=fee_rate)

    # Pinned: these sums are persisted (and re-derived under the same pin by
    # replay), so they must not round under a perturbed ambient context.
    with localcontext(DECIMAL_CONTEXT):
        new_wallet = ledger.wallet_balance + effect.wallet_delta
        # Equity proxy at the fill's own price (other symbols' marks are
        # unknown here); a definitive margin check is the engine's, this is
        # the ledger's last-line audit trail.
        new_pos = effect.position
        equity_at_fill_price = new_wallet + (
            Decimal(0)
            if new_pos.is_flat
            else unrealized_pnl(new_pos.size, price, new_pos.entry_price)
        )
        new_ledger = AccountLedger(
            wallet_balance=new_wallet,
            realized_pnl=ledger.realized_pnl + effect.realized_pnl_delta,
            total_fees=ledger.total_fees + effect.fee,
            net_funding_pnl=ledger.net_funding_pnl,
        )
    if new_wallet < 0 or equity_at_fill_price <= 0:
        logger.warning(
            "fill %s leaves run %s insolvent at its own price: wallet %s, equity(at fill price) %s",
            fill_id,
            run_id,
            new_wallet,
            equity_at_fill_price,
        )

    # Insert the fill first: its slice_id UNIQUE constraint is the exactly-once
    # guard, so a duplicate aborts before any state moves.
    repo.insert_fill(
        conn,
        fill_id=fill_id,
        mode=mode,
        run_id=run_id,
        order_id=order_id,
        symbol=symbol,
        side=side,
        fill_qty=qty,
        fill_price=price,
        fill_notional=effect.fill_notional,
        fee=effect.fee,
        fee_rate=fee_rate,
        realized_pnl_delta=effect.realized_pnl_delta,
        liquidity_type=liquidity_type,
        slice_id=slice_id,
        plan_id=plan_id,
        flip_leg=flip_leg,
        slice_index=slice_index,
        fill_reason=fill_reason,
        timestamp=now,
    )
    repo.upsert_current_position(conn, run_id, effect.position, updated_at=now)
    repo.upsert_current_account_state(conn, run_id, new_ledger, updated_at=now)
    return effect


_FUNDING_RESULT_STATUSES = frozenset({"posted", "pending", "already_posted"})


@dataclass(frozen=True)
class FundingResult:
    """Outcome of a funding settlement attempt (exactly-once).

    ``funding_pnl`` is present exactly when this call moved the wallet
    (``status == "posted"``); a retry against an already-posted event reports
    ``already_posted`` with no pnl (nothing moved *this* time).
    """

    status: str  # "posted" | "pending" | "already_posted"
    funding_event_id: str
    funding_pnl: Decimal | None

    def __post_init__(self) -> None:
        # Same construction-time coupling as the sibling result dataclasses
        # (FillEffect, LiquidationEstimate): a hand-built instance must not be
        # able to pair a non-posted status with a pnl (or a posted one without),
        # since callers branch on status while trusting funding_pnl's presence.
        check_enum(self.status, _FUNDING_RESULT_STATUSES, name="FundingResult.status")
        if (self.funding_pnl is not None) != (self.status == "posted"):
            raise ValueError(
                "FundingResult.funding_pnl must be present exactly when status "
                f"== 'posted' (status {self.status!r}, funding_pnl {self.funding_pnl})"
            )


def record_funding(
    db: Database,
    *,
    run_id: str,
    mode: str,
    symbol: str,
    funding_timestamp: datetime,
    position_size: Decimal,
    funding_rate: Decimal | None,
    mark_price: Decimal | None = None,
    source: str | None = None,
    recorded_at: datetime | None = None,
) -> FundingResult:
    """Post one hourly funding settlement to the wallet exactly once (execution §6.5).

    - rate available → post ``funding_pnl = -signed_position_notional *
      funding_rate`` to the wallet and ``net_funding_pnl``, recording the event
      ``posted``. The settlement basis (position size + mark) comes from the
      pending row on a backfill, otherwise from the call arguments.
    - rate unavailable → record (or leave) the event ``pending`` with no wallet
      move — ``mark_price`` is required here, so the settlement basis is captured
      complete; a later call with the backfilled rate transitions it to
      ``posted`` using the *stored* basis only (a mixed old-size/fresh-mark
      notional must never be fabricated).
    - already ``posted`` → no-op (a retry never double-posts).

    The wallet moves only on the transition into ``posted``, inside the same
    transaction that flips the status, so a crash cannot post twice.
    """
    # Canonicalise the settlement instant so the exactly-once key is stable: a
    # naive-vs-aware (or non-UTC-offset) representation of the same hour would
    # otherwise derive a different id and dedup key and let funding post twice.
    # Then floor to the settlement hour: the scheduler computes top-of-hour
    # instants but a fundingHistory backfill carries the venue's ms-epoch stamp
    # (usually, not contractually, exactly on the hour) — a sub-hour skew must
    # not split one settlement into two "different" events.
    if funding_timestamp.tzinfo is None:
        raise ValueError("funding_timestamp must be timezone-aware (UTC)")
    funding_timestamp = funding_timestamp.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )

    fe_id = funding_event_id(run_id, symbol, funding_timestamp)
    now = recorded_at or _utcnow()
    with db.transaction() as conn:
        existing = repo.get_funding_event(conn, fe_id)
        if existing is not None and existing["status"] == "posted":
            return FundingResult("already_posted", fe_id, None)

        # Rate still unavailable: keep/record a pending event, never fabricate a
        # rate (execution §6.5). Wallet untouched. The mark is required now —
        # the snapshot that told us the hour elapsed knows it — so the stored
        # settlement basis (size + mark) is complete and the backfill can never
        # be tempted to substitute a later, wrong-instant mark.
        if funding_rate is None:
            if mark_price is None:
                raise ValueError("a pending funding event must record its settlement mark_price")
            if mark_price <= 0:
                # Parity with compute_fill_effect / estimated_liquidation_price: a
                # non-positive mark must never enter the settlement basis. A stored
                # pending row with mark<=0 would later post internally-consistent but
                # nonsense pnl (mark=0 -> pnl=0, posted-once, permanently dropped).
                raise ValueError(f"funding settlement mark_price must be > 0, got {mark_price}")
            if existing is None:
                repo.insert_funding_event(
                    conn,
                    funding_event_id=fe_id,
                    mode=mode,
                    run_id=run_id,
                    symbol=symbol,
                    funding_timestamp=funding_timestamp,
                    position_size=position_size,
                    status="pending",
                    mark_price=mark_price,
                    source=source,
                    recorded_at=now,
                )
            return FundingResult("pending", fe_id, None)

        # Funding basis = the position and mark *at the settlement hour*. On a
        # pending backfill both were captured on the pending row when the hour
        # elapsed — use them, never the caller's current values, so a position
        # or mark change between pending and backfill can't post funding on a
        # basis that never actually existed. A fresh live posting (no pending
        # row) uses the caller's settlement-time values.
        if existing is None:
            basis_size, basis_mark = position_size, mark_price
            if basis_mark is None:
                raise ValueError("a mark_price is required to post funding with a known rate")
        else:
            basis_size = Decimal(existing["position_size"])
            stored_mark = existing["mark_price"]
            if stored_mark is None:  # pre-guard legacy/corrupt row: refuse to guess
                raise ValueError(
                    f"pending funding event {fe_id!r} has no stored mark_price; "
                    "cannot post on a fabricated basis"
                )
            basis_mark = Decimal(stored_mark)

        if basis_mark <= 0:  # pre-guard legacy row / fresh caller: refuse a non-positive basis
            raise ValueError(f"funding settlement mark_price must be > 0, got {basis_mark}")

        with localcontext(DECIMAL_CONTEXT):
            signed_notional = basis_size * basis_mark
            pnl = funding_pnl(signed_notional, funding_rate)

        if existing is None:
            repo.insert_funding_event(
                conn,
                funding_event_id=fe_id,
                mode=mode,
                run_id=run_id,
                symbol=symbol,
                funding_timestamp=funding_timestamp,
                position_size=basis_size,
                status="posted",
                mark_price=basis_mark,
                signed_position_notional=signed_notional,
                funding_rate=funding_rate,
                funding_pnl=pnl,
                source=source,
                recorded_at=now,
            )
        else:  # existing pending -> posted (the exactly-once transition)
            # position_size stays as recorded on the pending row (the settlement
            # basis); only the newly-learned rate / pnl / mark are filled in.
            repo.set_funding_status(
                conn,
                fe_id,
                status="posted",
                funding_rate=funding_rate,
                funding_pnl=pnl,
                signed_position_notional=signed_notional,
                mark_price=basis_mark,
                source=source,
                updated_at=now,
            )

        ledger = repo.require_current_account_state(conn, run_id)
        # Pinned for the same reason as post_fill's ledger sums: the persisted
        # wallet must match what replay re-derives under DECIMAL_CONTEXT.
        with localcontext(DECIMAL_CONTEXT):
            new_ledger = AccountLedger(
                wallet_balance=ledger.wallet_balance + pnl,
                realized_pnl=ledger.realized_pnl,
                total_fees=ledger.total_fees,
                net_funding_pnl=ledger.net_funding_pnl + pnl,
            )
        if new_ledger.wallet_balance < 0:
            # Same warn-never-block breadcrumb as post_fill and
            # runtime.accounting.initialize_run: funding
            # is the most realistic recurring driver of paper-account insolvency, so
            # crossing negative here must not happen silently. Hard checks are PR3's.
            logger.warning(
                "funding %s leaves run %s with a negative wallet: %s",
                fe_id,
                run_id,
                new_ledger.wallet_balance,
            )
        repo.upsert_current_account_state(conn, run_id, new_ledger, updated_at=now)
    return FundingResult("posted", fe_id, pnl)
