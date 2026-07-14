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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.errors import MalformedResponseError
from ..exchanges.hyperliquid.mapper import require_decimal
from ..paper.accounting import LiveFillEffect, compute_live_fill_effect
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
        fee = _parse_optional_fee(raw)
        key = exchange_fill_key(tid=tid)
        with localcontext(DECIMAL_CONTEXT):
            fill_notional = abs(qty * price)
        return cls(
            coin=coin,
            side=Side.parse(_HL_SIDE[raw_side]),
            qty=qty,
            price=price,
            closed_pnl=closed_pnl,
            fee=fee,
            liquidity_role=liquidity_role,
            exchange_order_id=exchange_order_id,
            exchange_fill_id=None if tid is None else str(tid),
            fill_time=fill_time,
            fill_notional=fill_notional,
            exchange_fill_key=key,
            raw=raw,
        )


# --------------------------------------------------------------------------
# Transactional posting (§14.3)
# --------------------------------------------------------------------------


def _require_ledger(conn: sqlite3.Connection, run_id: str) -> AccountLedger:
    ledger = repo.get_current_account_state(conn, run_id)
    if ledger is None:
        raise ValueError(f"run {run_id!r} has no account state; call initialize_run first")
    return ledger


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

    posted_fee = Decimal(0) if fill.fee is None else fill.fee
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

    POSTED = "posted"  # the fee was learned and posted for the first time
    ALREADY_POSTED = "already_posted"  # a prior backfill already posted it (no-op)


@dataclass(frozen=True)
class BackfillResult:
    outcome: BackfillOutcome
    adjustment_id: str
    fee: Decimal


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
    """Post a §15.1 fee that was pending at ingest — as an adjustment, exactly once.

    The recorded fill is immutable: the learned fee never overwrites it (§15.1
    rule 5). Instead it becomes one ``accounting_adjustment_events`` row — which
    live replay folds — and the wallet is moved (``-fee``) and total fees raised
    (``+fee``) in the SAME transaction that records it, so a crash cannot post
    the fee without recording why. The deterministic ``adjustment_id`` makes a
    re-run a no-op: a reconciliation job that re-learns the same fee cannot post
    it twice.

    Refuses to touch a fill whose fee was NOT pending (``exchange_fee`` already
    recorded) — that is not a backfill, and silently re-posting would double the
    fee. A non-finite fee is rejected outright (a NaN would poison the ledger).
    """
    if not exchange_fee.is_finite():
        raise ValueError(f"backfilled fee must be finite, got {exchange_fee}")
    adjustment_id = accounting_adjustment_id(run_id, "fee", fill_id)
    now = timestamp or datetime.now(timezone.utc)
    with db.transaction() as conn:
        row = repo.get_fill(conn, fill_id)
        if row is None:
            raise ValueError(f"fill {fill_id!r} does not exist; cannot backfill its fee")
        if row["mode"] != "live":
            raise ValueError(
                f"fill {fill_id!r} is not a live fill; §15 fee backfill does not apply"
            )
        if row["exchange_fee"] is not None:
            raise ValueError(
                f"fill {fill_id!r} already recorded exchange_fee {row['exchange_fee']!r}; its "
                "fee was not pending — refusing to re-post (would double the fee)"
            )
        if repo.get_accounting_adjustment_event(conn, adjustment_id) is not None:
            # A prior backfill already posted this fee; the ledger already moved.
            return BackfillResult(BackfillOutcome.ALREADY_POSTED, adjustment_id, exchange_fee)

        repo.insert_accounting_adjustment_event(
            conn,
            adjustment_id=adjustment_id,
            run_id=run_id,
            adjustment_type="fee",
            target_table="fills",
            target_id=fill_id,
            field="exchange_fee",
            # old = the amount posted at ingest for a pending fill: 0.
            old_value=Decimal(0),
            new_value=exchange_fee,
            reason=reason,
            source=source,
            timestamp=now,
        )
        ledger = _require_ledger(conn, run_id)
        with localcontext(DECIMAL_CONTEXT):
            new_ledger = AccountLedger(
                wallet_balance=ledger.wallet_balance - exchange_fee,
                realized_pnl=ledger.realized_pnl,
                total_fees=ledger.total_fees + exchange_fee,
                net_funding_pnl=ledger.net_funding_pnl,
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
    outcome: IngestOutcome
    fill: ExchangeFill
    effect: LiveFillEffect | None = None


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

    def ingest(self, raw_fill: Any) -> IngestResult:
        """Apply one raw fill (parse may raise ``MalformedResponseError`` — §11.3)."""
        fill = ExchangeFill.parse(raw_fill)

        # Dedupe pre-check: cheap, and lets the caller distinguish "already had it"
        # from "new" without racing the UNIQUE constraint. The constraint is still
        # the authority — a concurrent insert would surface as IntegrityError below.
        if repo.get_fill_by_exchange_key(self._db.conn, fill.exchange_fill_key) is not None:
            return IngestResult(IngestOutcome.DUPLICATE, fill)

        order = repo.get_order_by_exchange_order_id(self._db.conn, fill.exchange_order_id)
        if order is None:
            # §12.3: a fill for an order we do not know is a reconciliation case,
            # not something to force-apply — there is no owning order to attach it
            # to, and inventing one would corrupt the local position model. PR 4
            # reconciles it; once §8.3 recovery records the order's exchange id, a
            # REST backfill re-ingests this fill and the dedupe key keeps it once.
            logger.warning(
                "live fill %s references exchange order %s with no local bot order — "
                "left for reconciliation (§12.3)",
                fill.exchange_fill_key,
                fill.exchange_order_id,
            )
            return IngestResult(IngestOutcome.UNMAPPED, fill)

        raw_path = write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill",
            key=fill.exchange_fill_key,
            payload=raw_fill,
            now=self._clock.now(),
        )
        now = self._clock.now()
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
            # The dedupe key rejected it between the pre-check and here (a fill
            # already applied) — the exactly-once guarantee held; report no-op.
            return IngestResult(IngestOutcome.DUPLICATE, fill)
        return IngestResult(IngestOutcome.APPLIED, fill, effect)

    def ingest_message(self, message: Any) -> list[IngestResult]:
        """Ingest every fill in one drained WS message (§11.3 parse-fail handling).

        A ``userFills`` message carries ``data.fills`` — a list of fill dicts. Each
        is ingested independently; a malformed one records its raw payload + the
        error and is SKIPPED (§11.3 — never applied), so one bad fill neither
        crashes the drain nor blocks its well-formed siblings. Non-``userFills``
        messages (orderUpdates / clearinghouse) belong to PR 4's reconciliation and
        return ``[]`` here — drained and ignored so the queue never grows unbounded.
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
        results: list[IngestResult] = []
        for raw_fill in fills:
            try:
                results.append(self.ingest(raw_fill))
            except MalformedResponseError as exc:
                self._record_malformed(raw_fill, str(exc))
        return results

    def _record_malformed(self, raw: Any, error: str) -> None:
        """Persist a malformed event's raw payload + error, applying nothing (§11.3).

        The whole point of §11.3 is that a payload we could not parse is still
        EVIDENCE — dropped silently, a feed/format change would be invisible until
        the books drifted. ``write_raw_payload`` is itself fail-soft (a failed write
        warns, never raises), so recording the evidence can never turn a skipped
        fill into a crashed drain.
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
        )
