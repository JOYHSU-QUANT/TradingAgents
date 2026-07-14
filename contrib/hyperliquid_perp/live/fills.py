"""Live fill ingestion: parse, dedupe, and post exchange fills (phase3-spec §14/§15).

Phase 2 fills were locally simulated; a live fill is an exchange fact and its
money is exchange-authoritative (§15). This module owns three things:

1. :class:`ExchangeFill` — the parsed, validated shape of one raw Hyperliquid
   fill (WS ``userFills`` / REST ``userFillsByTime`` / ``orderStatus`` all speak
   the same fill dict), carrying its §14.2 ``exchange_fill_key`` dedupe key. A
   malformed payload raises :class:`MalformedResponseError`, so the WS/REST layer
   records the raw event and does NOT apply it (§11.3).

2. :func:`apply_live_fill` / :func:`post_live_fill` — post one fill atomically
   (§14.3): the fill row, the exchange fee / closedPnl, and the ``current_*``
   position + account update commit in ONE transaction, or none of it does. The
   UNIQUE ``exchange_fill_key`` makes it exactly-once: the SAME fill from a second
   source is rejected, never double-counted.

3. :func:`backfill_fill_fee` — the §15.1 fee-pending correction. A fill that
   arrived without a fee posts ``0`` at ingest; when the fee is later learned the
   correction is an ``accounting_adjustment_events`` row (never an overwrite of
   the immutable fill), which moves the wallet and which live replay folds.

:class:`LiveFillProcessor` ties the parse → dedupe → resolve-order → post pipeline
together for one run; the WS drain loop and the REST backfill both feed it.

A fill is applied only when its exchange order id maps to a known bot order
(``get_order_by_exchange_order_id``). A fill for an unknown order is a §12.3
reconciliation case (PR 4): it is logged and left, not force-applied — recording
a fill with no owning order would corrupt the bot's local position model. Once
§8.3 recovery records the order's exchange id, a REST backfill re-ingests the
fill and the dedupe key keeps it exactly-once.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.errors import MalformedResponseError
from ..exchanges.hyperliquid.mapper import require_decimal
from ..paper.accounting import (
    LiveFillEffect,
    adjustment_ledger_delta,
    compute_live_fill_effect,
)
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import accounting_adjustment_id, exchange_fill_key, live_fill_id
from ..persistence.models import DECIMAL_CONTEXT, AccountLedger, PositionState, Side
from .payloads import write_raw_payload
from .ws_stream import USER_FILLS_CHANNEL

__all__ = [
    "BackfillOutcome",
    "BackfillResult",
    "ExchangeFill",
    "IngestOutcome",
    "IngestResult",
    "LiveFillProcessor",
    "apply_live_fill",
    "backfill_fill_fee",
    "post_live_fill",
]

logger = logging.getLogger(__name__)

# Hyperliquid encodes fill direction as bid/ask, not buy/sell: "B" is a fill on
# the bid (a buy), "A" a fill on the ask (a sell). Mapped to the persistence
# Side vocabulary at the boundary, failing loud on anything else — a third code
# would otherwise be coerced by Side.parse into an opaque error far from here.
_HL_SIDE = {"B": "buy", "A": "sell"}

# Perp fees settle in USDC. A fee reported in any other token cannot be posted
# to the USDC ledger as-is, so it is treated as PENDING (§15.1) and left for a
# reconciliation job that can value it — never silently added as if it were USDC.
_FEE_TOKEN_USDC = "USDC"


def _require(raw: Any, key: str) -> Any:
    """One required field out of a raw fill dict, or fail loud (§11.3)."""
    if not isinstance(raw, dict) or key not in raw or raw[key] is None:
        raise MalformedResponseError(f"fill payload is missing required field {key!r}: {raw!r}")
    return raw[key]


def _parse_epoch_ms(value: Any) -> datetime:
    """A fill's ``time`` (epoch ms) as an aware UTC datetime, or fail loud."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise MalformedResponseError(f"fill 'time' is not a valid epoch-ms int: {value!r}") from exc


def _parse_optional_fee(raw: dict) -> Decimal | None:
    """The fill's USDC fee, or ``None`` (pending) when absent / non-USDC (§15.1).

    A fee reported in a non-USDC token cannot be posted to the USDC ledger, so it
    is left pending for a reconciliation job to value — never dropped silently,
    never coerced. A present but unparseable ``fee`` is malformed and fails loud.
    """
    if raw.get("fee") is None:
        return None
    fee_token = raw.get("feeToken")
    if fee_token is not None and fee_token != _FEE_TOKEN_USDC:
        logger.warning(
            "fill fee is in %r, not USDC — recording fee as pending for reconciliation",
            fee_token,
        )
        return None
    return require_decimal(raw["fee"], field="fill fee")


@dataclass(frozen=True)
class ExchangeFill:
    """One parsed Hyperliquid fill — the exchange-authoritative accounting basis.

    ``fee`` is ``None`` when the fill carried no (USDC) fee: the fill is still
    posted, with the fee PENDING (§15.1), and the amount is backfilled later.
    ``closed_pnl`` is always present (it arrives with the fill). ``exchange_fill_key``
    is the §14.2 dedupe key — the exchange's own ``tid``, which every source
    reports for the same fill, so it is stable across WS, REST and orderStatus.
    """

    coin: str
    side: Side
    qty: Decimal
    price: Decimal
    closed_pnl: Decimal
    fee: Decimal | None
    liquidity_role: str
    exchange_order_id: str
    exchange_fill_id: str | None
    fill_time: datetime
    fill_notional: Decimal
    exchange_fill_key: str
    raw: Any

    def __post_init__(self) -> None:
        # Parse-time invariants, enforced so a hand-built instance can't smuggle
        # a non-positive size/price into the fill math (which assumes both > 0).
        if self.qty <= 0:
            raise ValueError(f"ExchangeFill.qty must be > 0, got {self.qty}")
        if self.price <= 0:
            raise ValueError(f"ExchangeFill.price must be > 0, got {self.price}")
        if self.liquidity_role not in repo.LIVE_LIQUIDITY_ROLES:
            raise ValueError(
                f"ExchangeFill.liquidity_role must be maker/taker, got {self.liquidity_role!r}"
            )

    @classmethod
    def parse(cls, raw: Any) -> ExchangeFill:
        """Parse one raw Hyperliquid fill dict; raise ``MalformedResponseError`` on bad shape.

        The raised error routes the WS/REST layer to record the raw payload and
        skip application (§11.3) — never to apply a fill it could not fully read.

        EVERY rejection of an exchange payload leaves here as ``MalformedResponseError``
        and nothing else. The §11.3 skip-one-and-continue handler keys on that class,
        so a rejection raised as anything else — a bare ``ValueError`` from a
        constructor invariant or from an id helper — would escape that handler and
        abort the whole batch: on the WS path the already-drained siblings are lost,
        and on the REST path the same fill re-arrives in every window and wedges the
        backfill forever. The value checks below therefore run BEFORE the constructor,
        and the construction itself is wrapped, so the two exception vocabularies
        cannot drift apart again.
        """
        if not isinstance(raw, dict):
            raise MalformedResponseError(f"fill payload is not a dict: {raw!r}")
        coin = _require(raw, "coin")
        raw_side = _require(raw, "side")
        if raw_side not in _HL_SIDE:
            raise MalformedResponseError(
                f"fill side must be one of {sorted(_HL_SIDE)} (bid/ask), got {raw_side!r}"
            )
        price = require_decimal(_require(raw, "px"), field="fill px")
        qty = require_decimal(_require(raw, "sz"), field="fill sz")
        closed_pnl = require_decimal(_require(raw, "closedPnl"), field="fill closedPnl")
        exchange_order_id = str(_require(raw, "oid"))
        fill_time = _parse_epoch_ms(_require(raw, "time"))

        # ``crossed`` = taker (this order crossed the book); its absence is a
        # malformed fill, not a defaulted maker — guessing the liquidity role
        # would misrecord the audit trail.
        crossed = _require(raw, "crossed")
        if not isinstance(crossed, bool):
            raise MalformedResponseError(f"fill 'crossed' must be a bool, got {crossed!r}")
        liquidity_role = "taker" if crossed else "maker"

        # §14.2: the dedupe key IS the exchange's stable fill id. Hyperliquid
        # sends ``tid`` on every fill (required in the SDK's Fill type, on both
        # the WS stream and the REST endpoint), so a fill without one is an
        # anomaly — not a case for falling back on a composite key, which can
        # collide two genuinely distinct fills and silently drop one (see
        # ids.exchange_fill_key). Treat it as malformed: §11.3 records the raw
        # payload and the fill is NOT applied, surfacing for reconciliation
        # instead of being miscounted.
        tid = _require(raw, "tid")
        if str(tid) == "":
            raise MalformedResponseError(f"fill 'tid' must be non-empty (§14.2): {raw!r}")

        # The fill math assumes a strictly positive size and price. The exchange
        # should never send otherwise, but a zero/negative one is a PAYLOAD defect,
        # not an impossible internal state — so it is rejected in the malformed
        # vocabulary (recorded + skipped), not as the ValueError the constructor
        # invariant below would raise. See the class docstring: a ValueError here
        # would escape §11.3 and take the whole batch down with it.
        if qty <= 0:
            raise MalformedResponseError(f"fill 'sz' must be > 0, got {qty}: {raw!r}")
        if price <= 0:
            raise MalformedResponseError(f"fill 'px' must be > 0, got {price}: {raw!r}")

        fee = _parse_optional_fee(raw)
        try:
            # Inside the guard: DECIMAL_CONTEXT keeps the arithmetic traps armed, and
            # an absurd-but-finite exponent ("px": "1e1000000") clears require_decimal's
            # NaN/Inf check and then raises decimal.Overflow here. That is an
            # ArithmeticError, not a ValueError — so the backstop below catches BOTH,
            # or a payload could still escape §11.3 through the arithmetic door.
            with localcontext(DECIMAL_CONTEXT):
                fill_notional = abs(qty * price)
            key = exchange_fill_key(tid=tid)
            return cls(
                coin=coin,
                side=Side.parse(_HL_SIDE[raw_side]),
                qty=qty,
                price=price,
                closed_pnl=closed_pnl,
                fee=fee,
                liquidity_role=liquidity_role,
                exchange_order_id=exchange_order_id,
                exchange_fill_id=str(tid),
                fill_time=fill_time,
                fill_notional=fill_notional,
                exchange_fill_key=key,
                raw=raw,
            )
        except (ValueError, ArithmeticError) as exc:
            # Backstop, so the two exception vocabularies cannot drift apart again:
            # every check above already rejects in the malformed vocabulary, but a
            # future invariant added to __post_init__ (or to an id helper), or a new
            # piece of Decimal arithmetic, would otherwise silently re-open the §11.3
            # escape hatch this guards.
            raise MalformedResponseError(
                f"fill payload violates a fill invariant: {raw!r}"
            ) from exc


# --------------------------------------------------------------------------
# Transactional posting (§14.3)
# --------------------------------------------------------------------------


def _require_ledger(conn: sqlite3.Connection, run_id: str) -> AccountLedger:
    ledger = repo.get_current_account_state(conn, run_id)
    if ledger is None:
        raise ValueError(f"run {run_id!r} has no account state; call initialize_run first")
    return ledger


def _rebuild_position(conn: sqlite3.Connection, run_id: str, coin: str) -> PositionState:
    """Re-fold one symbol's live fills in exchange-time order, from the run's genesis.

    The repair for a fill that arrives OUT OF ORDER. Position is the one piece of
    state that does not commute: size and the weighted-average entry price depend on
    the order the fills are folded in, so a fill applied incrementally on top of newer
    ones lands on the wrong running position and stays wrong forever.

    Folded exactly the way ``accounting.replay_within`` folds it — same genesis (the
    run's seed positions), same chronological order, same per-fill math — so the
    materialized position and the replayed one agree BY CONSTRUCTION rather than by
    coincidence. If they could drift, each would still be internally consistent, and
    the §5 check could not tell which was right.

    Per-symbol, because that is all replay's fold needs: positions are independent
    across symbols, so re-folding one cannot disturb another. The ledger is untouched
    — wallet, realized and fees are SUMS of per-fill deltas and commute, so an
    out-of-order fill leaves them already correct.
    """
    seeds = {p.coin: p for p in repo.get_run_seed_positions(conn, run_id)}
    position = seeds.get(coin) or PositionState.flat(coin)
    with localcontext(DECIMAL_CONTEXT):
        for row in repo.iter_fills(conn, run_id, chronological=True):
            if row["symbol"] != coin:
                continue
            recorded_fee = row["exchange_fee"]
            effect = compute_live_fill_effect(
                position,
                side=row["side"],
                qty=Decimal(row["fill_qty"]),
                price=Decimal(row["fill_price"]),
                exchange_fee=repo.posted_exchange_fee(
                    None if recorded_fee is None else Decimal(recorded_fee)
                ),
                exchange_closed_pnl=Decimal(row["exchange_closed_pnl"]),
            )
            position = effect.position
    return position


def apply_live_fill(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    fill: ExchangeFill,
    order_id: str,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    flip_leg: str | None = None,
    plan_id: str | None = None,
    slice_index: int | None = None,
    fill_reason: str | None = None,
    raw_exchange_payload_path: str | None = None,
    timestamp: datetime | None = None,
) -> LiveFillEffect:
    """Post one live fill on a **caller-owned** transaction (§14.3 atomicity).

    Reads the current position + ledger, computes the exchange-basis effect
    (pure), then writes the fill row and both materialized ``current_*`` rows in
    one unit. A duplicate ``exchange_fill_key`` raises ``sqlite3.IntegrityError``
    and rolls the whole unit back — the exactly-once guard across WS / REST /
    orderStatus. Exposed on a caller-owned conn so the PR 5 engine can bundle a
    fill's protection / plan writes into the same transaction; :func:`post_live_fill`
    is the standalone entry point that opens its own.
    """
    if not conn.in_transaction:
        raise ValueError(
            "apply_live_fill must run inside an open transaction (db.transaction()); "
            "an autocommit connection would break the fill's §14.3 atomicity contract"
        )
    now = timestamp or datetime.now(timezone.utc)
    position = repo.get_current_position(conn, run_id, fill.coin) or PositionState.flat(fill.coin)
    ledger = _require_ledger(conn, run_id)

    # Read BEFORE the insert, so it is the newest fill already on the books, not this
    # one. A fill older than that is out of order: the socket and the REST backfill are
    # two racing sources, and §12.3's re-ingest of a once-unmapped fill lands it after
    # newer ones by design.
    newest_booked = repo.last_live_fill_time(conn, run_id, symbol=fill.coin)
    out_of_order = newest_booked is not None and fill.fill_time < newest_booked

    posted_fee = repo.posted_exchange_fee(fill.fee)
    effect = compute_live_fill_effect(
        position,
        side=fill.side,
        qty=fill.qty,
        price=fill.price,
        exchange_fee=posted_fee,
        exchange_closed_pnl=fill.closed_pnl,
    )

    with localcontext(DECIMAL_CONTEXT):
        new_ledger = AccountLedger(
            wallet_balance=ledger.wallet_balance + effect.wallet_delta,
            realized_pnl=ledger.realized_pnl + effect.realized_pnl_delta,
            total_fees=ledger.total_fees + effect.fee,
            net_funding_pnl=ledger.net_funding_pnl,
        )
    if new_ledger.wallet_balance < 0:
        # Same warn-never-block breadcrumb as the paper accounting layer: a live
        # fill that drives the wallet negative is an anomaly worth a loud record,
        # but the exchange is the truth source (§15) and pre-trade risk is the
        # engine's job — the ingester never refuses a fill the exchange executed.
        logger.warning(
            "live fill %s leaves run %s with a negative wallet: %s",
            fill.exchange_fill_key,
            run_id,
            new_ledger.wallet_balance,
        )

    # Insert the fill first: its exchange_fill_key UNIQUE constraint is the
    # exactly-once guard, so a duplicate aborts before any state moves.
    repo.insert_live_fill(
        conn,
        fill_id=live_fill_id(run_id, fill.exchange_fill_key),
        run_id=run_id,
        order_id=order_id,
        symbol=fill.coin,
        side=fill.side,
        fill_qty=fill.qty,
        fill_price=fill.price,
        fill_notional=fill.fill_notional,
        exchange_fill_key=fill.exchange_fill_key,
        exchange_closed_pnl=fill.closed_pnl,
        liquidity_role=fill.liquidity_role,
        exchange_fee=fill.fee,
        exchange_fill_id=fill.exchange_fill_id,
        exchange_order_id=fill.exchange_order_id,
        cloid_logical=cloid_logical,
        cloid_hex=cloid_hex,
        exchange_fill_time=fill.fill_time,
        plan_id=plan_id,
        flip_leg=flip_leg,
        slice_index=slice_index,
        fill_reason=fill_reason,
        raw_exchange_payload_path=raw_exchange_payload_path,
        timestamp=now,
    )
    if out_of_order:
        # The incremental fold above stacked this fill on top of NEWER ones, so its
        # size / weighted-average entry price are wrong — and would stay wrong, since
        # nothing revisits a booked position. Re-fold this symbol from genesis in
        # exchange-time order, which is exactly how replay folds it, so the two agree
        # by construction. The ledger needs no repair: its three totals are sums of
        # per-fill deltas and commute (only the position does not).
        logger.info(
            "live fill %s (%s) is older than the newest booked fill for %s — re-folding "
            "the position in exchange-time order",
            fill.exchange_fill_key,
            fill.fill_time.isoformat(),
            fill.coin,
        )
        effect = replace(effect, position=_rebuild_position(conn, run_id, fill.coin))
    repo.upsert_current_position(conn, run_id, effect.position, updated_at=now)
    repo.upsert_current_account_state(conn, run_id, new_ledger, updated_at=now)
    return effect


def post_live_fill(
    db: Database,
    *,
    run_id: str,
    fill: ExchangeFill,
    order_id: str,
    cloid_logical: str | None = None,
    cloid_hex: str | None = None,
    flip_leg: str | None = None,
    plan_id: str | None = None,
    slice_index: int | None = None,
    fill_reason: str | None = None,
    raw_exchange_payload_path: str | None = None,
    timestamp: datetime | None = None,
) -> LiveFillEffect:
    """Post one live fill in its own transaction (standalone :func:`apply_live_fill`)."""
    with db.transaction() as conn:
        return apply_live_fill(
            conn,
            run_id=run_id,
            fill=fill,
            order_id=order_id,
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            flip_leg=flip_leg,
            plan_id=plan_id,
            slice_index=slice_index,
            fill_reason=fill_reason,
            raw_exchange_payload_path=raw_exchange_payload_path,
            timestamp=timestamp,
        )


# --------------------------------------------------------------------------
# Fee backfill (§15.1)
# --------------------------------------------------------------------------


class BackfillOutcome(str, Enum):
    """The verdict of one :func:`backfill_fill_fee` call."""

    POSTED = "posted"  # the fee moved: a correction was recorded and the wallet moved
    ALREADY_POSTED = "already_posted"  # the books already carry this fee — no-op


@dataclass(frozen=True)
class BackfillResult:
    """What the books now say — never merely what the caller asked for.

    ``fee`` is the fee EFFECTIVE on the fill after this call, and ``adjustment_id``
    is the correction this call recorded (``None`` on a no-op that recorded none).
    On ``ALREADY_POSTED`` these describe what was already there, so a caller that
    logs the result cannot report an amount the ledger never took.
    """

    outcome: BackfillOutcome
    adjustment_id: str | None
    fee: Decimal


def _effective_fee(fill_row: sqlite3.Row, fee_adjustments: list[sqlite3.Row]) -> Decimal:
    """The fee currently effective on a fill: as ingested, plus every correction.

    A pending fill posted ``0`` at ingest (``exchange_fee`` NULL), and each later
    correction moved it to its ``new_value``. The fill row itself is immutable, so
    the effective amount is the newest correction's ``new_value``, or — with no
    corrections — whatever was recorded at ingest.
    """
    if fee_adjustments:
        latest = fee_adjustments[-1]["new_value"]
        return Decimal(0) if latest is None else Decimal(latest)
    recorded = fill_row["exchange_fee"]
    return Decimal(0) if recorded is None else Decimal(recorded)


def backfill_fill_fee(
    db: Database,
    *,
    run_id: str,
    fill_id: str,
    exchange_fee: Decimal,
    source: str | None = None,
    reason: str | None = None,
    timestamp: datetime | None = None,
) -> BackfillResult:
    """Post the §15.1 fee a fill did not carry at ingest — as an adjustment, exactly once.

    The recorded fill is immutable: a learned fee never overwrites it (§15.1 rule 5).
    It becomes one ``accounting_adjustment_events`` row — which live replay folds —
    and the ledger moves in the SAME transaction that records it, so a crash cannot
    post the fee without recording why.

    Corrections are CUMULATIVE, not one-shot. The ledger moves by the delta from the
    fee currently effective on the fill to the one being learned, which makes this
    both idempotent and re-correctable:

    - re-learning the SAME amount moves nothing and records nothing (``ALREADY_POSTED``)
      — a reconciliation job may call this on every pass;
    - learning a DIFFERENT amount (a referral discount, a late rebate, an exchange fee
      correction) posts the difference as the next correction in the sequence. Refusing
      it — as a one-correction-per-target key would — would not just lose the amount, it
      would wedge the job that keeps re-hitting that fill.

    The delta is routed through ``adjustment_ledger_delta``, the same definition live
    replay folds with, so the materialized ledger and the replayed one cannot drift.
    A non-finite fee is rejected outright (a NaN would poison the ledger irreversibly).
    """
    if not exchange_fee.is_finite():
        raise ValueError(f"backfilled fee must be finite, got {exchange_fee}")
    now = timestamp or datetime.now(timezone.utc)
    with db.transaction() as conn:
        row = repo.get_fill(conn, fill_id)
        if row is None:
            raise ValueError(f"fill {fill_id!r} does not exist; cannot backfill its fee")
        if row["mode"] != "live":
            raise ValueError(
                f"fill {fill_id!r} is not a live fill; §15 fee backfill does not apply"
            )
        if row["run_id"] != run_id:
            # The ledger this moves is run_id's. A fill id from another run (they do
            # circulate — the oid→order lookup is wallet-scoped) would otherwise debit
            # the wrong run's wallet for a fee it never paid.
            raise ValueError(
                f"fill {fill_id!r} belongs to run {row['run_id']!r}, not {run_id!r}; "
                "refusing to move this run's ledger for another run's fill"
            )

        prior = repo.iter_accounting_adjustment_events(conn, run_id, target_id=fill_id)
        fee_adjustments = [a for a in prior if a["adjustment_type"] == "fee"]
        effective = _effective_fee(row, fee_adjustments)
        if exchange_fee == effective:
            # The books already carry exactly this fee. Report what is actually
            # recorded, not the argument we were handed.
            last_id = fee_adjustments[-1]["adjustment_id"] if fee_adjustments else None
            return BackfillResult(BackfillOutcome.ALREADY_POSTED, last_id, effective)

        adjustment_id = accounting_adjustment_id(run_id, "fee", fill_id, seq=len(fee_adjustments))
        repo.insert_accounting_adjustment_event(
            conn,
            adjustment_id=adjustment_id,
            run_id=run_id,
            adjustment_type="fee",
            target_table="fills",
            target_id=fill_id,
            field="exchange_fee",
            # The amount the books currently carry — 0 for a fill that ingested with
            # its fee pending, or the previous correction's amount. Replay folds
            # (new - old), so this pair IS the ledger movement, not a description of it.
            old_value=effective,
            new_value=exchange_fee,
            reason=reason,
            source=source,
            timestamp=now,
        )
        wallet_d, realized_d, fees_d, funding_d = adjustment_ledger_delta(
            "fee", effective, exchange_fee
        )
        ledger = _require_ledger(conn, run_id)
        with localcontext(DECIMAL_CONTEXT):
            new_ledger = AccountLedger(
                wallet_balance=ledger.wallet_balance + wallet_d,
                realized_pnl=ledger.realized_pnl + realized_d,
                total_fees=ledger.total_fees + fees_d,
                net_funding_pnl=ledger.net_funding_pnl + funding_d,
            )
        if new_ledger.wallet_balance < 0:
            logger.warning(
                "fee backfill for fill %s leaves run %s with a negative wallet: %s",
                fill_id,
                run_id,
                new_ledger.wallet_balance,
            )
        repo.upsert_current_account_state(conn, run_id, new_ledger, updated_at=now)
    return BackfillResult(BackfillOutcome.POSTED, adjustment_id, exchange_fee)


# --------------------------------------------------------------------------
# Ingestion pipeline
# --------------------------------------------------------------------------


class IngestOutcome(str, Enum):
    """What :meth:`LiveFillProcessor.ingest` did with one raw fill."""

    APPLIED = "applied"  # posted to the books for the first time
    DUPLICATE = "duplicate"  # already applied (dedupe key seen) — no-op
    UNMAPPED = "unmapped"  # no bot order for this oid — left for PR 4 reconciliation


@dataclass(frozen=True)
class IngestResult:
    """One fill's verdict. ``effect`` is present exactly when the fill was APPLIED.

    The coupling is enforced, not merely observed: PR 5's engine is expected to read
    "APPLIED implies there is an effect" (that is the whole point of the outcome), and
    a DUPLICATE/UNMAPPED result carrying an effect would be a fill counted twice. Same
    contract, and the same enforcement, as ``FundingResult``'s posted/pnl pairing.
    """

    outcome: IngestOutcome
    fill: ExchangeFill
    effect: LiveFillEffect | None = None

    def __post_init__(self) -> None:
        if (self.outcome is IngestOutcome.APPLIED) != (self.effect is not None):
            raise ValueError(
                f"IngestResult.effect must be present iff the fill was applied; got "
                f"outcome={self.outcome.value!r} with effect={self.effect!r}"
            )


class LiveFillProcessor:
    """Parse → dedupe → resolve-order → post, for one run's live fills.

    Fed by both the WS drain loop and the REST backfill; a fill applied from one
    source is a no-op from the other (the dedupe key). ``payload_dir`` mirrors the
    order path's ``payloads/<run_id>/`` — one JSON file per fill, path recorded on
    the row.
    """

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        payload_dir: Path,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()

    def ingest(self, raw_fill: Any, *, fill: ExchangeFill | None = None) -> IngestResult:
        """Apply one raw fill (parse may raise ``MalformedResponseError`` — §11.3).

        ``fill`` lets a caller that has ALREADY parsed the payload pass the result
        in rather than paying for a second parse — :meth:`ingest_message` parses its
        whole batch up front so it can order it by exchange time before applying any
        of it. It must be the parse of ``raw_fill``; nothing else is a valid pairing.
        """
        fill = fill if fill is not None else ExchangeFill.parse(raw_fill)

        # Dedupe pre-check: cheap, and lets the caller distinguish "already had it"
        # from "new" without racing the UNIQUE constraint. The constraint is still
        # the authority — a concurrent insert would surface as IntegrityError below.
        if repo.get_fill_by_exchange_key(self._db.conn, fill.exchange_fill_key) is not None:
            return IngestResult(IngestOutcome.DUPLICATE, fill)

        order = repo.get_order_by_exchange_order_id(self._db.conn, fill.exchange_order_id)
        unmapped_reason = (
            "no local bot order for this exchange order id"
            if order is None
            else self._unmapped_reason(fill, order)
        )
        if order is None or unmapped_reason is not None:
            # §12.3: a fill we cannot confidently attach to one of THIS run's orders
            # is a reconciliation case, not something to force-apply — there is no
            # owning order to attach it to, and inventing one would corrupt the local
            # position model. PR 4 reconciles it; once §8.3 recovery records the
            # order's exchange id, a REST backfill re-ingests this fill and the
            # dedupe key keeps it exactly-once.
            #
            # The raw payload is persisted, exactly as a malformed fill's is: this is
            # a fill the exchange really executed and whose money we did NOT book, so
            # it is evidence, and it must outlive both the log and the REST lookback
            # window that PR 4 might arrive after.
            logger.warning(
                "live fill %s (exchange order %s) not applied — %s; left for "
                "reconciliation (§12.3)",
                fill.exchange_fill_key,
                fill.exchange_order_id,
                unmapped_reason,
            )
            write_raw_payload(
                payload_dir=self._payload_dir,
                kind="fill_unmapped",
                key=fill.exchange_fill_key,
                payload={"reason": unmapped_reason, "raw": raw_fill},
                now=self._clock.now(),
                # Once per fill, not once per sighting. An unapplied fill is never
                # inserted, so the backfill cursor never advances past it and every
                # later pass re-fetches and re-reports the same fill — an unbounded
                # write of the same evidence, for as long as it stays unreconciled.
                once=True,
            )
            return IngestResult(IngestOutcome.UNMAPPED, fill)

        now = self._clock.now()
        raw_path = write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill",
            key=fill.exchange_fill_key,
            payload=raw_fill,
            now=now,
        )
        try:
            with self._db.transaction() as conn:
                effect = apply_live_fill(
                    conn,
                    run_id=self._run_id,
                    fill=fill,
                    order_id=order["order_id"],
                    cloid_logical=order["cloid_logical"],
                    cloid_hex=order["cloid_hex"],
                    flip_leg=order["flip_leg"],
                    raw_exchange_payload_path=raw_path,
                    timestamp=now,
                )
        except sqlite3.IntegrityError:
            # ONLY the dedupe key may be read as "already applied". IntegrityError
            # also covers NOT NULL / CHECK / FOREIGN KEY violations, and reporting
            # one of those as DUPLICATE would drop a real fill forever while the
            # counters claimed the exactly-once guard had held — the fill is never
            # posted, and every retry repeats the sequence (the pre-check finds no
            # row, the insert fails, it is called a duplicate again). So confirm the
            # row actually exists; if it does not, this was some OTHER constraint and
            # it must fail loud.
            if repo.get_fill_by_exchange_key(self._db.conn, fill.exchange_fill_key) is None:
                raise
            return IngestResult(IngestOutcome.DUPLICATE, fill)
        return IngestResult(IngestOutcome.APPLIED, fill, effect)

    def _unmapped_reason(self, fill: ExchangeFill, order: sqlite3.Row) -> str | None:
        """Why this fill cannot be attached to one of THIS run's orders, or ``None``.

        The oid→order lookup is deliberately wallet-scoped (an oid is globally unique
        at the exchange and is not run-qualified), so the row it returns still has to
        be checked against the run and the symbol before its money is booked here. A
        restart RESUMES its run id, so a fill resolving to ANOTHER run's order is a
        genuine anomaly, not the ordinary case — booking it would post the fill under
        this run's ledger while pointing ``order_id`` at a different run's order. A
        symbol mismatch means the mapping itself is wrong, and applying it would open
        a position in a coin we never traded. Both are recorded as evidence and left
        for §12.3 reconciliation rather than guessed at.
        """
        if order["run_id"] != self._run_id:
            return (
                f"exchange order belongs to run {order['run_id']!r}, not this run {self._run_id!r}"
            )
        if order["symbol"] != fill.coin:
            return (
                f"exchange order is for symbol {order['symbol']!r} but the fill is for "
                f"{fill.coin!r}"
            )
        return None

    def ingest_message(self, message: Any) -> list[IngestResult]:
        """Ingest every fill in one drained WS message (§11.3 parse-fail handling).

        A ``userFills`` message carries ``data.fills`` — a list of fill dicts. Each
        is ingested independently; a malformed one records its raw payload + the
        error and is SKIPPED (§11.3 — never applied), so one bad fill neither
        crashes the drain nor blocks its well-formed siblings. Non-``userFills``
        messages (orderUpdates / clearinghouse) belong to PR 4's reconciliation and
        return ``[]`` here — drained and ignored so the queue never grows unbounded.

        Fills are applied in EXCHANGE-TIME order, not arrival order. The money does
        not care (realized PnL comes per-fill from the exchange's ``closedPnl``), but
        the position's weighted-average ``entry_price`` does: a reconnect backfill
        that posts an older missed fill AFTER newer socket fills would compute the
        average against the wrong running position and be permanently wrong — and
        because replay folds live fills in the same exchange-time order, replay
        would faithfully reproduce the wrong number, so the §5 consistency check
        could never catch it. Entry price feeds unrealized PnL, equity, the
        liquidation estimate and the levels PR 5 anchors SL/TP to.

        ONLY ``MalformedResponseError`` is skipped, deliberately. Every rejection of
        an exchange payload is raised in that vocabulary (see ``ExchangeFill.parse``),
        so anything else reaching here — a ``ValueError`` from the repository's
        write-boundary identities or from an uninitialised run's missing ledger, a
        ``sqlite3`` error — is an impossible internal state or an infrastructure
        failure, NOT one bad fill. Those must fail loud: nothing was committed (the
        fill posts in one transaction), and the dedupe key makes the caller's retry
        safe. Do not widen this except clause to make a batch "more robust" — it
        would convert a broken run into a silently short ledger.
        """
        if not isinstance(message, dict) or message.get("channel") != USER_FILLS_CHANNEL:
            return []
        data = message.get("data")
        fills = data.get("fills") if isinstance(data, dict) else None
        if not isinstance(fills, list):
            # A userFills envelope whose payload is not a fills list is itself
            # malformed — record it and skip, the same §11.3 discipline a bad fill
            # gets, rather than raising into the drain loop.
            self._record_malformed(message, "userFills message has no fills list")
            return []

        # Parse first, so the batch can be ordered before ANY of it is applied; a
        # fill that will not parse is recorded and dropped here, exactly as before.
        parsed: list[tuple[ExchangeFill, Any]] = []
        for raw_fill in fills:
            try:
                parsed.append((ExchangeFill.parse(raw_fill), raw_fill))
            except MalformedResponseError as exc:
                self._record_malformed(raw_fill, str(exc))

        # Sort key = the DB's live-replay ORDER BY (exchange_fill_time, then the
        # fill id as TEXT). The two must agree or the materialized books and the
        # replayed books would fold the same fills in different orders. Ties (the
        # same coin, the same millisecond) only need to be broken DETERMINISTICALLY,
        # and both sides break them the same way.
        parsed.sort(key=lambda item: (item[0].fill_time, item[0].exchange_fill_key))
        return [self.ingest(raw_fill, fill=fill) for fill, raw_fill in parsed]

    def _record_malformed(self, raw: Any, error: str) -> None:
        """Persist a malformed event's raw payload + error, applying nothing (§11.3).

        The whole point of §11.3 is that a payload we could not parse is still
        EVIDENCE — dropped silently, a feed/format change would be invisible until
        the books drifted. ``write_raw_payload`` is itself fail-soft (a failed write
        warns, never raises), so recording the evidence can never turn a skipped
        fill into a crashed drain.

        Written ONCE per fill, like the unmapped path and for the same reason: a
        malformed fill is never inserted either, so it sits inside every subsequent
        REST window and is re-reported on every backfill pass. One file per sighting
        would grow without bound for as long as the exchange kept sending it.
        """
        key = "unparsed"
        if isinstance(raw, dict):
            key = str(raw.get("tid") or raw.get("oid") or "unparsed")
        logger.warning("skipping malformed live fill (%s): %r", error, raw)
        write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill_parse_error",
            key=key,
            payload={"error": error, "raw": raw},
            now=self._clock.now(),
            once=True,
        )
