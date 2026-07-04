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

:func:`replay` rebuilds positions and the ledger from the committed fills and
posted funding alone and compares them to the materialized ``current_*`` tables,
reporting any mismatch (spec §5 accounting-replay acceptance check).

Ledger convention (execution §6.1/§6.5): ``wallet_balance`` already includes
realized PnL, fees and funding, so ``account_equity = wallet_balance +
total_unrealized_pnl`` — nothing is double-counted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..domains.perp.margin import MarginSchedule
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import funding_event_id
from ..persistence.models import AccountLedger, PositionState

__all__ = [
    "AccountMetrics",
    "FillEffect",
    "FundingResult",
    "PositionValuation",
    "ReplayResult",
    "account_equity",
    "available_balance",
    "compute_fill_effect",
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
    "summarize_account",
    "unrealized_pnl",
    "used_initial_margin",
]

_BUY = "buy"
_SELL = "sell"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Pure formulas (execution §6)
# --------------------------------------------------------------------------


def position_notional(size: Decimal, mark_price: Decimal) -> Decimal:
    """``abs(size * mark_price)`` — a single position's notional (§6.1)."""
    return abs(size * mark_price)


def unrealized_pnl(size: Decimal, mark_price: Decimal, entry_price: Decimal) -> Decimal:
    """``size * (mark - entry)`` — signed, long positive on a rise (§6.3)."""
    return size * (mark_price - entry_price)


def account_equity(wallet_balance: Decimal, total_unrealized_pnl: Decimal) -> Decimal:
    """``wallet_balance + total_unrealized_pnl`` (§6.1)."""
    return wallet_balance + total_unrealized_pnl


def available_balance(equity: Decimal, used_initial_margin_total: Decimal) -> Decimal:
    """``account_equity - used_initial_margin`` (§6.1)."""
    return equity - used_initial_margin_total


def effective_leverage(total_position_notional: Decimal, equity: Decimal) -> Decimal:
    """``total_notional / equity`` (§6.1); ``0`` on a non-positive equity.

    A non-positive equity means a margin-called / liquidatable account; reporting
    ``0`` (rather than dividing) keeps the snapshot writable — the liquidation
    model, not this ratio, is what flags the danger.
    """
    if equity <= 0:
        return Decimal(0)
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


def compute_fill_effect(
    position: PositionState,
    *,
    side: str,
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
    if side not in (_BUY, _SELL):
        raise ValueError(f"fill side must be {_BUY!r} or {_SELL!r}, got {side!r}")
    if qty <= 0:
        raise ValueError(f"fill qty must be > 0, got {qty}")
    if price <= 0:
        raise ValueError(f"fill price must be > 0, got {price}")
    if fee_rate < 0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")

    signed_fill = qty if side == _BUY else -qty
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
# Account aggregation (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionValuation:
    """A live position plus the data needed to value it: mark and margin schedule."""

    position: PositionState
    mark_price: Decimal
    schedule: MarginSchedule


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
    effective_leverage: Decimal
    margin_ratio: Decimal | None


def summarize_account(
    ledger: AccountLedger,
    valuations: Sequence[PositionValuation],
    *,
    leverage: Decimal,
) -> AccountMetrics:
    """Aggregate the account-level metrics from the ledger and open positions."""
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
        total_unrealized += unrealized_pnl(v.position.size, v.mark_price, v.position.entry_price)
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
    """
    now = created_at or _utcnow()
    with db.transaction() as conn:
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


def post_fill(
    db: Database,
    *,
    run_id: str,
    mode: str,
    fill_id: str,
    order_id: str,
    symbol: str,
    side: str,
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
    """
    now = timestamp or _utcnow()
    with db.transaction() as conn:
        position = repo.get_current_position(conn, run_id, symbol) or PositionState.flat(symbol)
        ledger = repo.get_current_account_state(conn, run_id)
        if ledger is None:
            raise ValueError(f"run {run_id!r} has no account state; call initialize_run first")

        effect = compute_fill_effect(position, side=side, qty=qty, price=price, fee_rate=fee_rate)

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
        repo.upsert_current_account_state(
            conn,
            run_id,
            AccountLedger(
                wallet_balance=ledger.wallet_balance + effect.wallet_delta,
                realized_pnl=ledger.realized_pnl + effect.realized_pnl_delta,
                total_fees=ledger.total_fees + effect.fee,
                net_funding_pnl=ledger.net_funding_pnl,
            ),
            updated_at=now,
        )
    return effect


@dataclass(frozen=True)
class FundingResult:
    """Outcome of a funding settlement attempt (exactly-once)."""

    status: str  # "posted" | "pending" | "already_posted"
    funding_event_id: str
    funding_pnl: Decimal | None


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
      move; a later call with the backfilled rate transitions it to ``posted``.
    - already ``posted`` → no-op (a retry never double-posts).

    The wallet moves only on the transition into ``posted``, inside the same
    transaction that flips the status, so a crash cannot post twice.
    """
    # Canonicalise the settlement instant so the exactly-once key is stable: a
    # naive-vs-aware (or non-UTC-offset) representation of the same hour would
    # otherwise derive a different id and dedup key and let funding post twice.
    if funding_timestamp.tzinfo is None:
        raise ValueError("funding_timestamp must be timezone-aware (UTC)")
    funding_timestamp = funding_timestamp.astimezone(timezone.utc)

    fe_id = funding_event_id(run_id, symbol, funding_timestamp.isoformat())
    now = recorded_at or _utcnow()
    with db.transaction() as conn:
        existing = repo.get_funding_event(conn, fe_id)
        if existing is not None and existing["status"] == "posted":
            return FundingResult("already_posted", fe_id, None)

        # Rate still unavailable: keep/record a pending event, never fabricate a
        # rate (execution §6.5). Wallet untouched.
        if funding_rate is None:
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
        # pending backfill those were captured on the pending row when the hour
        # elapsed — use them, not the caller's current values, so a position
        # change between pending and backfill can't post funding on the wrong
        # notional (the caller may no longer know the historical size). A fresh
        # live posting (no pending row) uses the caller's settlement-time values.
        if existing is None:
            basis_size, basis_mark = position_size, mark_price
        else:
            basis_size = Decimal(existing["position_size"])
            stored_mark = existing["mark_price"]
            basis_mark = Decimal(stored_mark) if stored_mark is not None else mark_price
        if basis_mark is None:
            raise ValueError("a mark_price is required to post funding with a known rate")

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
            )

        ledger = repo.get_current_account_state(conn, run_id)
        if ledger is None:
            raise ValueError(f"run {run_id!r} has no account state; call initialize_run first")
        repo.upsert_current_account_state(
            conn,
            run_id,
            AccountLedger(
                wallet_balance=ledger.wallet_balance + pnl,
                realized_pnl=ledger.realized_pnl,
                total_fees=ledger.total_fees,
                net_funding_pnl=ledger.net_funding_pnl + pnl,
            ),
            updated_at=now,
        )
    return FundingResult("posted", fe_id, pnl)


# --------------------------------------------------------------------------
# Accounting replay (spec §5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """Positions + ledger rebuilt from committed events, vs the materialized state.

    ``position_mismatches`` names each symbol whose replayed state differs from
    ``current_positions``; ``account_matches`` is the ledger comparison.
    ``is_consistent`` is the spec §5 acceptance signal (both must agree).
    """

    positions: dict[str, PositionState]
    ledger: AccountLedger
    position_mismatches: tuple[str, ...]
    account_matches: bool

    @property
    def is_consistent(self) -> bool:
        return not self.position_mismatches and self.account_matches


def replay(
    db: Database,
    *,
    run_id: str,
    initial_balance: Decimal,
    initial_positions: Iterable[PositionState] = (),
) -> ReplayResult:
    """Rebuild positions/ledger from committed fills + posted funding; compare to current.

    Recomputes purely from each fill's ``(side, qty, price, fee_rate)`` and the
    posted funding events, independent of the stored per-row realized/fee — so a
    corrupted materialized ``current_*`` row surfaces as a mismatch rather than
    being trusted. Deterministic: the same committed events always rebuild the
    same state.
    """
    positions: dict[str, PositionState] = {p.coin: p for p in initial_positions}
    wallet = initial_balance
    realized_total = Decimal(0)
    total_fees = Decimal(0)

    conn = db.conn
    for fill in repo.iter_fills(conn, run_id):
        symbol = fill["symbol"]
        current = positions.get(symbol) or PositionState.flat(symbol)
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

    net_funding = Decimal(0)
    for event in repo.iter_funding_events(conn, run_id, status="posted"):
        pnl = Decimal(event["funding_pnl"])
        wallet += pnl
        net_funding += pnl

    ledger = AccountLedger(
        wallet_balance=wallet,
        realized_pnl=realized_total,
        total_fees=total_fees,
        net_funding_pnl=net_funding,
    )

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
