"""Paper accounting: fill posting, funding exactly-once, account math, replay.

Two layers:

- **Pure math** (no I/O): the account/PnL/margin formulas of execution §6 and
  :func:`compute_fill_effect`, which turns one fill into its position delta,
  realized PnL, fee and wallet delta. All-Decimal, deterministic — the same
  inputs always produce the same output, which is what makes replay meaningful.
- **Transactional posting** (over :class:`~..persistence.db.Database`):
  :func:`post_fill` writes the fill *and* the updated ``current_positions`` /
  ``current_account_state`` in one transaction (phase2-data §1); a duplicate
  ``slice_id`` aborts the whole unit. :func:`record_funding` posts a funding
  settlement to the wallet exactly once (phase2-data §10 / execution §6.5): the
  wallet moves only on the ``pending -> posted`` (or first ``posted``)
  transition, never on a retry.

:func:`replay` rebuilds positions and the ledger from the committed fills,
posted funding and — for a live run — the recorded accounting adjustments
(§15.1 fee corrections, folded through :func:`adjustment_ledger_delta`), and
compares them to the materialized ``current_*`` tables, reporting any mismatch
(spec §5 accounting-replay acceptance check).

Ledger convention (execution §6.1/§6.5): ``wallet_balance`` already includes
realized PnL, fees and funding, so ``account_equity = wallet_balance +
total_unrealized_pnl`` — nothing is double-counted.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import NamedTuple

from ..common.enum_guard import check_enum
from ..domains.perp.margin import (
    MarginSchedule,
    account_equity,
    position_notional,
    unrealized_pnl,
)
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import funding_event_id
from ..persistence.models import DECIMAL_CONTEXT, AccountLedger, PositionState, Side

__all__ = [
    "AccountMetrics",
    "FillEffect",
    "FundingResult",
    "LedgerDeltas",
    "LiveFillEffect",
    "PositionValuation",
    "ReplayResult",
    "account_equity",
    "adjustment_ledger_delta",
    "apply_fill",
    "available_balance",
    "compute_fill_effect",
    "compute_live_fill_effect",
    "effective_leverage",
    "fee_for_notional",
    "funding_pnl",
    "initial_margin",
    "initialize_run",
    "maintenance_margin",
    "margin_ratio",
    "position_notional",
    "post_fill",
    "record_funding",
    "replay",
    "replay_within",
    "summarize_account",
    "unrealized_pnl",
    "used_initial_margin",
]

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value: object) -> Decimal | None:
    """Decode a stored TEXT value back to Decimal; ``None`` stays ``None``.

    A local twin of the repository's decoder, used by the live-replay branch to
    read the exchange-basis columns (``exchange_fee``, adjustment ``old/new_value``)
    without importing a private helper across the module boundary.
    """
    return None if value is None else Decimal(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Pure formulas (execution §6)
# --------------------------------------------------------------------------
# ``position_notional`` / ``unrealized_pnl`` / ``account_equity`` live in
# ``domains.perp.margin`` (the liquidation search shares them) and are
# re-exported here so accounting stays the one-stop module for the §6 formulas.


def available_balance(equity: Decimal, used_initial_margin_total: Decimal) -> Decimal:
    """``account_equity - used_initial_margin`` (§6.1)."""
    return equity - used_initial_margin_total


def effective_leverage(total_position_notional: Decimal, equity: Decimal) -> Decimal | None:
    """``total_notional / equity`` (§6.1); ``None`` on a non-positive equity.

    A non-positive equity means a margin-called / liquidatable account, where
    leverage is undefined. ``None`` (stored NULL) rather than ``0``: a ``0``
    would read downstream (AI prompt, CSV) as "no exposure" — the opposite of
    the truth — with no way to tell the two states apart.
    """
    if equity <= 0:
        return None
    return total_position_notional / equity


def margin_ratio(equity: Decimal, total_maintenance_margin: Decimal) -> Decimal | None:
    """``account_equity / total_maintenance_margin`` (§6.6); ``None`` when no margin.

    With no open position the maintenance margin is ``0`` and the ratio is
    undefined — ``None`` rather than a divide-by-zero (there is no liquidation
    risk to express).
    """
    if total_maintenance_margin == 0:
        return None
    return equity / total_maintenance_margin


def initial_margin(notional: Decimal, leverage: Decimal) -> Decimal:
    """``position_notional / configured_leverage`` (§6.6)."""
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    return notional / leverage


def maintenance_margin(schedule: MarginSchedule, size: Decimal, mark_price: Decimal) -> Decimal:
    """A position's maintenance margin at the current mark (§6.6 / §6.6.1)."""
    return schedule.maintenance_margin(position_notional(size, mark_price))


def fee_for_notional(fill_notional: Decimal, taker_fee_rate: Decimal) -> Decimal:
    """``abs(fill_notional) * taker_fee_rate`` — always a non-negative cost (§6.5)."""
    return abs(fill_notional) * taker_fee_rate


def funding_pnl(signed_position_notional: Decimal, funding_rate: Decimal) -> Decimal:
    """``-signed_position_notional * funding_rate`` — income positive, cost negative (§6.5)."""
    return -signed_position_notional * funding_rate


def used_initial_margin(notionals: Iterable[Decimal], leverage: Decimal) -> Decimal:
    """Sum of each position's initial margin (§6.6)."""
    return sum((initial_margin(n, leverage) for n in notionals), Decimal(0))


# --------------------------------------------------------------------------
# Fill effect (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FillEffect:
    """The full effect of one fill on a position and the wallet (all pure math).

    ``position`` is the new :class:`PositionState` (with ``realized_pnl``
    advanced by ``realized_pnl_delta``); ``wallet_delta`` is
    ``realized_pnl_delta - fee`` — what the fill moves the wallet by.
    """

    position: PositionState
    realized_pnl_delta: Decimal
    fee: Decimal
    fill_notional: Decimal
    wallet_delta: Decimal

    def __post_init__(self) -> None:
        # The documented identities, enforced at construction so a hand-built
        # (test/mock) instance can't quietly disagree with the fill math.
        if self.fee < 0:
            raise ValueError(f"FillEffect.fee must be >= 0, got {self.fee}")
        if self.fill_notional < 0:
            raise ValueError(f"FillEffect.fill_notional must be >= 0, got {self.fill_notional}")
        if self.wallet_delta != self.realized_pnl_delta - self.fee:
            raise ValueError(
                f"FillEffect.wallet_delta {self.wallet_delta} != "
                f"realized_pnl_delta - fee {self.realized_pnl_delta - self.fee}"
            )


def compute_fill_effect(
    position: PositionState,
    *,
    side: Side | str,
    qty: Decimal,
    price: Decimal,
    fee_rate: Decimal,
) -> FillEffect:
    """Apply one fill to ``position``; return the position delta, PnL and fee (§6.3/§6.5).

    Handles add (weighted-average entry), reduce (entry unchanged, realize the
    closed portion), full close, and a reduce that crosses zero into the opposite
    side in a single fill (the remainder opens at ``price``). Realized PnL is
    ``closed_qty * (price - entry)`` signed by the *old* side (long positive when
    ``price > entry``); a reduce/close credits it to the wallet along with the
    (always-subtracted) fee.
    """
    side = Side.parse(side)
    if qty <= 0:
        raise ValueError(f"fill qty must be > 0, got {qty}")
    if price <= 0:
        raise ValueError(f"fill price must be > 0, got {price}")
    if fee_rate < 0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")

    with localcontext(DECIMAL_CONTEXT):
        signed_fill = qty if side is Side.BUY else -qty
        old_size = position.size
        old_entry = position.entry_price
        new_size = old_size + signed_fill

        fill_notional = abs(qty * price)
        fee = fee_for_notional(fill_notional, fee_rate)

        realized_delta = Decimal(0)
        reducing = old_size != 0 and (signed_fill > 0) != (old_size > 0)
        if reducing:
            assert old_entry is not None  # a non-flat position always has an entry
            closed_qty = min(abs(old_size), qty)
            direction = Decimal(1) if old_size > 0 else Decimal(-1)
            realized_delta = closed_qty * (price - old_entry) * direction

        if new_size == 0:
            new_entry: Decimal | None = None
        elif old_size == 0:
            new_entry = price  # opening from flat
        elif (new_size > 0) == (old_size > 0):
            # Same side after the fill: adding grows the average entry, reducing keeps it.
            if abs(new_size) > abs(old_size):
                assert old_entry is not None
                new_entry = (abs(old_size) * old_entry + qty * price) / abs(new_size)
            else:
                new_entry = old_entry
        else:
            # Crossed zero into the opposite side: the remainder is a fresh open.
            new_entry = price

        new_position = PositionState(
            coin=position.coin,
            size=new_size,
            entry_price=new_entry,
            realized_pnl=position.realized_pnl + realized_delta,
        )
        return FillEffect(
            position=new_position,
            realized_pnl_delta=realized_delta,
            fee=fee,
            fill_notional=fill_notional,
            wallet_delta=realized_delta - fee,
        )


# --------------------------------------------------------------------------
# Live fill effect (pure) — exchange-authoritative basis (phase3-spec §15)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveFillEffect:
    """The effect of one *live* (exchange-sourced) fill — money from the exchange.

    The live counterpart of :class:`FillEffect`. Its size/entry transition is
    identical (a fill moves a position the same way in either mode), but the
    MONEY is not modelled: ``realized_pnl_delta`` is the exchange's ``closedPnl``
    and ``fee`` is the exchange's ``fee`` as posted at ingest (``0`` while the fee
    is still pending — §15.1). ``wallet_delta`` is ``realized_pnl_delta - fee``.

    Unlike :class:`FillEffect`, ``fee`` is NOT constrained ``>= 0``: an exchange
    fee is whatever the venue reported — a maker rebate is legitimately negative,
    and a pending fill posts ``0``. Only the ``wallet_delta`` identity and a
    non-negative notional are invariants a hand-built (test/recovery) instance
    must still satisfy.
    """

    position: PositionState
    realized_pnl_delta: Decimal
    fee: Decimal
    fill_notional: Decimal
    wallet_delta: Decimal

    def __post_init__(self) -> None:
        if self.fill_notional < 0:
            raise ValueError(f"LiveFillEffect.fill_notional must be >= 0, got {self.fill_notional}")
        if self.wallet_delta != self.realized_pnl_delta - self.fee:
            raise ValueError(
                f"LiveFillEffect.wallet_delta {self.wallet_delta} != "
                f"realized_pnl_delta - fee {self.realized_pnl_delta - self.fee}"
            )


def compute_live_fill_effect(
    position: PositionState,
    *,
    side: Side | str,
    qty: Decimal,
    price: Decimal,
    exchange_fee: Decimal,
    exchange_closed_pnl: Decimal,
) -> LiveFillEffect:
    """Apply one exchange fill to ``position`` with the venue's fee / closedPnl (§15).

    The size/entry transition reuses :func:`compute_fill_effect` verbatim (passing
    ``fee_rate=0`` — its modelled fee/realized are discarded), so the two paths can
    never disagree about how a fill moves a position. The realized PnL and fee then
    come from the exchange, not the model: the returned position's ``realized_pnl``
    advances by ``exchange_closed_pnl`` (not the computed delta), and the wallet
    moves by ``exchange_closed_pnl - exchange_fee``.

    ``exchange_fee`` is the amount POSTED for this fill (``0`` while a fee is
    pending — the correction arrives later as an ``accounting_adjustment_events``
    row that replay folds, never as a mutation of the recorded fill).

    ``closedPnl`` is GROSS of the fill's fee, so subtracting ``exchange_fee`` here
    is a subtraction, NOT a double-subtraction. Verified empirically against live
    mainnet ``userFills`` (2026-07-14): over clean round trips on six coins, both
    directions, maker and taker tiers, ``closedPnl == side * (exit - entry) * size``
    exactly — residual zero, never ``-fee`` — and an OPENING fill reports
    ``closedPnl == "0.0"`` even when it charged a fee. Beware the official "Entry
    price and PnL" docs page, which gives ``closed pnl = fee + side * (mark -
    entry) * size``: that page scopes itself to the FRONTEND's column and does not
    describe this API field — do not "correct" the formula from it. A maker rebate
    arrives as a NEGATIVE ``fee``, which this expression credits correctly.
    (``builderFee`` is a SEPARATE field, not folded into ``fee``; if we ever route
    orders through a builder it has to be subtracted here too.)
    """
    transition = compute_fill_effect(position, side=side, qty=qty, price=price, fee_rate=Decimal(0))
    moved = transition.position
    with localcontext(DECIMAL_CONTEXT):
        new_position = PositionState(
            coin=moved.coin,
            size=moved.size,
            entry_price=moved.entry_price,
            realized_pnl=position.realized_pnl + exchange_closed_pnl,
        )
        wallet_delta = exchange_closed_pnl - exchange_fee
    return LiveFillEffect(
        position=new_position,
        realized_pnl_delta=exchange_closed_pnl,
        fee=exchange_fee,
        fill_notional=transition.fill_notional,
        wallet_delta=wallet_delta,
    )


# --------------------------------------------------------------------------
# Account aggregation (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionValuation:
    """A live position plus the data needed to value it: mark and margin schedule."""

    position: PositionState
    mark_price: Decimal
    schedule: MarginSchedule

    def __post_init__(self) -> None:
        # Same boundary rule as every other mark-price entry point
        # (compute_fill_effect, estimated_liquidation_price): a zero/negative
        # mark — say a market-data gap's placeholder — must fail here, not
        # flow into equity / maintenance-margin figures as silent nonsense.
        if self.mark_price <= 0:
            raise ValueError(f"PositionValuation.mark_price must be > 0, got {self.mark_price}")
        # A position must be valued against its *own* asset's margin schedule.
        # summarize_account trusts the caller (PR3's engine) to zip each open
        # position with its coin's schedule; a mismatched pairing would silently
        # value the position against another asset's tier table (wrong
        # maintenance margin / liquidation estimate), so reject it at construction.
        if self.position.coin != self.schedule.coin:
            raise ValueError(
                "PositionValuation.position and .schedule must be the same asset, got "
                f"position {self.position.coin!r} vs schedule {self.schedule.coin!r}"
            )


@dataclass(frozen=True)
class AccountMetrics:
    """Derived account-level figures at a given set of marks (execution §6.1/§6.6)."""

    wallet_balance: Decimal
    account_equity: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    total_position_notional: Decimal
    used_initial_margin: Decimal
    total_maintenance_margin: Decimal
    effective_leverage: Decimal | None
    margin_ratio: Decimal | None

    def __post_init__(self) -> None:
        # The documented None-couplings (see effective_leverage / margin_ratio
        # above), enforced at construction so a hand-built instance — a test
        # double, or a future deserializer rebuilding metrics from a stored
        # snapshot — can't pair a blown-up equity with a "real" leverage (which
        # downstream reads as exposure truth) or vice versa.
        if (self.effective_leverage is None) != (self.account_equity <= 0):
            raise ValueError(
                "AccountMetrics.effective_leverage must be None exactly when "
                f"account_equity <= 0 (equity {self.account_equity}, "
                f"leverage {self.effective_leverage})"
            )
        if (self.margin_ratio is None) != (self.total_maintenance_margin == 0):
            raise ValueError(
                "AccountMetrics.margin_ratio must be None exactly when "
                f"total_maintenance_margin == 0 (margin {self.total_maintenance_margin}, "
                f"ratio {self.margin_ratio})"
            )
        # The two arithmetic identities the ledger convention rests on (module
        # docstring; account_equity()/available_balance() in margin.py), enforced
        # like FillEffect's wallet_delta check so a hand-built or deserialized
        # instance can't silently disagree with the account math. Recomputed under
        # the same pinned context summarize_account used, so equality is exact.
        with localcontext(DECIMAL_CONTEXT):
            expected_equity = self.wallet_balance + self.unrealized_pnl
            expected_available = self.account_equity - self.used_initial_margin
        if self.account_equity != expected_equity:
            raise ValueError(
                f"AccountMetrics.account_equity {self.account_equity} != "
                f"wallet_balance + unrealized_pnl {expected_equity}"
            )
        if self.available_balance != expected_available:
            raise ValueError(
                f"AccountMetrics.available_balance {self.available_balance} != "
                f"account_equity - used_initial_margin {expected_available}"
            )


def summarize_account(
    ledger: AccountLedger,
    valuations: Sequence[PositionValuation],
    *,
    leverage: Decimal,
) -> AccountMetrics:
    """Aggregate the account-level metrics from the ledger and open positions.

    ``valuations`` must cover *every* open position for the run: this is a pure
    aggregation and cannot tell a deliberately-flat account from one missing a
    position, so an incomplete sequence silently under-reports notional / margin /
    leverage. The PR3 snapshot writer owns fetching the full set
    (``get_all_current_positions``) before calling this — completeness is that
    caller's responsibility, not enforced here.
    """
    with localcontext(DECIMAL_CONTEXT):
        total_unrealized = Decimal(0)
        total_notional = Decimal(0)
        total_maint = Decimal(0)
        notionals: list[Decimal] = []
        for v in valuations:
            if v.position.is_flat:
                continue
            assert v.position.entry_price is not None
            notional = position_notional(v.position.size, v.mark_price)
            total_notional += notional
            notionals.append(notional)
            total_unrealized += unrealized_pnl(
                v.position.size, v.mark_price, v.position.entry_price
            )
            total_maint += maintenance_margin(v.schedule, v.position.size, v.mark_price)
        equity = account_equity(ledger.wallet_balance, total_unrealized)
        used_im = used_initial_margin(notionals, leverage)
        return AccountMetrics(
            wallet_balance=ledger.wallet_balance,
            account_equity=equity,
            available_balance=available_balance(equity, used_im),
            unrealized_pnl=total_unrealized,
            total_position_notional=total_notional,
            used_initial_margin=used_im,
            total_maintenance_margin=total_maint,
            effective_leverage=effective_leverage(total_notional, equity),
            margin_ratio=margin_ratio(equity, total_maint),
        )


# --------------------------------------------------------------------------
# Transactional posting
# --------------------------------------------------------------------------


def initialize_run(
    db: Database,
    *,
    run_id: str,
    mode: str,
    initial_balance_usdc: Decimal,
    schema_version: int,
    initial_positions: Iterable[PositionState] = (),
    config_json: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """Create a new run's row, opening ledger and any seed positions (one transaction).

    Applies the §5.4 initial balance / positions exactly once, at run creation;
    a normal restart must *not* call this (it recovers from the committed state).
    The seeds are also persisted to ``run_seed_positions`` — the replay genesis —
    so :func:`replay` never depends on the caller's (possibly since-edited) config.

    Solvency is not enforced here (margin feasibility needs marks and a margin
    schedule, which live in the PR3 engine's pre-trade checks); an obviously
    insolvent genesis is only warned about, never blocked — simulating a
    near-liquidation account is a legitimate scenario for direct callers
    (tests, liquidation studies). The YAML boundary is deliberately stricter:
    ``PaperAccountConfig`` rejects a non-positive opening balance outright,
    because there it is almost certainly an operator typo — a stressed genesis
    is still expressible in config via adverse seed positions.
    """
    if initial_balance_usdc <= 0:
        logger.warning(
            "run %s starts with a non-positive initial balance (%s USDC)",
            run_id,
            initial_balance_usdc,
        )
    now = created_at or _utcnow()
    with db.transaction() as conn:
        # Re-initializing an existing run is a lifecycle error (a restart replays,
        # it does not re-init). Surface it as the same clean domain error the
        # missing-run path gives (repo.require_current_account_state), not the raw sqlite3.IntegrityError
        # a bare insert_run PK conflict would raise.
        if repo.get_run(conn, run_id) is not None:
            raise ValueError(
                f"run {run_id!r} is already initialized; replay or post fills to "
                "continue it — do not call initialize_run again"
            )
        repo.insert_run(
            conn,
            run_id=run_id,
            mode=mode,
            initial_balance_usdc=initial_balance_usdc,
            schema_version=schema_version,
            config_json=config_json,
            created_at=now,
        )
        repo.upsert_current_account_state(
            conn, run_id, AccountLedger(wallet_balance=initial_balance_usdc), updated_at=now
        )
        for pos in initial_positions:
            repo.upsert_current_position(conn, run_id, pos, updated_at=now)
            repo.insert_run_seed_position(conn, run_id, pos)


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
            # Same warn-never-block breadcrumb as post_fill / initialize_run: funding
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


# --------------------------------------------------------------------------
# Accounting replay (spec §5; live basis phase3-spec §15)
# --------------------------------------------------------------------------


# The §15 fold below has one arm per adjustment type, and the registry decides
# what types exist. Bound at import, like the live-side vocabulary guards: the
# runtime raise in the fold is the belt, but it only fires mid-posting (inside a
# live wallet write) or mid-replay — after the type is already in the store.
# This fires before any of that, when the registry itself gains a member.
_FOLDED_ADJUSTMENT_TYPES = frozenset({"fee", "funding", "realized_pnl"})
if _FOLDED_ADJUSTMENT_TYPES != repo.ACCOUNTING_ADJUSTMENT_TYPES:
    raise AssertionError(
        "adjustment_ledger_delta's arms drifted from repository.ACCOUNTING_ADJUSTMENT_TYPES"
    )


class LedgerDeltas(NamedTuple):
    """One §15 correction's ledger movement, slot by NAME.

    All four slots are Decimals, so a bare tuple would let a producer/consumer
    transposition (a fee posted as funding) pass the type checker — and pass the
    §5 replay comparison too, because replay folds the SAME function and would
    drift identically. Construct and consume these by field name.
    """

    wallet: Decimal
    realized: Decimal
    fees: Decimal
    funding: Decimal


def adjustment_ledger_delta(
    adjustment_type: str,
    old_value: Decimal | None,
    new_value: Decimal | None,
) -> LedgerDeltas:
    """The ``(wallet, realized, fees, funding)`` deltas one §15 correction folds.

    A correction moves the ledger by ``new_value - old_value`` (a missing ``None``
    reads as ``0`` — the amount before/after the correction), routed by type:

    - ``fee`` — a learned/backfilled fee REDUCES the wallet and RAISES total fees;
    - ``funding`` — a funding settlement moves the wallet and net funding by its
      signed pnl;
    - ``realized_pnl`` — a realized-PnL correction moves the wallet and realized.

    This is the one definition of what an adjustment event *means* to the books,
    shared by the wallet posting (``backfill_*`` in :mod:`...live.fills`) and by
    live replay's fold below, so the two can never drift. Pinned to
    ``DECIMAL_CONTEXT`` so the fold rounds exactly like the posting did.
    """
    check_enum(adjustment_type, repo.ACCOUNTING_ADJUSTMENT_TYPES, name="adjustment_type")
    with localcontext(DECIMAL_CONTEXT):
        delta = (Decimal(0) if new_value is None else new_value) - (
            Decimal(0) if old_value is None else old_value
        )
        zero = Decimal(0)
        if adjustment_type == "fee":
            return LedgerDeltas(wallet=-delta, realized=zero, fees=delta, funding=zero)
        if adjustment_type == "funding":
            return LedgerDeltas(wallet=delta, realized=zero, fees=zero, funding=delta)
        if adjustment_type == "realized_pnl":
            return LedgerDeltas(wallet=delta, realized=delta, fees=zero, funding=zero)
        # The belt to that import-time brace. Not a bare `else`: check_enum
        # above validates MEMBERSHIP, so a new
        # adjustment type added to the registry passes it and would land in the
        # realized_pnl arm — wrong wallet direction, wrong realized, wrong fees.
        # And because this one definition feeds BOTH the live wallet posting and
        # replay's fold, the books would move wrong and replay would agree with
        # itself: validate's account_replay_mismatch_count reads 0 and the
        # corruption never surfaces. Fail loud instead.
        raise AssertionError(
            f"adjustment type {adjustment_type!r} is in "
            "repository.ACCOUNTING_ADJUSTMENT_TYPES but has no fold arm here"
        )


def _fold_posted_funding(
    conn: sqlite3.Connection, run_id: str, ledger: AccountLedger
) -> AccountLedger:
    """Fold every posted funding settlement into the ledger's wallet / net funding.

    Shared by both replay modes: funding is settled identically (a posted
    ``funding_events`` row carries its full basis) whether the run is paper or
    live, so the fold lives once here. Taking and returning the ledger type
    itself keeps the money slots named end to end — there is no bare tuple for
    a producer/consumer transposition to hide in.
    """
    wallet = ledger.wallet_balance
    net_funding = ledger.net_funding_pnl
    for event in repo.iter_funding_events(conn, run_id, status="posted"):
        pnl = funding_pnl(
            Decimal(event["position_size"]) * Decimal(event["mark_price"]),
            Decimal(event["funding_rate"]),
        )
        wallet += pnl
        net_funding += pnl
    return replace(ledger, wallet_balance=wallet, net_funding_pnl=net_funding)


def _fold_adjustments(
    conn: sqlite3.Connection, run_id: str, ledger: AccountLedger
) -> AccountLedger:
    """Fold every §15 accounting correction into the replayed ledger.

    Live-only: the recorded fill / funding rows are immutable, so a fee or
    funding learned after the fact lives ONLY in an ``accounting_adjustment_events``
    row (§15.1 rule 4 — never a silent overwrite). Replay must therefore fold
    these deltas to reproduce the materialized state a ``backfill_*`` call left.
    Each event moves the running totals one at a time — the same per-correction
    association the posting side used — so materialized and replayed ledgers
    stay equal by construction, not just numerically.
    """
    wallet = ledger.wallet_balance
    realized = ledger.realized_pnl
    fees = ledger.total_fees
    net_funding = ledger.net_funding_pnl
    for adj in repo.iter_accounting_adjustment_events(conn, run_id):
        deltas = adjustment_ledger_delta(
            adj["adjustment_type"], _dec(adj["old_value"]), _dec(adj["new_value"])
        )
        wallet += deltas.wallet
        realized += deltas.realized
        fees += deltas.fees
        net_funding += deltas.funding
    return AccountLedger(
        wallet_balance=wallet,
        realized_pnl=realized,
        total_fees=fees,
        net_funding_pnl=net_funding,
    )


@dataclass(frozen=True)
class ReplayResult:
    """Positions + ledger rebuilt from committed events, vs the materialized state.

    ``position_mismatches`` names each symbol whose replayed state differs from
    ``current_positions``; ``account_matches`` is the ledger comparison.
    ``is_consistent`` is the spec §5 acceptance signal (both must agree).
    """

    positions: Mapping[str, PositionState]
    ledger: AccountLedger
    position_mismatches: tuple[str, ...]
    account_matches: bool

    def __post_init__(self) -> None:
        # frozen=True only blocks attribute reassignment; wrap the mapping so
        # the rebuilt-from-committed-events result is deep-immutable like its
        # sibling ``position_mismatches`` tuple — a caller mutating
        # ``result.positions`` would silently corrupt the §5 acceptance signal.
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))

    @property
    def is_consistent(self) -> bool:
        return not self.position_mismatches and self.account_matches

    @property
    def mismatch_detail(self) -> str:
        """One canonical rendering of what mismatched — every reporter (restart
        refusal, mid-run halt, breadcrumb) formats through here so the texts
        stay grep-compatible."""
        return (
            f"position mismatches {list(self.position_mismatches)!r}, "
            f"account_matches={self.account_matches}"
        )


def replay(db: Database, *, run_id: str) -> ReplayResult:
    """Rebuild positions/ledger from committed fills + posted funding; compare to current.

    The genesis — opening balance and seed positions — is read from the run's own
    committed rows (``runs.initial_balance_usdc`` + ``run_seed_positions``), not
    taken from the caller: replay must trust nothing but the DB (phase2-data §1),
    and a config edited after run creation would otherwise shift the baseline and
    misreport (or mask) a mismatch.

    Recomputes purely from each event's *basis* fields — a fill's ``(side, qty,
    price, fee_rate)``, a posted funding's ``(position_size, mark_price,
    funding_rate)`` — never the stored derived columns (per-row realized/fee,
    ``funding_pnl``), so corruption of a derived column or a materialized
    ``current_*`` row surfaces as a mismatch rather than being trusted.
    Deterministic: the same committed events always rebuild the same state (the
    decimal context is pinned, so an ambient-precision change cannot perturb
    the arithmetic). All reads run in one snapshot (``read_transaction``), so a
    write committed mid-replay cannot fabricate — or mask — a mismatch.
    """
    with db.read_transaction() as conn:
        return replay_within(conn, run_id=run_id)


def replay_within(conn: sqlite3.Connection, *, run_id: str) -> ReplayResult:
    """The replay core, on a caller-owned read snapshot (semantics of :func:`replay`).

    Exposed so a consumer combining replay with its *own* reads (the spec §5
    validator) can pin everything to one snapshot — two back-to-back
    ``read_transaction``\\ s racing a live writer would otherwise mix two points
    in time into one report.
    """
    run = repo.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"run {run_id!r} does not exist; nothing to replay")

    # Fold-order invariant: the live ledger accrued its deltas chronologically
    # interleaved (each post_fill/record_funding did wallet += delta), while
    # replay sums all fills then all funding. Decimal addition at prec=28 is
    # only order-sensitive when operands span more than 28 significant figures;
    # USDC balances and per-event deltas never do, so both fold orders yield the
    # identical wallet and the bit-for-bit equality check below holds exactly.
    with localcontext(DECIMAL_CONTEXT):
        positions: dict[str, PositionState] = {
            p.coin: p for p in repo.get_run_seed_positions(conn, run_id)
        }
        wallet = Decimal(run["initial_balance_usdc"])
        realized_total = Decimal(0)
        total_fees = Decimal(0)

        # A run is single-mode (runs.mode), so which fill math to trust is a
        # per-run choice, never a per-fill one: a paper run recomputes fee /
        # realized from the modelled (side, qty, price, fee_rate); a live run
        # takes them from the exchange as recorded on the fill (§15). Live also
        # folds the §15 accounting adjustments, which paper never has.
        live = run["mode"] == "live"
        # Live fills fold in EXCHANGE-time order, not insertion order — the two
        # differ whenever a reconnect backfill posts fills older than ones the
        # socket already delivered, and entry price is order-dependent. This is the
        # same order the ingester applies them in; see repo.iter_fills.
        for fill in repo.iter_fills(conn, run_id, chronological=live):
            symbol = fill["symbol"]
            current = positions.get(symbol) or PositionState.flat(symbol)
            if live:
                # AS-RECORDED: a pending-fee fill posted 0 at ingest, and its
                # correction is a folded adjustment below — never read here. The
                # NULL→0 pending placeholder is repo.posted_exchange_fee's single
                # definition (its docstring explains why a private copy of that
                # "0" here would let the replayed and materialized ledgers drift).
                effect: FillEffect | LiveFillEffect = compute_live_fill_effect(
                    current,
                    side=fill["side"],
                    qty=Decimal(fill["fill_qty"]),
                    price=Decimal(fill["fill_price"]),
                    exchange_fee=repo.posted_exchange_fee(_dec(fill["exchange_fee"])),
                    exchange_closed_pnl=repo.require_live_fill_basis(fill),
                )
            else:
                effect = compute_fill_effect(
                    current,
                    side=fill["side"],
                    qty=Decimal(fill["fill_qty"]),
                    price=Decimal(fill["fill_price"]),
                    fee_rate=Decimal(fill["fee_rate"]),
                )
            positions[symbol] = effect.position
            wallet += effect.wallet_delta
            realized_total += effect.realized_pnl_delta
            total_fees += effect.fee

        ledger = AccountLedger(
            wallet_balance=wallet,
            realized_pnl=realized_total,
            total_fees=total_fees,
            net_funding_pnl=Decimal(0),
        )
        # From the stored settlement basis, symmetric with the fill loop above —
        # the write boundary guarantees a posted funding row carries all three
        # basis fields. Same in both modes.
        ledger = _fold_posted_funding(conn, run_id, ledger)
        if live:
            ledger = _fold_adjustments(conn, run_id, ledger)

    # Compare to the materialized state over the union of symbols.
    materialized = {p.coin: p for p in repo.get_all_current_positions(conn, run_id)}
    mismatches = tuple(
        sorted(
            symbol
            for symbol in set(positions) | set(materialized)
            if positions.get(symbol) != materialized.get(symbol)
        )
    )
    current_ledger = repo.get_current_account_state(conn, run_id)
    account_matches = current_ledger == ledger
    return ReplayResult(
        positions=positions,
        ledger=ledger,
        position_mismatches=mismatches,
        account_matches=account_matches,
    )
