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
   source is rejected, never double-counted. A redelivered duplicate is not merely
   dropped — its content is VERIFIED against the booked row (`_verify_redelivery`):
   a fee difference feeds the §15.1 correction lane (or, on another run's fill,
   a §12.3 fee-drift case — that ledger is not ours to move), an identity
   difference is recorded as a §12.3 case, and matching content (the common
   case) writes nothing.

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
fill and the dedupe key keeps it exactly-once. Every unmapped / malformed /
money-drift / fee-drift sighting also lands as an ``exchange_reconciliation_events`` row
(once per fact): the evidence FILE outlives the log, but only the DB row is a
queryable backlog once the fill ages out of every backfill window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

from ..exchanges.hyperliquid.errors import MalformedResponseError
from ..exchanges.hyperliquid.mapper import (
    HL_SIDE_TO_LOCAL,
    hex_identity_matches,
    require_decimal,
)
from ..paper.accounting import (
    LiveFillEffect,
    adjustment_ledger_delta,
    compute_live_fill_effect,
)
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import (
    accounting_adjustment_id,
    exchange_fill_key,
    live_fill_id,
    usable_fill_tid,
)
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

# Fill direction arrives in the shared HL bid/ask vocabulary (the mapper's
# ``HL_SIDE_TO_LOCAL``: "B" a buy on the bid, "A" a sell on the ask) and is
# mapped to the persistence Side words at the boundary, failing loud on
# anything else — a third code would otherwise be coerced by Side.parse into
# an opaque error far from here.

# Perp fees settle in USDC. A fee reported in any other token cannot be posted
# to the USDC ledger as-is, so it is treated as PENDING (§15.1) and left for a
# reconciliation job that can value it — never silently added as if it were USDC.
_FEE_TOKEN_USDC = "USDC"


# Fact keys for the two ENVELOPE-level faults, which are properties of the
# stream rather than of any one message (see ingest_message). Constants, so a
# condition that repeats at message cadence still records once — mirroring
# reconcile's _EQUITY_MISMATCH_FACT_KEY, and unlike the per-payload derivation
# in _malformed_key below, which is right for a single bad fill.
_ENVELOPE_WRONG_USER_FACT_KEY = "envelope-wrong-user"
_ENVELOPE_NO_FILLS_FACT_KEY = "envelope-no-fills-list"
# Reserved: _malformed_key must never DERIVE one of these from an untrusted
# payload, or the two would silently share one evidence file and one case row.
_RESERVED_FACT_KEYS = frozenset({_ENVELOPE_WRONG_USER_FACT_KEY, _ENVELOPE_NO_FILLS_FACT_KEY})


def _malformed_key(raw: Any) -> str:
    """A stable, COLLISION-FREE evidence key for a payload that would not parse.

    The raw payload's bare ``tid`` when it has one. NOT ``ids.exchange_fill_key``'s
    ``tid|<tid>`` form — a malformed payload's tid is untrusted and may violate that
    derivation's invariants (raising inside the evidence path), and no alignment is
    needed: the ``kind=`` prefix already namespaces these files apart from a parsed
    fill's. Otherwise a digest of the payload itself, because everything else on hand is
    ambiguous: two tid-less fills on one ``oid`` share an oid, and a non-dict payload has
    no fields at all. Since the evidence file is written once per key, an ambiguous key
    silently discards the second distinct payload — while the digest keeps a RE-sighting
    of the same payload (every backfill window re-delivers it) collapsing onto one file,
    which is the point of writing it once.
    """
    if isinstance(raw, dict):
        tid = raw.get("tid")
        # A malformed payload's tid is UNTRUSTED and stringifies to anything at
        # all, including one of the envelope fact keys above — and that string
        # collision would silently discard this payload's evidence (``once``
        # returns the existing file) and suppress its case row (the fact key is
        # already recorded), which is the precise silent loss the digest exists
        # to prevent. Reserved keys therefore fall through to the digest; a real
        # tid is an integer and never reaches this branch.
        if tid is not None and str(tid) not in ("", *_RESERVED_FACT_KEYS):
            return str(tid)
    return f"unparsed-{_payload_digest(raw)}"


def _payload_digest(obj: Any) -> str:
    """A short, stable content digest — the one derivation for every evidence key.

    Deriving a key is part of RECORDING EVIDENCE, which must never crash the
    drain (``_record_malformed``'s contract) — and a payload reaching here gets
    no benefit of the doubt that it will serialise, hence the ``repr`` fallback.
    Shared by ``_malformed_key`` and the drift-case key so the two truncated-digest
    derivations cannot drift apart.
    """
    try:
        canonical = json.dumps(obj, default=str, sort_keys=True)
    except (TypeError, ValueError):
        canonical = repr(obj)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _require(raw: Any, key: str) -> Any:
    """One required field out of a raw fill dict, or fail loud (§11.3)."""
    if not isinstance(raw, dict) or key not in raw or raw[key] is None:
        raise MalformedResponseError(f"fill payload is missing required field {key!r}: {raw!r}")
    return raw[key]


def _require_scalar_id(raw: Any, key: str) -> str | int:
    """A required id field as its documented wire type (str/int), or fail loud.

    ``oid`` / ``tid`` are ints on the wire and get ``str()``-coerced downstream —
    which is exactly why a non-scalar must be rejected HERE: ``str([123])``
    stringifies without error into an id that matches nothing, so a genuinely
    readable fill would be mislabeled as a §12.3 unmapped case (oid) or get a
    nonsense-but-permanent dedupe key (tid), instead of the malformed verdict the
    drift actually is. ``bool`` is excluded (an int subclass; ``"True"`` is not
    an id).
    """
    value = _require(raw, key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedResponseError(f"fill {key!r} must be a string or int, got {value!r}")
    return value


def _parse_epoch_ms(value: Any) -> datetime:
    """A fill's ``time`` (epoch ms) as an aware UTC datetime, or fail loud."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise MalformedResponseError(f"fill 'time' is not a valid epoch-ms int: {value!r}") from exc


def _parse_optional_fee(raw: dict) -> Decimal | None:
    """The fill's USDC fee, or ``None`` (pending) when absent / not proven USDC (§15.1).

    A fee reported in a non-USDC token cannot be posted to the USDC ledger, so it
    is left pending for a reconciliation job to value — never dropped silently,
    never coerced. A fee whose ``feeToken`` is MISSING rides the same pending lane:
    the field is documented as always present, so its absence is payload drift, and
    booking an amount we cannot prove is USDC risks recording a non-USDC number as
    USDC — pending merely defers the fee, never mis-books it. A present but
    unparseable ``fee`` is malformed and fails loud.
    """
    if raw.get("fee") is None:
        return None
    fee_token = raw.get("feeToken")
    if fee_token != _FEE_TOKEN_USDC:
        logger.warning(
            "fill fee token is %s — recording fee as pending for reconciliation",
            "absent" if fee_token is None else f"{fee_token!r}, not USDC",
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
    exchange_fill_id: str  # the exchange's tid as text; ``parse`` requires it (§14.2)
    fill_time: datetime
    fill_notional: Decimal
    exchange_fill_key: str
    raw: Any

    def __post_init__(self) -> None:
        # Parse-time invariants, enforced so a hand-built instance can't smuggle
        # a non-positive size/price into the fill math (which assumes both > 0).
        if not isinstance(self.coin, str) or not self.coin:
            # ``coin`` is a SQL bind parameter and the position-model key; a
            # non-str would surface as sqlite3.InterfaceError far from here.
            raise ValueError(f"ExchangeFill.coin must be a non-empty str, got {self.coin!r}")
        if self.qty <= 0:
            raise ValueError(f"ExchangeFill.qty must be > 0, got {self.qty}")
        if self.price <= 0:
            raise ValueError(f"ExchangeFill.price must be > 0, got {self.price}")
        if self.liquidity_role not in repo.LIVE_LIQUIDITY_ROLES:
            raise ValueError(
                f"ExchangeFill.liquidity_role must be maker/taker, got {self.liquidity_role!r}"
            )
        # ``fill_time`` is half of the §14.3 ordering key: it is compared against
        # ``newest_live_fill_order_key`` and sorted on in ``ingest_message``. A
        # naive datetime would fail those comparisons with an opaque TypeError far
        # from whoever built the instance — reject it here instead, the same
        # boundary discipline as ids._canonical_instant / fill_backfill._require_aware.
        # (``parse`` always supplies an aware UTC time via _parse_epoch_ms.)
        if self.fill_time.tzinfo is None:
            raise ValueError(
                f"ExchangeFill.fill_time must be timezone-aware, got {self.fill_time!r}"
            )
        # ``exchange_fill_key`` is derived state: §14.2 pins it to the same tid that
        # ``exchange_fill_id`` records. A hand-built instance whose key disagrees with
        # its id would dedupe under one identity while auditing another — the row's
        # advertised tid and its dedupe slot silently diverge, and no later read can
        # tell. (``parse`` derives both fields from the one tid, so it cannot trip this.)
        if self.exchange_fill_key != exchange_fill_key(tid=self.exchange_fill_id):
            raise ValueError(
                f"ExchangeFill.exchange_fill_key {self.exchange_fill_key!r} does not "
                f"match exchange_fill_id {self.exchange_fill_id!r} (§14.2: tid|<tid>)"
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
        if not isinstance(coin, str) or not coin:
            # An untyped coin would not crash here — it would flow to the symbol
            # comparison in _unmapped_reason, always mismatch, and file a genuinely
            # readable fill under "unmapped". That mislabels payload drift as a
            # §12.3 reconciliation case; drift is a MALFORMED payload and must say so.
            raise MalformedResponseError(f"fill 'coin' must be a non-empty string: {raw!r}")
        raw_side = _require(raw, "side")
        # isinstance BEFORE the membership test: ``in`` on a dict HASHES its operand,
        # so an unhashable payload value (["B"]) would raise TypeError — which neither
        # the §11.3 handlers nor the (ValueError, ArithmeticError) backstop below
        # catches, and one drifted fill would wedge the REST backfill forever.
        if not isinstance(raw_side, str) or raw_side not in HL_SIDE_TO_LOCAL:
            raise MalformedResponseError(
                f"fill side must be one of {sorted(HL_SIDE_TO_LOCAL)} (bid/ask), got {raw_side!r}"
            )
        price = require_decimal(_require(raw, "px"), field="fill px")
        qty = require_decimal(_require(raw, "sz"), field="fill sz")
        closed_pnl = require_decimal(_require(raw, "closedPnl"), field="fill closedPnl")
        exchange_order_id = str(_require_scalar_id(raw, "oid"))
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
        tid = _require_scalar_id(raw, "tid")
        # The scalar check above narrowed the wire type; the shared predicate
        # (ids.usable_fill_tid — the reconciliation cross-check keys on the
        # SAME definition) can now only fail on blankness.
        if not usable_fill_tid(tid):
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
                side=Side.parse(HL_SIDE_TO_LOCAL[raw_side]),
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
        for row in repo.iter_fills(conn, run_id, chronological=True, symbol=coin):
            recorded_fee = row["exchange_fee"]
            effect = compute_live_fill_effect(
                position,
                side=row["side"],
                qty=Decimal(row["fill_qty"]),
                price=Decimal(row["fill_price"]),
                exchange_fee=repo.posted_exchange_fee(
                    None if recorded_fee is None else Decimal(recorded_fee)
                ),
                exchange_closed_pnl=repo.require_live_fill_basis(row),
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
    ledger = repo.require_current_account_state(conn, run_id)

    # Read BEFORE the insert, so it is the newest fill already on the books, not this
    # one. A fill that sorts BEFORE it is out of order: the socket and the REST backfill
    # are two racing sources, and §12.3's re-ingest of a once-unmapped fill lands after
    # newer ones by design.
    #
    # Compared on the FOLD's key — (time, exchange_fill_key) — not on time alone. Two
    # fills can share a millisecond, and the fold breaks that tie on the key, so a fill
    # whose key sorts earlier belongs earlier in the fold even though its timestamp is
    # not older. Judged on time alone it would look in-order and be stacked on top, and
    # the materialized position would then disagree with the replayed one.
    newest_booked = repo.newest_live_fill_order_key(conn, run_id, fill.coin)
    out_of_order = (
        newest_booked is not None
        and (
            fill.fill_time,
            fill.exchange_fill_key,
        )
        < newest_booked
    )

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

    The one-way coupling (POSTED must name its correction) is enforced in
    ``__post_init__``, same family as ``IngestResult``/``FundingResult``.
    """

    outcome: BackfillOutcome
    adjustment_id: str | None
    fee: Decimal

    def __post_init__(self) -> None:
        if self.outcome is BackfillOutcome.POSTED and self.adjustment_id is None:
            raise ValueError(
                "BackfillResult: a POSTED outcome must name the adjustment it recorded; "
                "only ALREADY_POSTED may carry adjustment_id=None"
            )


def _effective_fee(fill_row: sqlite3.Row, fee_adjustments: list[sqlite3.Row]) -> Decimal:
    """The fee currently effective on a fill: as ingested, plus every correction.

    A pending fill posted ``0`` at ingest (``exchange_fee`` NULL), and each later
    correction moved it to its ``new_value``. The fill row itself is immutable, so
    the effective amount is the newest correction's ``new_value``, or — with no
    corrections — whatever was recorded at ingest. Both legs read the pending
    placeholder through :func:`repo.posted_exchange_fee`, the one definition of
    that "0" (its docstring explains why a private copy here would be a bug).
    """
    if fee_adjustments:
        latest = fee_adjustments[-1]["new_value"]
        return repo.posted_exchange_fee(None if latest is None else Decimal(latest))
    recorded = fill_row["exchange_fee"]
    return repo.posted_exchange_fee(None if recorded is None else Decimal(recorded))


class _FeeBooksState(NamedTuple):
    """``_fee_books_state``'s answer, slot by NAME.

    ``fee_adjustments`` (a list) and ``resolved`` (a bool) are only ever consumed
    in truthy contexts, so a bare-tuple transposition of the two would keep
    running with backwards pending/resolved logic instead of raising — named
    access is what makes that a visible error.
    """

    fee_adjustments: list[sqlite3.Row]
    effective: Decimal
    resolved: bool


def _fee_books_state(
    conn: sqlite3.Connection, run_id: str, fill_row: sqlite3.Row
) -> _FeeBooksState:
    """One definition of "do the books already carry this fee", for every lane.

    Returns the fill's fee-correction chain (insertion order), the fee currently
    effective on it, and whether that fee is RESOLVED — corrected at least once,
    or carried at ingest. A pending fill (``exchange_fee`` NULL, no correction)
    reads as effective 0 but UNRESOLVED: §15.1 rule 5's pending exception —
    learning even the placeholder amount is new information, and only an
    adjustment row takes the fill out of the rule-3 backlog.

    :func:`backfill_fill_fee` gates ``ALREADY_POSTED`` on this;
    ``_record_cross_run_fee_drift`` asks the same question read-only about
    another run's fill. A caller-side copy of this computation could silently
    drift from the posting lane (see ``_reconcile_redelivered_fee``'s "one
    definition" note) — which is why it lives here and nowhere else.
    """
    prior = repo.iter_accounting_adjustment_events(conn, run_id, target_id=fill_row["fill_id"])
    fee_adjustments = [a for a in prior if a["adjustment_type"] == "fee"]
    effective = _effective_fee(fill_row, fee_adjustments)
    resolved = bool(fee_adjustments) or fill_row["exchange_fee"] is not None
    return _FeeBooksState(fee_adjustments=fee_adjustments, effective=effective, resolved=resolved)


def backfill_fill_fee(
    db: Database,
    *,
    run_id: str,
    fill_id: str,
    exchange_fee: Decimal,
    fee_token: str,
    source: str | None = None,
    reason: str | None = None,
    timestamp: datetime | None = None,
) -> BackfillResult:
    """Post the §15.1 fee a fill did not carry at ingest — as an adjustment, exactly once.

    ``fee_token`` is the denomination PROOF, not metadata: anything but
    ``"USDC"`` is rejected, symmetric with ``_parse_optional_fee`` on the ingest
    side — §15.1 rule 3 explains why the obvious backfill job would otherwise
    mis-book a non-USDC amount. A caller resolving a non-USDC fee must VALUE it
    first and pass the resulting USDC figure.

    The recorded fill is immutable: a learned fee never overwrites it (§15.1 rule 4).
    It becomes one ``accounting_adjustment_events`` row — which live replay folds —
    and the ledger moves in the SAME transaction that records it, so a crash cannot
    post the fee without recording why.

    Corrections are CUMULATIVE, not one-shot. The ledger moves by the delta from the
    fee currently effective on the fill to the one being learned, which makes this
    both idempotent and re-correctable:

    - re-learning the SAME amount moves nothing and records nothing (``ALREADY_POSTED``)
      — a reconciliation job may call this on every pass. The one exception is the
      FIRST resolution of a still-pending fee: even a learned fee of exactly 0 (the
      placeholder amount) writes an adjustment row, because that row is what takes
      the fill out of the §15.1-rule-3 pending backlog (the ledger moves by 0);
    - learning a DIFFERENT amount (a referral discount, a late rebate, an exchange fee
      correction) posts the difference as the next correction in the sequence. Refusing
      it — as a one-correction-per-target key would — would not just lose the amount, it
      would wedge the job that keeps re-hitting that fill.

    The delta is routed through ``adjustment_ledger_delta``, the same definition live
    replay folds with, so the materialized ledger and the replayed one cannot drift.
    A non-finite fee is rejected outright (a NaN would poison the ledger irreversibly).
    """
    if fee_token != _FEE_TOKEN_USDC:
        raise ValueError(
            f"backfilled fee must be proven USDC, got fee_token={fee_token!r}; "
            "value the amount first and pass the USDC figure (§15.1 rule 3)"
        )
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

        books = _fee_books_state(conn, run_id, row)
        chain = books.fee_adjustments
        if exchange_fee == books.effective and books.resolved:
            # The books already carry exactly this fee. Report what is actually
            # recorded, not the argument we were handed.
            last_id = chain[-1]["adjustment_id"] if chain else None
            return BackfillResult(BackfillOutcome.ALREADY_POSTED, last_id, books.effective)

        adjustment_id = accounting_adjustment_id(run_id, "fee", fill_id, seq=len(chain))
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
            old_value=books.effective,
            new_value=exchange_fee,
            reason=reason,
            source=source,
            timestamp=now,
        )
        deltas = adjustment_ledger_delta("fee", books.effective, exchange_fee)
        ledger = repo.require_current_account_state(conn, run_id)
        with localcontext(DECIMAL_CONTEXT):
            new_ledger = AccountLedger(
                wallet_balance=ledger.wallet_balance + deltas.wallet,
                realized_pnl=ledger.realized_pnl + deltas.realized,
                total_fees=ledger.total_fees + deltas.fees,
                net_funding_pnl=ledger.net_funding_pnl + deltas.funding,
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
        wallet_address: str | None = None,
    ) -> None:
        # ``wallet_address`` arms ingest_message's envelope-identity check (the
        # WS ``userFills`` envelope names the wallet it is for). ``None`` skips
        # it — identity-agnostic tests — and production wiring passes the
        # signed client's wallet (both cli.py sites, 2026-08-17).
        self._db = db
        self._run_id = run_id
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()
        self._wallet_address = wallet_address

    def ingest(self, raw_fill: Any, *, fill: ExchangeFill | None = None) -> IngestResult:
        """Apply one raw fill (parse may raise ``MalformedResponseError`` — §11.3).

        ``fill`` lets a caller that has ALREADY parsed the payload pass the result
        in rather than paying for a second parse — :meth:`ingest_message` parses its
        whole batch up front so it can order it by exchange time before applying any
        of it. It must be the parse of ``raw_fill``; nothing else is a valid pairing,
        and the pairing is ENFORCED (by identity, below): a mismatched pair would
        apply one fill's money while recording the OTHER fill's payload as its
        evidence, and both the dedupe key and the audit trail would look internally
        consistent. The mismatch is a caller-contract violation, so it raises a
        plain ``ValueError`` — deliberately NOT ``MalformedResponseError``, which
        the §11.3 skip-one-and-continue handlers would swallow as a payload defect.
        """
        if fill is None:
            fill = ExchangeFill.parse(raw_fill)
        elif fill.raw is not raw_fill:
            raise ValueError(
                "ingest(fill=...) must be the parse of raw_fill — the passed fill "
                f"was parsed from a different payload (dedupe key {fill.exchange_fill_key})"
            )

        # Dedupe pre-check: cheap, and lets the caller distinguish "already had it"
        # from "new" without racing the UNIQUE constraint. The constraint is still
        # the authority — a concurrent insert would surface as IntegrityError below.
        booked = repo.get_fill_by_exchange_key(self._db.conn, fill.exchange_fill_key)
        if booked is not None:
            return self._verify_redelivery(fill, booked)

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
            raw_path = write_raw_payload(
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
            # The DB row is what makes the sighting a BACKLOG, not just evidence:
            # once this fill ages out of every backfill window (trailing 6h, floor
            # and gap obligations all retired), no path re-fetches it — the payload
            # file and the log line are then unreachable by any query, and this row
            # is the one durable, queryable record that exchange money exists which
            # the books do not carry (PR 4's §12.3 discovery reads it). Fail-loud:
            # a store that cannot record the case is the same infrastructure
            # failure as one that cannot record a fill.
            self._record_case(
                case_type="fill_unmapped",
                exchange_value=fill.exchange_fill_key,
                symbol=fill.coin,
                detail={
                    "reason": unmapped_reason,
                    "exchange_order_id": fill.exchange_order_id,
                    "exchange_fill_time": fill.fill_time.isoformat(),
                    "payload_path": raw_path,
                },
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
            booked = repo.get_fill_by_exchange_key(self._db.conn, fill.exchange_fill_key)
            if booked is None:
                raise
            return self._verify_redelivery(fill, booked)
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

    def _verify_redelivery(self, fill: ExchangeFill, booked: sqlite3.Row) -> IngestResult:
        """One DUPLICATE verdict, with the redelivered payload's content VERIFIED.

        The heartbeat re-fetches the trailing window every pass, so an already-booked
        fill is re-delivered on a schedule — which makes the redelivery the one free,
        automatic channel through which the exchange can tell us a booked amount
        changed (§15.1 rule 5's referral discounts / late rebates / fee corrections).
        Matching content is the overwhelmingly common case and returns without a
        write. Two kinds of mismatch are handled asymmetrically:

        - a FEE difference is exchange-authoritative new information with a built
          correction path: it is posted through :func:`backfill_fill_fee` (cumulative,
          idempotent, replay-folded) — never by touching the immutable fill row;
        - any IDENTITY difference (size, price, side, symbol, closedPnl, liquidity
          role) means "same tid, different fill" — there is no lane that can re-book
          money already folded into the position, so it is recorded as evidence + a
          §12.3 case row and NOT applied. The fee is deliberately not auto-posted on
          top of an identity mismatch: a payload that disagrees about WHICH fill this
          is cannot be trusted about what the fill cost.

        A fill booked under ANOTHER run is not this run's ledger to correct
        (:func:`backfill_fill_fee` would rightly refuse), so nothing is ever
        posted for it — but a mismatch is still recorded either way: identity
        drift through `_record_money_drift`, a fee-only difference through
        `_record_cross_run_fee_drift`. Without the latter, an exchange fee
        correction on a finished run's fill would vanish silently (the fee lane
        cannot post, and fee is excluded from `_money_drift`), leaving the
        predecessor run's frozen books wrong with no breadcrumb.
        """
        drift = self._money_drift(fill, booked)
        if drift:
            self._record_money_drift(fill, booked, drift)
        elif booked["run_id"] == self._run_id:
            self._reconcile_redelivered_fee(fill, booked)
        else:
            self._record_cross_run_fee_drift(fill, booked)
        return IngestResult(IngestOutcome.DUPLICATE, fill)

    @staticmethod
    def _money_drift(fill: ExchangeFill, booked: sqlite3.Row) -> dict[str, tuple[str, str]]:
        """Identity fields where the redelivered payload contradicts the booked row.

        ``{field: (booked, redelivered)}``, empty when they agree. The fee is NOT
        compared here — it has its own correction lane (`_reconcile_redelivered_fee`);
        these are the fields with no lane, where a mismatch can only be evidence.
        Decimal comparison is numeric (the row stores canonical text), so a
        formatting-only difference ("1.0" vs "1.00") is not drift.
        """
        booked_pnl = booked["exchange_closed_pnl"]
        pairs: dict[str, tuple[object, object]] = {
            "side": (booked["side"], fill.side.value),
            "coin": (booked["symbol"], fill.coin),
            "sz": (Decimal(booked["fill_qty"]), fill.qty),
            "px": (Decimal(booked["fill_price"]), fill.price),
            "closedPnl": (
                None if booked_pnl is None else Decimal(booked_pnl),
                fill.closed_pnl,
            ),
            "liquidity_role": (booked["liquidity_role"], fill.liquidity_role),
        }
        return {
            field: (str(recorded), str(redelivered))
            for field, (recorded, redelivered) in pairs.items()
            if recorded != redelivered
        }

    def _record_money_drift(
        self, fill: ExchangeFill, booked: sqlite3.Row, drift: dict[str, tuple[str, str]]
    ) -> None:
        """Persist a same-tid-different-money sighting: evidence file + §12.3 case row.

        Keyed on the fill key PLUS a digest of the drift itself: the same drifted
        payload is re-delivered every pass (dedupe to one record), while a SECOND,
        different drift on the same fill is new evidence and gets its own record.
        """
        case_key = f"{fill.exchange_fill_key}|{_payload_digest(drift)}"
        logger.warning(
            "redelivered fill %s contradicts the booked row on %s — recorded, not applied "
            "(a booked fill's identity is immutable; left for §12.3 reconciliation)",
            fill.exchange_fill_key,
            sorted(drift),
        )
        raw_path = write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill_money_drift",
            key=case_key,
            payload={"drift": drift, "raw": fill.raw},
            now=self._clock.now(),
            once=True,
        )
        self._record_case(
            case_type="fill_money_drift",
            exchange_value=case_key,
            symbol=fill.coin,
            local_value=booked["fill_id"],
            detail={"drift": drift, "payload_path": raw_path},
        )

    def _reconcile_redelivered_fee(self, fill: ExchangeFill, booked: sqlite3.Row) -> None:
        """Post a redelivered fee that differs from the books (§15.1 rule 5's feeder).

        Ordered cheapest-first, because this runs for every duplicate on every pass:

        1. no USDC-proven fee on the redelivery — nothing to learn (a pending fee
           stays pending; §15.1 rule 2);
        2. the redelivery agrees with the AS-INGESTED fee — no new information from
           the exchange, no queries. Deliberately compared against the ingested
           column, not the corrections-folded effective fee: a manual valuation
           (say, of a non-USDC rebate) must not be flip-flopped back by the same
           stale-but-identical payload arriving on every heartbeat;
        3. otherwise delegate to :func:`backfill_fill_fee` — whose OWN idempotence
           decides between posting and ``ALREADY_POSTED`` (the corrections already
           carry this amount: silent). One definition of "do the books already carry
           this fee", not a caller-side copy that could drift from it; the cost is a
           no-op transaction per pass for the RARE already-corrected fill still
           inside the window.
        """
        if fill.fee is None:
            return
        recorded = booked["exchange_fee"]
        if recorded is not None and Decimal(recorded) == fill.fee:
            return
        result = backfill_fill_fee(
            self._db,
            run_id=self._run_id,
            fill_id=booked["fill_id"],
            exchange_fee=fill.fee,
            fee_token=_FEE_TOKEN_USDC,
            source="live_fill_redelivery",
            reason=f"redelivered payload carries fee {fill.fee}",
            timestamp=self._clock.now(),
        )
        if result.outcome is BackfillOutcome.POSTED:
            logger.info(
                "fee correction from redelivered fill %s: posted %s",
                fill.exchange_fill_key,
                result.fee,
            )

    def _record_cross_run_fee_drift(self, fill: ExchangeFill, booked: sqlite3.Row) -> None:
        """A fee difference on ANOTHER run's fill: breadcrumb only, nothing posted.

        The same-run lane routes a redelivered fee through :func:`backfill_fill_fee`;
        a fill booked under another run has no lane at all (that run's ledger is not
        ours to move), so without this recorder an exchange fee correction landing
        after a run ends would vanish silently — the predecessor run's frozen books
        stay wrong and nothing anywhere says so. Mirrors the same-run lane's NET
        semantics gate for gate:

        1. no USDC-proven fee on the redelivery — nothing to learn;
        2. agrees with the AS-INGESTED fee — the ordinary redelivery, no news;
        3. agrees with the corrections-folded EFFECTIVE fee of a RESOLVED fill —
           the other run already carries this amount (the same question
           `backfill_fill_fee` answers with ``ALREADY_POSTED``); recording it
           would plant a breadcrumb known to be false. An UNRESOLVED pending
           fill is never silenced by this gate — same as the rule-5 pending
           exception, a fee equal to the placeholder 0 is still new information;
        4. otherwise: evidence file + §12.3 ``fill_fee_drift`` case row, keyed on
           the fill key plus a digest of ``(effective, redelivered)`` — the same
           stale payload every pass dedupes to one row, a later different
           correction is new evidence.
        """
        if fill.fee is None:
            return
        recorded = booked["exchange_fee"]
        if recorded is not None and Decimal(recorded) == fill.fee:
            return
        books = _fee_books_state(self._db.conn, booked["run_id"], booked)
        effective = books.effective
        if books.resolved and fill.fee == effective:
            return
        fee_change = (str(effective), str(fill.fee))
        case_key = f"{fill.exchange_fill_key}|{_payload_digest({'fee': fee_change})}"
        logger.warning(
            "redelivered fill %s carries fee %s but is booked under run %r "
            "(effective fee %s) — recorded, not posted (another run's ledger; "
            "left for §12.3 reconciliation)",
            fill.exchange_fill_key,
            fill.fee,
            booked["run_id"],
            effective,
        )
        raw_path = write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill_fee_drift",
            key=case_key,
            payload={"fee": fee_change, "raw": fill.raw},
            now=self._clock.now(),
            once=True,
        )
        self._record_case(
            case_type="fill_fee_drift",
            exchange_value=case_key,
            symbol=fill.coin,
            local_value=booked["fill_id"],
            detail={
                "booked_run_id": booked["run_id"],
                "as_ingested": recorded,
                "effective": str(effective),
                "redelivered": str(fill.fee),
                "payload_path": raw_path,
            },
        )

    def _record_case(
        self,
        *,
        case_type: str,
        exchange_value: str,
        symbol: str | None = None,
        local_value: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Record one §12.3 case row, once per fact (see repository docstrings).

        The steady state is the RE-sighting (every backfill pass re-observes the
        same unbooked fill), so the already-recorded check runs first as a pure
        read on the plain connection — no write lock, no detail serialisation.
        The insert itself re-checks inside its transaction (the once-per-fact
        guard lives at the write boundary), so this pre-check is purely a fast
        path, not the guarantee.

        Opens its own transaction on first sighting, so it must be called OUTSIDE
        any open unit of work — true of every call site (the unmapped lane, the
        malformed recorder and the two drift recorders all run before/after the
        fill transaction, never inside it).
        """
        if repo.has_exchange_reconciliation_case(
            self._db.conn, self._run_id, case_type=case_type, exchange_value=exchange_value
        ):
            return
        with self._db.transaction() as conn:
            repo.insert_exchange_reconciliation_event(
                conn,
                run_id=self._run_id,
                trigger="live_fill_ingest",
                case_type=case_type,
                symbol=symbol,
                local_value=local_value,
                exchange_value=exchange_value,
                detail=None if detail is None else json.dumps(detail, sort_keys=True),
                timestamp=self._clock.now(),
            )

    def ingest_message(self, message: Any) -> list[IngestResult]:
        """Ingest every fill in one drained WS message (§11.3 parse-fail handling).

        A ``userFills`` message carries ``data.fills`` — a list of fill dicts. Each
        is ingested independently; a malformed one records its raw payload + the
        error and is SKIPPED (§11.3 — never applied), so one bad fill neither
        crashes the drain nor blocks its well-formed siblings. Non-``userFills``
        messages (orderUpdates / clearinghouse) have no consumer yet — PR 4/5
        reconciliation is REST-based; their consumer is PR 6's live-socket
        rework — so they return ``[]`` here, drained and ignored to keep the
        queue from growing unbounded.

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
        # Envelope identity (2026-08-17): the WS envelope names the wallet it is
        # for, and a mismatch means these are ANOTHER wallet's fills — a
        # subscription mix-up. Not one of them may be applied, but the drain
        # must survive, so it is recorded as malformed evidence and skipped
        # (§11.3), like the no-fills-list envelope below. Checked only when the
        # key is PRESENT: the REST backfill reuses this path through a synthetic
        # envelope that carries no ``user`` — there the identity lives in the
        # by-wallet request itself (decision 2026-08-17).
        if self._wallet_address is not None and isinstance(data, dict) and "user" in data:
            envelope_user = data["user"]
            if not hex_identity_matches(envelope_user, self._wallet_address):
                # Keyed on the FACT, not the message: §11.3's evidence is
                # written once per fact, and the fact is "this stream is not
                # serving our wallet". A crossed subscription repeats at fill
                # cadence with different fills every time, so the derived key
                # (a digest, there being no ``tid`` on an envelope) would mint a
                # fresh evidence file AND a fresh blocking fill_malformed case
                # per message, without bound — the exact growth that key exists
                # to prevent, and every such case needs its own human stamp
                # before the run can be clean again. A CONSTANT rather than the
                # offending address: which wrong wallet it is belongs in the
                # message (it is there), while an untrusted value would reach a
                # filename and reopen the cardinality hole if the stream flips
                # between several. The recorded payload is the header alone —
                # enough to prove the fact, and another wallet's fill data has
                # no business on our disk.
                #
                # Two costs, taken deliberately. A stream that later serves a
                # DIFFERENT stranger adds no second file and no second case: the
                # durable backlog names the first address, the rest live only in
                # the log. And because the fills themselves are not kept, one
                # cannot tell after the fact whether a mislabeled envelope was
                # carrying our own fills. Both are the price of one stampable
                # case per fault, which is what keeps the run recoverable.
                self._record_malformed(
                    {"channel": message.get("channel"), "data": {"user": envelope_user}},
                    f"userFills envelope carries user {envelope_user!r}, expected "
                    f"{self._wallet_address!r} — refusing another wallet's fills",
                    key=_ENVELOPE_WRONG_USER_FACT_KEY,
                )
                return []
        fills = data.get("fills") if isinstance(data, dict) else None
        if not isinstance(fills, list):
            # A userFills envelope whose payload is not a fills list is itself
            # malformed — record it and skip, the same §11.3 discipline a bad fill
            # gets, rather than raising into the drain loop.
            #
            # Keyed on the fact for the same reason as the wallet branch above:
            # "envelopes on this channel carry no fills list" is a schema drift,
            # a property of the stream and not of one message, and it repeats at
            # message cadence with a different body each time. The derived key
            # would digest each of those differently and mint an unbounded run
            # of evidence files and blocking cases — and an envelope-level case
            # can never resolve itself (reconcile counts it unresolved until a
            # human stamps it), so the cardinality decides whether the run is
            # recoverable at all. Unlike the wallet branch this records the WHOLE
            # message: the drifted shape is the diagnosis, it is our own
            # channel's data, and ``once`` keeps the first one that carried it.
            self._record_malformed(
                message,
                "userFills message has no fills list",
                key=_ENVELOPE_NO_FILLS_FACT_KEY,
            )
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

    def _record_malformed(self, raw: Any, error: str, *, key: str | None = None) -> None:
        """Persist a malformed event's raw payload + error, applying nothing (§11.3).

        ``key`` overrides the derived one for a caller whose FACT is not the
        payload. The default derivation assumes one bad payload = one fact,
        which holds for a fill; it does not for an ENVELOPE-level fault, where
        one persistent stream property (wrong wallet, no fills list) re-arrives
        with different contents at message cadence and would digest differently
        every time. Those callers name their fact, and the evidence still keeps
        the first payload that carried it (2026-08-17 identity-echo review).

        The whole point of §11.3 is that a payload we could not parse is still
        EVIDENCE — dropped silently, a feed/format change would be invisible until
        the books drifted. ``write_raw_payload`` is itself fail-soft (a failed write
        warns, never raises), so recording the evidence can never turn a skipped
        fill into a crashed drain.

        Written ONCE per fill, like the unmapped path and for the same reason: a
        malformed fill is never inserted either, so it sits inside every subsequent
        REST window and is re-reported on every backfill pass. One file per sighting
        would grow without bound for as long as the exchange kept sending it.

        Which makes the KEY load-bearing: ``once`` keeps the first payload per key, so
        two distinct payloads sharing a key would collapse onto one file and the second
        would be lost — and a malformed fill has no reliable id by definition (that is
        often WHY it is malformed). A content digest is therefore folded in whenever the
        ``tid`` is missing, so distinct evidence stays distinct while a re-sighting of
        the SAME payload still dedupes.
        """
        key = _malformed_key(raw) if key is None else key
        logger.warning("skipping malformed live fill (%s): %r", error, raw)
        raw_path = write_raw_payload(
            payload_dir=self._payload_dir,
            kind="fill_parse_error",
            key=key,
            payload={"error": error, "raw": raw},
            now=self._clock.now(),
            once=True,
        )
        try:
            # The same durable backlog row the unmapped lane writes, and for the same
            # reason: once the payload ages out of every backfill window, this row is
            # the only queryable trace that the exchange sent money-shaped data we
            # never booked. Fail-SOFT here, unlike the unmapped lane: this recorder's
            # contract is that a STORAGE failure never crashes the drain, and the
            # §11.3 skip must stand even when the store cannot take the breadcrumb.
            # (Only storage: an invalid case_type/trigger literal raises ValueError
            # past this catch — a programming error here, not a store failure.)
            self._record_case(
                case_type="fill_malformed",
                exchange_value=key,
                detail={"error": error, "payload_path": raw_path},
            )
        except sqlite3.Error as exc:
            logger.error("failed to record malformed-fill case row for %s: %s", key, exc)
