"""Reading the two legs' stores — ``sqlite3`` only, and only reads.

The spot package may not be imported (its isolation test), so its store is
read as a file with plain queries; the perp store is read the same way for
symmetry, and because a handful of queries is all that is needed. Both are
opened read-only through the URI form (the perp package's own spelling of a
path as a URI, which a UNC share and a relative path both survive), so this
package cannot write to either even by mistake.

Two readers serve the coordinator's sizing (:func:`perp_equity`,
:func:`spot_equity`): the run's latest equity and the instant that row was
written at (carried into the handoff so a reader can see how old the sizing
is). The rest serve the report (:mod:`.report`), which reads each leg's
books over a span of UTC days: the perp run's posted funding, its fills'
fees, its cycle-end snapshots and how each decision cycle ended; the spot
run's decisions, valuations, fills and the targets it read. The columns:

- perp: ``account_snapshots.account_equity`` and ``timestamp`` of the
  run's latest snapshot (the paper engine writes one per cycle; the
  timestamp is the repository's ISO form, decoded by its paired decoder),
  joined for the report with the ``position_snapshots`` row of the same
  instant (written only while the run holds a position); ``funding_events``
  with ``status = 'posted'`` (``funding_pnl`` is income-positive, the
  ledger's sign); ``fills.fee``; ``decision_attempts`` joined to the
  ``ai_inputs`` row and the ``ai_outputs`` row each names (the model word
  and the rationale prefix tell a file-target cycle's kind apart);
- spot: ``valuations.total_value`` and ``time`` of the run's latest bar
  (the spot engine values the portfolio after every decision; ``time`` is
  the bar's epoch seconds), joined with ``runs.quote`` so a run valued in
  anything but a USD stable is refused rather than sized in the wrong unit;
  for the report also ``valuations.balances`` / ``prices`` (JSON objects
  of decimal text), ``decisions``, ``fills`` (amounts in token units, gas
  in ETH) and ``targets`` under the source the run's config snapshot names.

No row is :class:`StoreReadError`, not "unknown": a mistyped run id reads
exactly like a run too new to have a row, and sizing the spot leg as if
the two legs held equal capital on a typo must not pass in silence. The
operator who really has a brand-new run says so by leaving the store flags
out for that one visit. A missing file or a store without the table is the
same error.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Final

from .signal import decimal, instant_ms
from .upstream import (
    FILE_TARGET_MODEL,
    FILE_TARGET_PENDING_PREFIX,
    FILE_TARGET_UNUSABLE_PREFIX,
    AttemptStatus,
    RiskAction,
    sqlite_file_uri,
)

__all__ = [
    "USD_QUOTES",
    "CycleKind",
    "Equity",
    "PerpCycle",
    "PerpPosition",
    "PerpSnapshot",
    "SpotFill",
    "SpotRun",
    "SpotTarget",
    "SpotValuation",
    "StoreReadError",
    "perp_cycles",
    "perp_equity",
    "perp_fees",
    "perp_funding",
    "perp_pending_funding",
    "perp_snapshots",
    "spot_decisions",
    "spot_equity",
    "spot_fills",
    "spot_run",
    "spot_targets",
    "spot_valuations",
]

# The quote tokens a spot run may value itself in for its equity to be compared
# with the perp leg's USD equity. A run quoted in WETH would hand the sizing a
# number in the wrong unit that still looks like a weight.
USD_QUOTES: Final = frozenset({"USDC", "USDT", "DAI"})


class StoreReadError(RuntimeError):
    """The store could not be read as the kind of store it was named as."""


@dataclass(frozen=True)
class Equity:
    """A leg's equity in its quote currency, and when the store wrote that row."""

    value: Decimal
    at_ms: int


def _rows(
    path: Path, query: str, params: Sequence[object], *, leg: str
) -> list[tuple[object, ...]]:
    if not path.is_file():
        raise StoreReadError(f"{leg} store {path}: no such file")
    try:
        with closing(sqlite3.connect(f"{sqlite_file_uri(path)}?mode=ro", uri=True)) as conn:
            return [tuple(row) for row in conn.execute(query, tuple(params))]
    except sqlite3.Error as exc:
        raise StoreReadError(f"{leg} store {path}: {exc}") from None


def _equity_row(path: Path, query: str, run_id: str, *, leg: str) -> tuple[object, ...]:
    rows = _rows(path, query, (run_id,), leg=leg)
    if not rows:
        raise StoreReadError(
            f"{leg} store {path}: run {run_id!r} has no equity row; a mistyped run id reads "
            f"the same, and a run too new to have one is sized as equal capital by leaving "
            f"out --{leg}-db and --{leg}-run-id"
        )
    return rows[0]


def _decimal(raw: object, path: Path, *, leg: str, what: str) -> Decimal:
    try:
        return decimal(raw, what, error=StoreReadError)
    except StoreReadError as exc:
        raise StoreReadError(f"{leg} store {path}: {exc}") from None


def _perp_ms(stamp: object, path: Path, *, what: str) -> int:
    try:
        return instant_ms(stamp, what, error=StoreReadError)
    except StoreReadError as exc:
        raise StoreReadError(f"perp store {path}: {exc}") from None


def _spot_ms(seconds: object, path: Path, *, what: str) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise StoreReadError(f"spot store {path}: {what} bar time {seconds!r} is not valid")
    return seconds * 1000


def _amounts(raw: object, path: Path, *, what: str) -> dict[str, Decimal]:
    """A ``symbol -> decimal text`` JSON object as the spot store writes it."""
    try:
        document = json.loads(str(raw))
    except ValueError:
        raise StoreReadError(f"spot store {path}: {what} {raw!r} is not JSON") from None
    if not isinstance(document, dict) or not all(
        isinstance(symbol, str) and isinstance(amount, str) for symbol, amount in document.items()
    ):
        raise StoreReadError(f"spot store {path}: {what} {raw!r} is not an object of decimal text")
    return {
        symbol: _decimal(amount, path, leg="spot", what=f"{what} {symbol}")
        for symbol, amount in document.items()
    }


def _require_usd_quote(quote: object, path: Path, run_id: str) -> str:
    if quote not in USD_QUOTES:
        raise StoreReadError(
            f"spot store {path}: run {run_id!r} is quoted in {quote!r}, not a USD stable "
            f"({', '.join(sorted(USD_QUOTES))}); its equity cannot be sized against the perp leg's"
        )
    return str(quote)


# --- the coordinator's sizing -------------------------------------------------


def perp_equity(path: Path, run_id: str) -> Equity:
    """The perp run's latest ``account_equity``, stamped with that snapshot's instant."""
    raw, stamp = _equity_row(
        path,
        "SELECT account_equity, timestamp FROM account_snapshots WHERE run_id = ? "
        "ORDER BY timestamp DESC, snapshot_id DESC LIMIT 1",
        run_id,
        leg="perp",
    )
    at_ms = _perp_ms(stamp, path, what=f"run {run_id!r}: the perp snapshot's timestamp")
    return Equity(_decimal(raw, path, leg="perp", what=f"run {run_id!r} equity"), at_ms)


def spot_equity(path: Path, run_id: str) -> Equity:
    """The spot run's latest ``total_value``, stamped with that bar's instant."""
    raw, seconds, quote = _equity_row(
        path,
        "SELECT v.total_value, v.time, r.quote FROM valuations v JOIN runs r ON r.run_id = v.run_id "
        "WHERE v.run_id = ? ORDER BY v.time DESC LIMIT 1",
        run_id,
        leg="spot",
    )
    _require_usd_quote(quote, path, run_id)
    return Equity(
        _decimal(raw, path, leg="spot", what=f"run {run_id!r} equity"),
        _spot_ms(seconds, path, what=f"run {run_id!r}"),
    )


# --- the report's reads: the perp leg -----------------------------------------


class CycleKind(str, Enum):
    """How one of the perp run's decision cycles ended, as the report counts it."""

    # The cycle read an applicable handoff and put its target to the gate.
    APPLIED = "applied"
    # The cycle sat in the minutes before the next boundary with that boundary's
    # document already written: the normal schedule, not a fault.
    PENDING = "pending"
    # No document, a stale one, another coin's, an unreadable one: both legs
    # held (D5). The fault the report counts days of.
    UNUSABLE = "unusable"
    # The cycle ended without a decision (market data, a write failure, an
    # interruption): nothing was read.
    NO_DECISION = "no_decision"
    # The cycle was decided by the engine behind a model, not from the handoff.
    ENGINE = "engine"


@dataclass(frozen=True)
class PerpCycle:
    """One decision cycle: when it was scheduled, how it ended, and the gate's word on it.

    ``risk_action`` is the gate's word on an applied target, ``None`` for a
    cycle that put nothing to the gate.
    """

    scheduled_ms: int
    kind: CycleKind
    risk_action: RiskAction | None


@dataclass(frozen=True)
class PerpPosition:
    """The position a snapshot carried: its signed size (a short is negative) and the mark."""

    size: Decimal
    mark: Decimal


@dataclass(frozen=True)
class PerpSnapshot:
    """A cycle-end account snapshot, with the position of the same instant when there was one.

    The engine writes the position row only while the run holds a position,
    so ``position`` is ``None`` exactly when the run was flat.
    """

    at_ms: int
    equity: Decimal
    position: PerpPosition | None


# The attempt statuses under which the provider's answer reached the store:
# ``completed``, and ``invalid_output`` — the scheduler's word for an answer
# the decision contract refused (a file target with an off-grid margin fails
# closed THERE, after the sidecar was written), so it is an applied cycle the
# gate refused, not a cycle that decided nothing.
_DECIDED: Final = frozenset({AttemptStatus.COMPLETED.value, AttemptStatus.INVALID_OUTPUT.value})


def _kind(status: object, model: object, reason: object) -> CycleKind:
    if status not in _DECIDED or reason is None:
        # No answer row at all is missing evidence: it fails toward the alarm.
        return CycleKind.NO_DECISION
    if model != FILE_TARGET_MODEL:
        return CycleKind.ENGINE
    text = str(reason)
    if text.startswith(FILE_TARGET_UNUSABLE_PREFIX):
        return CycleKind.UNUSABLE
    if text.startswith(FILE_TARGET_PENDING_PREFIX):
        return CycleKind.PENDING
    return CycleKind.APPLIED


def _risk_action(value: object, path: Path, *, what: str) -> RiskAction | None:
    """The gate's word through its own vocabulary; a word this package does not know is refused."""
    if value is None:
        return None
    try:
        return RiskAction(value)
    except ValueError:
        raise StoreReadError(
            f"perp store {path}: {what}: risk_action {value!r} is not a gate action"
        ) from None


def perp_cycles(path: Path, run_id: str) -> list[PerpCycle]:
    """Every decision cycle of the run, oldest first, with its kind.

    A cycle is a ``decision_attempts`` row (one per scheduled cycle, whatever
    its tries), read through the input row and the output row the attempt
    names; an attempt still in progress has no kind yet and is left out.
    """
    rows = _rows(
        path,
        "SELECT a.scheduled_at, a.status, i.model, o.decision_reason, o.risk_action "
        "FROM decision_attempts a "
        "LEFT JOIN ai_inputs i ON i.input_id = a.input_id "
        "LEFT JOIN ai_outputs o ON o.output_id = a.output_id "
        "WHERE a.run_id = ? ORDER BY a.scheduled_at",
        (run_id,),
        leg="perp",
    )
    what = f"run {run_id!r}: a cycle's scheduled_at"
    return [
        PerpCycle(
            _perp_ms(scheduled, path, what=what),
            _kind(status, model, reason),
            _risk_action(action, path, what=f"run {run_id!r}: a cycle's output"),
        )
        for scheduled, status, model, reason, action in rows
        if status != AttemptStatus.IN_PROGRESS.value
    ]


def perp_funding(path: Path, run_id: str, coin: str) -> list[tuple[int, Decimal]]:
    """The run's posted funding on ``coin``: ``(settlement instant, pnl)``, income positive."""
    rows = _rows(
        path,
        "SELECT funding_timestamp, funding_pnl FROM funding_events "
        "WHERE run_id = ? AND symbol = ? AND status = 'posted' ORDER BY funding_timestamp",
        (run_id, coin),
        leg="perp",
    )
    return [
        (
            _perp_ms(stamp, path, what=f"run {run_id!r}: a funding event's timestamp"),
            _decimal(pnl, path, leg="perp", what=f"run {run_id!r} funding_pnl"),
        )
        for stamp, pnl in rows
    ]


def perp_pending_funding(path: Path, run_id: str, coin: str) -> list[int]:
    """The instants of the run's settlements on ``coin`` not yet posted (rate unavailable)."""
    rows = _rows(
        path,
        "SELECT funding_timestamp FROM funding_events "
        "WHERE run_id = ? AND symbol = ? AND status != 'posted' ORDER BY funding_timestamp",
        (run_id, coin),
        leg="perp",
    )
    what = f"run {run_id!r}: a funding event's timestamp"
    return [_perp_ms(stamp, path, what=what) for (stamp,) in rows]


def perp_fees(path: Path, run_id: str, coin: str) -> list[tuple[int, Decimal]]:
    """The run's fills on ``coin``: ``(fill instant, fee)``."""
    rows = _rows(
        path,
        "SELECT timestamp, fee FROM fills WHERE run_id = ? AND symbol = ? ORDER BY timestamp",
        (run_id, coin),
        leg="perp",
    )
    return [
        (
            _perp_ms(stamp, path, what=f"run {run_id!r}: a fill's timestamp"),
            _decimal(fee, path, leg="perp", what=f"run {run_id!r} fill fee"),
        )
        for stamp, fee in rows
    ]


def perp_snapshots(path: Path, run_id: str, coin: str) -> list[PerpSnapshot]:
    """The run's cycle-end snapshots, oldest first, each with its ``coin`` position if any."""
    rows = _rows(
        path,
        "SELECT a.timestamp, a.account_equity, p.position_size, p.mark_price "
        "FROM account_snapshots a "
        "LEFT JOIN position_snapshots p "
        "ON p.run_id = a.run_id AND p.timestamp = a.timestamp AND p.symbol = ? "
        "WHERE a.run_id = ? ORDER BY a.timestamp, a.snapshot_id",
        (coin, run_id),
        leg="perp",
    )
    what = f"run {run_id!r}"
    snapshots = []
    for stamp, equity, size, mark in rows:
        at_ms = _perp_ms(stamp, path, what=f"{what}: the perp snapshot's timestamp")
        if (size is None) != (mark is None):
            raise StoreReadError(
                f"perp store {path}: {what}: the position row at {stamp} has a size "
                f"without a mark, or a mark without a size"
            )
        position = (
            None
            if size is None
            else PerpPosition(
                _decimal(size, path, leg="perp", what=f"{what} position_size"),
                _decimal(mark, path, leg="perp", what=f"{what} mark_price"),
            )
        )
        equity_value = _decimal(equity, path, leg="perp", what=f"{what} account_equity")
        snapshots.append(PerpSnapshot(at_ms, equity_value, position))
    return snapshots


# --- the report's reads: the spot leg -----------------------------------------


@dataclass(frozen=True)
class SpotRun:
    """What the spot run's stored config says of it: its quote, its tokens, its target source."""

    quote: str
    tokens: tuple[str, ...]
    target_source: str


@dataclass(frozen=True)
class SpotValuation:
    """The portfolio after a bar's decision, valued at that bar's prices."""

    at_ms: int
    balances: Mapping[str, Decimal]
    prices: Mapping[str, Decimal]
    total: Decimal


@dataclass(frozen=True)
class SpotFill:
    """One swap that filled at a bar."""

    at_ms: int
    token_in: str
    token_out: str
    amount_in: Decimal
    amount_out: Decimal
    gas_eth: Decimal


@dataclass(frozen=True)
class SpotTarget:
    """A target the source holds for the token at a bar: the weight and the handoff's digest."""

    at_ms: int
    weight: Decimal
    digest: str


def spot_run(path: Path, run_id: str) -> SpotRun:
    """The run's quote, tokens and target source, from its config snapshot.

    A run whose snapshot has no ``targets`` section reads no targets and is
    not a carry run: refused, as the report would have nothing to reconcile.
    """
    rows = _rows(path, "SELECT quote, config FROM runs WHERE run_id = ?", (run_id,), leg="spot")
    if not rows:
        raise StoreReadError(f"spot store {path}: there is no run {run_id!r}")
    quote, config = rows[0]
    try:
        document = json.loads(str(config))
    except ValueError:
        raise StoreReadError(f"spot store {path}: run {run_id!r}: config is not JSON") from None
    targets = document.get("targets") if isinstance(document, dict) else None
    source = targets.get("source") if isinstance(targets, dict) else None
    if not isinstance(source, str) or not source:
        raise StoreReadError(
            f"spot store {path}: run {run_id!r} reads no targets (its config has no targets "
            f"section); it is not the carry sleeve's spot leg"
        )
    tokens = document.get("tokens")
    if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
        raise StoreReadError(f"spot store {path}: run {run_id!r}: config names no tokens")
    return SpotRun(_require_usd_quote(quote, path, run_id), tuple(tokens), source)


def spot_decisions(path: Path, run_id: str) -> list[tuple[int, str]]:
    """The run's decisions, oldest first: ``(bar instant, outcome)``."""
    rows = _rows(
        path,
        "SELECT time, outcome FROM decisions WHERE run_id = ? ORDER BY time",
        (run_id,),
        leg="spot",
    )
    what = f"run {run_id!r}"
    return [(_spot_ms(seconds, path, what=what), str(outcome)) for seconds, outcome in rows]


def spot_valuations(path: Path, run_id: str) -> list[SpotValuation]:
    """The run's valuations, oldest first."""
    rows = _rows(
        path,
        "SELECT time, balances, prices, total_value FROM valuations WHERE run_id = ? ORDER BY time",
        (run_id,),
        leg="spot",
    )
    what = f"run {run_id!r}"
    return [
        SpotValuation(
            _spot_ms(seconds, path, what=what),
            _amounts(balances, path, what=f"{what} balances"),
            _amounts(prices, path, what=f"{what} prices"),
            _decimal(total, path, leg="spot", what=f"{what} total_value"),
        )
        for seconds, balances, prices, total in rows
    ]


def spot_fills(path: Path, run_id: str) -> list[SpotFill]:
    """The run's fills, oldest first."""
    rows = _rows(
        path,
        "SELECT time, token_in, token_out, amount_in, amount_out, gas_cost_eth FROM fills "
        "WHERE run_id = ? ORDER BY time, leg",
        (run_id,),
        leg="spot",
    )
    what = f"run {run_id!r}"
    return [
        SpotFill(
            _spot_ms(seconds, path, what=what),
            str(token_in),
            str(token_out),
            _decimal(amount_in, path, leg="spot", what=f"{what} amount_in"),
            _decimal(amount_out, path, leg="spot", what=f"{what} amount_out"),
            _decimal(gas, path, leg="spot", what=f"{what} gas_cost_eth"),
        )
        for seconds, token_in, token_out, amount_in, amount_out, gas in rows
    ]


def spot_targets(path: Path, source: str, token: str) -> list[SpotTarget]:
    """The targets ``source`` holds for ``token``, oldest first."""
    rows = _rows(
        path,
        "SELECT time, weight, digest FROM targets WHERE source = ? AND symbol = ? ORDER BY time",
        (source, token),
        leg="spot",
    )
    what = f"targets of {source!r}"
    return [
        SpotTarget(
            _spot_ms(seconds, path, what=what),
            _decimal(weight, path, leg="spot", what=f"{what} weight"),
            str(digest),
        )
        for seconds, weight, digest in rows
    ]
