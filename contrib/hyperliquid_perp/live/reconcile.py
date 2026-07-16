"""Live exchange reconciliation — the §12 sweep over orders, fills, positions,
account equity and SL protection.

§12.1: the exchange is the truth source for live state; SQLite is the audit
trail. This module never lets the local record overrule the exchange — every
§12.3 case is RECORDED (``exchange_reconciliation_events`` + the snapshot rows'
``reconciliation_status``/``reconciliation_diff``), the ones with a safe
mechanical fix are fixed (settle a stuck order from ``orderStatus``, back-fill
an orphan's local row, book missing fills through the PR 3 backfiller), and the
rest flip the pass to MISMATCH, which the caller turns into safe mode
(:meth:`LiveReconciler.reconcile_and_apply`).

One deliberate asymmetry: the sweep only ever writes what the exchange PROVED
(an ``orderStatus`` verdict, a booked fill, an ack). It never zeroes a phantom
local position or fabricates a correcting entry — "以交易所為準" is reached by
booking the exchange events that explain the difference (the fill backfill),
and when those cannot be found the books are wrong in a way only a human should
touch: the case stays open, the pass stays unclean, and the §13.5 repeated-
mismatch ladder escalates to manual safe mode.

Reads are seams (callables) so every §12.3 case is testable with fakes;
production binds them to the PR 1 signed client and Info wrapper.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..domains.perp.enum_guard import check_enum
from ..domains.perp.schema import AccountSnapshot, PerpPosition
from ..exchanges.hyperliquid.mapper import map_account_snapshot
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import exchange_fill_key
from .fill_backfill import (
    DEFAULT_MAX_PAGES,
    RESPONSE_FILL_CAP,
    BackfillSummary,
    FillBackfiller,
)
from .orders import local_status_for_exchange_status, parse_order_status
from .payloads import write_raw_payload
from .safe_mode import (
    REASON_INVALID_LOCAL_FILL,
    REASON_NON_BOT_OWNED_ORDER,
    REASON_RECONCILIATION_MISMATCH,
    REASON_SL_MISSING,
    REASON_UNKNOWN_POSITION,
    SafeModeManager,
)
from .ws_stream import LiveWsStream

__all__ = [
    "EQUITY_TOLERANCE_ABS_USDC",
    "EQUITY_TOLERANCE_REL",
    "LiveReconciler",
    "ReconciliationCase",
    "ReconciliationReport",
]

logger = logging.getLogger(__name__)

# §12.3 "equity difference beyond tolerance". The spec names the rule but not
# the number; these are PROVISIONAL tuning constants (same convention as the
# PR 2 deadband constants) sized for the §21 mainnet-tiny account: pending
# fees/funding legitimately float the local ledger by cents between passes, so
# the tolerance is 1% of exchange equity with a 1 USDC floor. Revisit against
# testnet_live telemetry (PR 6 acceptance) before mainnet.
EQUITY_TOLERANCE_ABS_USDC = Decimal("1")
EQUITY_TOLERANCE_REL = Decimal("0.01")

# The invalid-local-fill cross-check window (§12.3 "SQLite 有 fill，但交易所查
# 不到"). Mirrors the backfiller's trailing lookback; fills near the window
# edges are excluded from the verdict — a fill booked milliseconds ago (or one
# at the window's far edge) can be absent from one read without being invalid.
_FILL_CROSSCHECK_LOOKBACK = timedelta(hours=6)
_FILL_CROSSCHECK_EDGE_MARGIN = timedelta(minutes=2)

_HL_SIDE = {"B": "buy", "A": "sell"}


@dataclass(frozen=True)
class ReconciliationCase:
    """One observed §12.3 case: what, where, and whether a human must decide."""

    case_type: str
    symbol: str | None
    local_value: str | None
    exchange_value: str | None
    detail: str | None = None
    action_taken: str | None = None
    # True → §13.5 manual safe mode (non-bot order, unknown position, a fill
    # the exchange denies); False → recoverable, healable by a later pass.
    manual: bool = False
    # True → the case was RESOLVED in this pass (settled/back-filled); it is
    # recorded for the audit trail but does not make the pass unclean.
    resolved: bool = False

    def __post_init__(self) -> None:
        # Mutually exclusive by the module's model: a manual case is one only a
        # human may dispose of, so nothing in this pass can have resolved it.
        # Enforced because ``manual_cases`` filters on ``not resolved`` — a
        # future path constructing both True would silently skip the §13.5
        # manual escalation for a case that requires it.
        if self.manual and self.resolved:
            raise ValueError(
                f"a ReconciliationCase cannot be both manual and resolved "
                f"({self.case_type}): manual means only a human may dispose of it"
            )


@dataclass(frozen=True)
class ReconciliationReport:
    """One pass's verdict, leg by leg — the §13.4 release conditions read it."""

    trigger: str
    timestamp: datetime
    cases: tuple[ReconciliationCase, ...]
    orders_reconciled: bool
    fills_reconciled: bool
    position_reconciled: bool
    account_reconciled: bool
    position_protected: bool
    backfill_complete: bool
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """§13.4's "no unresolved mismatch": every leg proved, nothing open."""
        return (
            self.orders_reconciled
            and self.fills_reconciled
            and self.position_reconciled
            and self.account_reconciled
            and self.position_protected
            and self.backfill_complete
            and not self.errors
        )

    @property
    def manual_cases(self) -> tuple[ReconciliationCase, ...]:
        return tuple(c for c in self.cases if c.manual and not c.resolved)


class LiveReconciler:
    """One run's §12 reconciliation sweep, bound to injectable exchange reads.

    ``fetch_open_orders`` / ``fetch_clearinghouse`` / ``query_order_by_cloid``
    / ``fetch_fills`` are the exchange seams (production: the signed client's
    ``open_orders``, an Info ``user_state`` read, ``query_order_by_cloid`` and
    ``user_fills_by_time``). ``backfiller`` + ``stream`` are the PR 3 fill
    leg; either may be None in reads-only wirings (tests, offline verdicts),
    which skips booking and reports the fill leg from the sighting backlog
    alone.
    """

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        coin: str,
        fetch_open_orders: Callable[[], Any],
        fetch_clearinghouse: Callable[[], Any],
        query_order_by_cloid: Callable[[str], Any],
        fetch_fills: Callable[[int, int], Any] | None = None,
        backfiller: FillBackfiller | None = None,
        stream: LiveWsStream | None = None,
        payload_dir: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._coin = coin
        self._fetch_open_orders = fetch_open_orders
        self._fetch_clearinghouse = fetch_clearinghouse
        self._query_order_by_cloid = query_order_by_cloid
        self._fetch_fills = fetch_fills
        self._backfiller = backfiller
        self._stream = stream
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()

    # ------------------------------------------------------------------ run

    def run(self, trigger: str) -> ReconciliationReport:
        """One full §12.3 sweep; records everything, raises nothing.

        A leg whose exchange read fails is reported UNRECONCILED (with the
        error) rather than raising: §12.2 runs this from heartbeats and
        shutdown paths that must keep running, and "could not prove" already
        maps to the fail-safe verdict (unclean → safe mode).
        """
        check_enum(trigger, repo.RECONCILIATION_TRIGGERS, name="trigger")
        now = self._clock.now()
        cases: list[ReconciliationCase] = []
        errors: list[str] = []

        # -- exchange reads (each leg degrades independently) ---------------
        open_orders: list | None = None
        try:
            raw_orders = self._fetch_open_orders()
            if isinstance(raw_orders, list):
                open_orders = raw_orders
            else:
                errors.append(f"open_orders returned {type(raw_orders).__name__}, expected a list")
        except Exception as exc:  # noqa: BLE001 — a failed read is a verdict, not a crash
            errors.append(f"open_orders failed: {exc}")

        snapshot: AccountSnapshot | None = None
        raw_clearinghouse: Any = None
        try:
            raw_clearinghouse = self._fetch_clearinghouse()
            snapshot = map_account_snapshot(raw_clearinghouse)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"clearinghouse state read failed: {exc}")

        # -- legs -----------------------------------------------------------
        backfill_summary = self._run_fill_backfill(errors)
        orders_ok = self._reconcile_orders(open_orders, cases, errors, now)
        fills_ok = self._reconcile_fills(cases, errors, now)
        position_ok, protected = self._reconcile_positions(open_orders, snapshot, cases)
        account_ok = self._reconcile_account(snapshot, cases, errors)

        report = ReconciliationReport(
            trigger=trigger,
            timestamp=now,
            cases=tuple(cases),
            orders_reconciled=orders_ok,
            fills_reconciled=fills_ok,
            position_reconciled=position_ok,
            account_reconciled=account_ok,
            position_protected=protected,
            backfill_complete=backfill_summary is None or backfill_summary.complete,
            errors=tuple(errors),
        )
        self._record(report, snapshot, raw_clearinghouse, backfill_summary)
        if not report.clean:
            logger.warning(
                "reconciliation (%s) UNCLEAN: orders=%s fills=%s position=%s account=%s "
                "protected=%s backfill=%s cases=%d errors=%s",
                trigger,
                orders_ok,
                fills_ok,
                position_ok,
                account_ok,
                protected,
                report.backfill_complete,
                len([c for c in cases if not c.resolved]),
                "; ".join(errors) or "none",
            )
        return report

    def reconcile_and_apply(
        self,
        trigger: str,
        *,
        safe_mode: SafeModeManager,
        ws_restored: bool = True,
        kill_switch_active: bool = True,
    ) -> ReconciliationReport:
        """Run a pass and drive the safe-mode machine from its verdict.

        Manual cases enter manual safe mode with their own reason; any other
        unclean pass enters (or keeps) recoverable safe mode; a clean pass
        counts toward §13.4 auto-recovery, with the caller attesting the two
        conditions the reconciler cannot see (WS restored, kill switch
        healthy — pass ``kill_switch.release_safe_mode()``'s verdict there
        when the kill-switch latch is up).
        """
        report = self.run(trigger)
        for case in report.manual_cases:
            reason = {
                "non_bot_owned_order": REASON_NON_BOT_OWNED_ORDER,
                "invalid_local_fill": REASON_INVALID_LOCAL_FILL,
                "exchange_position_mismatch": REASON_UNKNOWN_POSITION,
            }.get(case.case_type, REASON_RECONCILIATION_MISMATCH)
            safe_mode.enter("manual", reason, detail=case.detail or case.case_type)
        safe_mode.note_reconciliation_outcome(report.clean)
        if report.clean:
            if not safe_mode.try_auto_recover(
                reconciliation_clean=True,
                ws_restored=ws_restored,
                kill_switch_active=kill_switch_active,
            ):
                # Not in recoverable safe mode (nothing to release), or manual
                # is still latched: a clean pass still re-proves the §4.1
                # "state is reconciled" line — the manual flag keeps blocking
                # on its own until the §13.6 CLI release.
                safe_mode.set_state_reconciled(True)
        else:
            safe_mode.set_state_reconciled(False)
            if not report.manual_cases:
                unresolved = sorted({c.case_type for c in report.cases if not c.resolved})
                # The one unclean shape with its own named reason: a position
                # whose only problem is a missing/insufficient SL (repair is
                # PR 5's manager) — queryable in safe_mode_events as such.
                reason = (
                    REASON_SL_MISSING
                    if unresolved == ["position_sl_missing"] and not report.errors
                    else REASON_RECONCILIATION_MISMATCH
                )
                safe_mode.enter(
                    "recoverable",
                    reason,
                    detail="; ".join(unresolved) or "; ".join(report.errors),
                )
        return report

    # ------------------------------------------------------------- fills leg

    def _run_fill_backfill(self, errors: list[str]) -> BackfillSummary | None:
        """§12.3 "交易所有 fill，但 SQLite 沒記錄": book them via the PR 3 path.

        The epoch discipline mirrors the stream's contract: read the epoch,
        run the pass, clear with the epoch read — and only for a COMPLETE
        pass, so a capped window never retires the gap it failed to cover.
        """
        if self._backfiller is None:
            return None
        epoch = self._stream.backfill_epoch() if self._stream is not None else None
        if self._stream is not None:
            since = self._stream.backfill_since()
        else:
            # No WS stream in this wiring (the PR 4 startup command; the daemon
            # loop with its stream-held obligations is PR 5): the whole
            # process-was-down era is owed, so the floor is the newest booked
            # fill — or the run's genesis when none exists yet (§11.2 rule 5).
            # The trailing lookback alone would silently skip any outage longer
            # than 6h. The §11.2 v12 durable clean-backfill watermark (which
            # hardens this derivation against a crash mid-backfill) lands with
            # PR 5's daemon wiring — decided 2026-07-16.
            since = repo.last_live_fill_time(self._db.conn, self._run_id)
            if since is None:
                run_row = repo.get_run(self._db.conn, self._run_id)
                if run_row is not None:
                    try:
                        since = datetime.fromisoformat(run_row["created_at"])
                    except (TypeError, ValueError):
                        since = None  # unparseable genesis: fall back to the lookback
        try:
            summary = self._backfiller.backfill(self._clock.now(), since=since)
        except Exception as exc:  # noqa: BLE001 — transport failure = gap still open
            errors.append(f"fill backfill failed: {exc}")
            return BackfillSummary(
                fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0, complete=False
            )
        if summary.complete and self._stream is not None and epoch is not None:
            self._stream.mark_backfill_done(epoch)
        return summary

    def _reconcile_fills(
        self, cases: list[ReconciliationCase], errors: list[str], now: datetime
    ) -> bool:
        """The fill-ledger legs: sighting backlog + the invalid-local check."""
        ok = True
        conn = self._db.conn

        # Sweep the malformed backlog first: a sighting whose bare-tid key has
        # since been booked (a §8.3 recovery re-ingested it) resolves itself;
        # digest-keyed sightings are human territory (§12.3 v11) and do not
        # block the pass — they are an audit backlog, not a proven missing fill.
        for row in repo.iter_exchange_reconciliation_events(
            conn, self._run_id, case_type="fill_malformed"
        ):
            if row["action_taken"] is not None:
                continue
            key = row["exchange_value"]
            if not key or key.startswith("unparsed-"):
                continue
            try:
                booked = repo.get_fill_by_exchange_key(conn, exchange_fill_key(tid=key))
            except ValueError:
                continue  # the malformed tid violates the key derivation: human territory
            if booked is not None:
                with self._db.transaction() as tx:
                    repo.set_reconciliation_action(tx, row["event_id"], "resolved_fill_booked")

        # §12.3 (v10/v11): unmapped sightings still absent from the ledger are
        # the known "exchange has a fill we have not booked" backlog.
        unbooked = repo.iter_unresolved_fill_sightings(conn, self._run_id)
        if unbooked:
            ok = False
            logger.warning(
                "%d unmapped fill sighting(s) still unbooked — fills are not reconciled",
                len(unbooked),
            )

        # §12.3 "SQLite 有 fill，但交易所查不到" → invalid_local_fill (manual:
        # money the exchange denies is booked money we cannot trust).
        if self._fetch_fills is not None:
            window_start = now - _FILL_CROSSCHECK_LOOKBACK
            exchange_keys = self._fetch_window_fill_keys(
                self._fetch_fills, window_start, now, errors
            )
            if exchange_keys is None:
                return False  # fetch failed or window provably not covered: inconclusive
            verdict_start = window_start + _FILL_CROSSCHECK_EDGE_MARGIN
            verdict_end = now - _FILL_CROSSCHECK_EDGE_MARGIN
            for fill in repo.iter_live_fills(conn, self._run_id):
                fill_time = fill["exchange_fill_time"]
                if fill_time is None:
                    continue
                try:
                    stamp = datetime.fromisoformat(fill_time)
                except ValueError:
                    # run()'s contract is "records everything, raises nothing";
                    # an unparseable stored timestamp is a verdict, not a crash.
                    errors.append(
                        f"fill {fill['fill_id']} has unparseable exchange_fill_time "
                        f"{fill_time!r} — fills leg cannot be proven"
                    )
                    ok = False
                    continue
                if not (verdict_start <= stamp <= verdict_end):
                    continue
                if fill["exchange_fill_key"] not in exchange_keys:
                    ok = False
                    cases.append(
                        ReconciliationCase(
                            case_type="invalid_local_fill",
                            symbol=fill["symbol"],
                            local_value=fill["fill_id"],
                            exchange_value=fill["exchange_fill_key"],
                            detail=(
                                f"fill {fill['fill_id']} booked locally at {fill_time} is "
                                "absent from the exchange's fill history — its accounting "
                                "cannot be trusted (§14: never re-applied; human review)"
                            ),
                            manual=True,
                        )
                    )
        return ok

    def _fetch_window_fill_keys(
        self,
        fetch: Callable[[int, int], Any],
        window_start: datetime,
        now: datetime,
        errors: list[str],
    ) -> set[str] | None:
        """Every §14.2 key the exchange reports in the window, or None if unproven.

        PAGED, exactly like the backfiller: ``userFillsByTime`` caps a response
        (2000 fills), and judging "the exchange denies this fill" against a
        TRUNCATED window would flag genuinely booked fills as invalid and force
        manual safe mode on a false premise. A window that cannot be proven
        covered (page budget out, unadvanceable page) returns None — the leg
        reports inconclusive (fail-safe) instead of issuing manual verdicts.
        """
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        keys: set[str] = set()
        for _ in range(DEFAULT_MAX_PAGES):
            try:
                raw = fetch(start_ms, end_ms)
                if not isinstance(raw, list):
                    raise ValueError(f"user_fills_by_time returned {type(raw).__name__}")
            except Exception as exc:  # noqa: BLE001 — a failed read is a verdict
                errors.append(f"fill cross-check fetch failed: {exc}")
                return None
            newest_ms: int | None = None
            for f in raw:
                if isinstance(f, dict):
                    if f.get("tid") not in (None, ""):
                        keys.add(exchange_fill_key(tid=f["tid"]))
                    t = f.get("time")
                    if isinstance(t, int) and (newest_ms is None or t > newest_ms):
                        newest_ms = t
            if len(raw) < RESPONSE_FILL_CAP:
                return keys  # the exchange gave everything: the window is covered
            if newest_ms is None or newest_ms <= start_ms:
                break  # cannot advance: paging again would refetch the same page
            start_ms = newest_ms
        errors.append(
            "fill cross-check window not covered (response still capped after "
            f"{DEFAULT_MAX_PAGES} pages) — invalid-fill verdicts withheld"
        )
        return None

    # ------------------------------------------------------------ orders leg

    def _reconcile_orders(
        self,
        open_orders: list | None,
        cases: list[ReconciliationCase],
        errors: list[str],
        now: datetime,
    ) -> bool:
        if open_orders is None:
            return False
        ok = True
        conn = self._db.conn
        exchange_open_cloids: set[str] = set()

        for order in open_orders:
            if not isinstance(order, dict):
                errors.append(f"malformed open_orders entry ({type(order).__name__})")
                ok = False
                continue
            oid = str(order.get("oid", "?"))
            coin = order.get("coin")
            cloid = order.get("cloid")
            registry = repo.get_cloid_by_hex(conn, cloid) if isinstance(cloid, str) else None
            if registry is None or not isinstance(cloid, str):
                # §12.3 row 3 / §19.3: no cloid, or a cloid our registry never
                # issued → non-bot-owned → manual safe mode; never manage it.
                ok = False
                cases.append(
                    ReconciliationCase(
                        case_type="non_bot_owned_order",
                        symbol=coin if isinstance(coin, str) else None,
                        local_value=None,
                        exchange_value=f"oid={oid}",
                        detail=(
                            f"exchange open order oid={oid} cloid={cloid!r} has no "
                            "cloid_registry mapping — not bot-owned (§19.3); manual "
                            "safe mode, the bot never manages it (§25)"
                        ),
                        manual=True,
                    )
                )
                continue
            exchange_open_cloids.add(cloid)
            local = repo.get_order_by_cloid_hex(conn, cloid)
            if local is None:
                # §12.3 row 2: bot-owned on the exchange, absent locally →
                # back-fill the local row from what the exchange reported
                # (fail-safe direction: once the row exists, every later sweep
                # — kill-switch shutdown included — sees and manages it).
                resolved = self._backfill_orphan_order(order, registry, now)
                cases.append(
                    ReconciliationCase(
                        case_type="orphan_exchange_order",
                        symbol=registry["symbol"],
                        local_value=None,
                        exchange_value=cloid,
                        detail=f"exchange open order oid={oid} had no local orders row",
                        action_taken="local_row_backfilled" if resolved else None,
                        resolved=resolved,
                    )
                )
                if not resolved:
                    ok = False
            elif local["status"] not in repo.LIVE_ORDER_STATUSES:
                # The exchange lists the order OPEN but the local row is
                # terminal — the local record contradicts the exchange (§12.1).
                # The usual path here: a past pass settled the row 'rejected'
                # off an unknownOid answer, and the send had in fact landed.
                # The exchange wins: the row reopens (making the order visible
                # again to iter_open_live_orders and the shutdown cross-check),
                # never the other way around.
                with self._db.transaction() as tx:
                    repo.update_order(
                        tx,
                        local["order_id"],
                        status="open",
                        status_reason="reopened_from_exchange_reconciliation",
                        exchange_order_id=str(order.get("oid")),
                        exchange_status="open",
                        updated_at=now,
                    )
                cases.append(
                    ReconciliationCase(
                        case_type="orphan_exchange_order",
                        symbol=registry["symbol"],
                        local_value=f"{local['order_id']}:{local['status']}",
                        # Distinct fact key: a later re-settle of the same cloid
                        # must not dedupe against a plain-orphan sighting.
                        exchange_value=f"{cloid}|local_terminal",
                        detail=(
                            f"exchange lists oid={oid} open but the local row was "
                            f"terminal ({local['status']}) — reopened per §12.1"
                        ),
                        action_taken="local_row_reopened",
                        resolved=True,
                    )
                )

        # §12.3 row 1: locally-live orders the exchange's open-orders view did
        # not list. Ask orderStatus directly; never resend (§8.3).
        for row in repo.iter_open_live_orders(conn):
            cloid = row["cloid_hex"]
            if cloid in exchange_open_cloids:
                continue
            settled, case = self._settle_absent_order(row, now)
            if case is not None:
                cases.append(case)
            if not settled:
                ok = False
        return ok

    def _backfill_orphan_order(self, order: dict, registry: Any, now: datetime) -> bool:
        """Insert the missing local row for a bot-owned exchange order."""
        try:
            side_raw = order.get("side")
            side = _HL_SIDE.get(side_raw) if isinstance(side_raw, str) else None
            if side is None:
                raise ValueError(f"open_orders entry side {side_raw!r} not recognised")
            # `is None` fallback, not dict.get's default: a PRESENT-but-null
            # origSz must still fall through to sz (get's default only covers
            # the absent-key case, and Decimal(str(None)) raises).
            raw_orig = order.get("origSz")
            if raw_orig is None:
                raw_orig = order.get("sz")
            qty = Decimal(str(raw_orig))
            remaining = Decimal(str(order.get("sz")))
            price = order.get("limitPx")
            with self._db.transaction() as conn:
                repo.insert_order(
                    conn,
                    # Deterministic and collision-free: one orphan row per cloid
                    # (idx_orders_cloid_hex would reject a second anyway).
                    order_id=f"orphan|{registry['cloid_hex']}",
                    mode="live",
                    run_id=registry["run_id"],
                    symbol=registry["symbol"],
                    order_role=registry["order_role"],
                    side=side,
                    order_type="ioc_limit",
                    qty=qty,
                    filled_qty=qty - remaining,
                    remaining_qty=remaining,
                    status="open",
                    status_reason="backfilled_from_exchange_reconciliation",
                    price=None if price is None else Decimal(str(price)),
                    reduce_only=bool(order.get("reduceOnly", False)),
                    cloid_logical=registry["cloid_logical"],
                    cloid_hex=registry["cloid_hex"],
                    exchange_order_id=str(order.get("oid")),
                    exchange_status="open",
                    exchange_raw_status=str(order.get("orderType", "open")),
                    is_bot_owned=True,
                    timestamp=now,
                )
            return True
        except Exception as exc:  # noqa: BLE001 — an unfillable orphan stays a mismatch
            logger.warning(
                "could not back-fill orphan order for cloid %s: %s", registry["cloid_hex"], exc
            )
            return False

    def _settle_absent_order(
        self, row: Any, now: datetime
    ) -> tuple[bool, ReconciliationCase | None]:
        """One locally-live order absent from open_orders, put to orderStatus.

        Returns ``(settled, case)``. The §8.3 rule-10 evidence split decides
        the unknownOid answer: durable proof the exchange took the cloid makes
        "I don't know it" a MISMATCH (never a licence to resend); no proof
        means the send never landed, and the row is settled as rejected.
        """
        cloid = row["cloid_hex"]
        order_id = row["order_id"]
        try:
            payload = self._query_order_by_cloid(cloid)
            parsed = parse_order_status(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orderStatus for cloid %s failed: %s", cloid, exc)
            return False, ReconciliationCase(
                case_type="order_missing_on_exchange",
                symbol=row["symbol"],
                local_value=order_id,
                exchange_value=cloid,
                detail=f"absent from open_orders and orderStatus failed: {exc}",
            )
        if parsed is None:
            if repo.has_exchange_known_cloid(self._db.conn, cloid_hex=cloid):
                return False, ReconciliationCase(
                    case_type="order_missing_on_exchange",
                    symbol=row["symbol"],
                    local_value=order_id,
                    exchange_value=cloid,
                    detail=(
                        "orderStatus answers unknownOid but durable local evidence "
                        "says the exchange took this cloid (§8.3 rule 10) — "
                        "unresolvable here, never resent"
                    ),
                )
            # No proof the exchange ever saw it: the send never landed.
            with self._db.transaction() as conn:
                repo.update_order(
                    conn,
                    order_id,
                    status="rejected",
                    status_reason="send_never_reached_exchange",
                    updated_at=now,
                )
            return True, ReconciliationCase(
                case_type="order_missing_on_exchange",
                symbol=row["symbol"],
                local_value=order_id,
                exchange_value=cloid,
                detail="unknownOid with no §8.3 rule-10 evidence — settled as rejected",
                action_taken="settled_never_sent",
                resolved=True,
            )
        exchange_order_id, raw_status = parsed
        local_status = local_status_for_exchange_status(raw_status)
        if local_status in repo.LIVE_ORDER_STATUSES:
            # Still live per orderStatus; open_orders was merely behind. Not a
            # §12.3 case — two eventually-consistent exchange reads disagreeing
            # for a moment is not a local/exchange conflict.
            return True, None
        with self._db.transaction() as conn:
            repo.update_order(
                conn,
                order_id,
                status=local_status,
                exchange_order_id=exchange_order_id,
                exchange_status=local_status,
                exchange_raw_status=raw_status,
                updated_at=now,
            )
        return True, ReconciliationCase(
            case_type="order_missing_on_exchange",
            symbol=row["symbol"],
            local_value=order_id,
            exchange_value=cloid,
            detail=f"settled from orderStatus: {raw_status}",
            action_taken=f"settled_{local_status}",
            resolved=True,
        )

    # ---------------------------------------------------------- position leg

    def _reconcile_positions(
        self,
        open_orders: list | None,
        snapshot: AccountSnapshot | None,
        cases: list[ReconciliationCase],
    ) -> tuple[bool, bool]:
        """§12.3 position rows + the SL-protection invariant → (ok, protected)."""
        if snapshot is None:
            return False, False
        ok = True
        conn = self._db.conn

        # Any exchange position OUTSIDE the configured symbol is unknown to
        # this run — §13.5 "unknown exchange position" → manual.
        for pos in snapshot.positions:
            if pos.coin != self._coin:
                ok = False
                cases.append(
                    ReconciliationCase(
                        case_type="exchange_position_mismatch",
                        symbol=pos.coin,
                        local_value=None,
                        exchange_value=str(pos.size),
                        detail=(
                            f"exchange holds a {pos.coin} position but this run trades "
                            f"only {self._coin} — unknown position (§13.5), manual safe mode"
                        ),
                        manual=True,
                    )
                )

        exch = snapshot.position_for(self._coin)
        exch_size = Decimal(0) if exch is None else exch.size
        local = repo.get_current_position(conn, self._run_id, self._coin)
        local_size = Decimal(0) if local is None else local.size

        if exch_size != local_size:
            ok = False
            if exch_size == 0 and local_size != 0:
                case_type, detail = (
                    "local_position_phantom",
                    "SQLite has a position but the exchange is flat — the exchange is "
                    "the truth source; the closing fills must be booked (backfill), "
                    "never fabricated (§12.3)",
                )
            elif exch_size != 0 and local_size == 0:
                case_type, detail = (
                    "exchange_position_mismatch",
                    "exchange has a position but SQLite believes flat — new entries stop "
                    "until the missing fills are booked (§12.3)",
                )
            else:
                case_type, detail = (
                    "exchange_position_mismatch",
                    "position sizes differ — fills are missing or double-booked (§12.3)",
                )
            cases.append(
                ReconciliationCase(
                    case_type=case_type,
                    symbol=self._coin,
                    local_value=str(local_size),
                    exchange_value=str(exch_size),
                    detail=detail,
                )
            )

        # §12.3 last row / §17: an exchange position without a valid SL.
        protected = True
        if exch is not None:
            protected = self._has_valid_sl(open_orders, exch)
            if not protected:
                cases.append(
                    ReconciliationCase(
                        case_type="position_sl_missing",
                        symbol=self._coin,
                        local_value=None if local is None else str(local_size),
                        exchange_value=str(exch_size),
                        detail=(
                            "live position has no valid reduce-only SL covering its size "
                            "— repair is the PR 5 protection manager; until then the run "
                            "stays in safe mode (§17.1 rule 1)"
                        ),
                    )
                )
        return ok, protected

    def _has_valid_sl(self, open_orders: list | None, position: PerpPosition) -> bool:
        """A bot-owned reduce-only stop_loss order covering the full position.

        Coverage counts only orders on the CLOSING side of the CURRENT
        position: a stale SL left from an opposite-direction position (the bot
        was short, flipped long while down) rests reduce-only with the right
        role and size but would never protect the position it now shares a
        book with — counting it would silently pass §17.1 rule 1 on a
        position with no real stop.
        """
        if open_orders is None:
            return False
        conn = self._db.conn
        need = abs(position.size)
        closing_side = "A" if position.size > 0 else "B"  # sell closes long, buy closes short
        covered = Decimal(0)
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            cloid = order.get("cloid")
            if not isinstance(cloid, str):
                continue
            registry = repo.get_cloid_by_hex(conn, cloid)
            if registry is None or registry["order_role"] != "stop_loss":
                continue
            if registry["symbol"] != position.coin:
                continue
            if not bool(order.get("reduceOnly", False)):
                continue
            if order.get("side") != closing_side:
                continue
            try:
                covered += Decimal(str(order.get("sz")))
            except Exception:  # noqa: BLE001 — an unparseable size covers nothing
                continue
        return covered >= need

    # ----------------------------------------------------------- account leg

    def _reconcile_account(
        self,
        snapshot: AccountSnapshot | None,
        cases: list[ReconciliationCase],
        errors: list[str],
    ) -> bool:
        """§12.3 equity-tolerance row: exchange equity vs the local ledger."""
        if snapshot is None:
            return False
        ledger = repo.get_current_account_state(self._db.conn, self._run_id)
        if ledger is None:
            errors.append("no local current_account_state row — ledger genesis missing")
            return False
        # Local equity = wallet_balance (realized/fees/funding already folded)
        # + unrealized at the EXCHANGE's own mark — using their unrealized
        # isolates the comparison to ledger drift instead of mark drift.
        exch_unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
        local_equity = ledger.wallet_balance + exch_unrealized
        diff = abs(snapshot.account_value - local_equity)
        tolerance = max(EQUITY_TOLERANCE_ABS_USDC, snapshot.account_value * EQUITY_TOLERANCE_REL)
        if diff > tolerance:
            cases.append(
                ReconciliationCase(
                    case_type="equity_mismatch",
                    symbol=None,
                    local_value=str(local_equity),
                    exchange_value=str(snapshot.account_value),
                    detail=(
                        f"|exchange {snapshot.account_value} − local {local_equity}| = "
                        f"{diff} exceeds tolerance {tolerance} "
                        "(pending fees/funding cannot explain it) — safe mode (§12.3)"
                    ),
                )
            )
            return False
        return True

    # ------------------------------------------------------------- recording

    def _record(
        self,
        report: ReconciliationReport,
        snapshot: AccountSnapshot | None,
        raw_clearinghouse: Any,
        backfill_summary: BackfillSummary | None,
    ) -> None:
        """Persist the pass: case rows + the §16.3/§16.4 snapshot rows."""
        now = report.timestamp
        status = "ok" if report.clean else "mismatch"
        diff = json.dumps(
            {
                "trigger": report.trigger,
                "cases": [
                    {
                        "case_type": c.case_type,
                        "symbol": c.symbol,
                        "local": c.local_value,
                        "exchange": c.exchange_value,
                        "resolved": c.resolved,
                        # Severity survives into the durable record: two cases
                        # can share a case_type (position mismatch on our coin
                        # vs an unknown coin) yet differ on whether a human
                        # must decide.
                        "manual": c.manual,
                    }
                    for c in report.cases
                ],
                "errors": list(report.errors),
                "backfill": None
                if backfill_summary is None
                else {
                    "fetched": backfill_summary.fetched,
                    "applied": backfill_summary.applied,
                    "complete": backfill_summary.complete,
                },
            },
            sort_keys=True,
        )
        raw_path = None
        if self._payload_dir is not None and raw_clearinghouse is not None:
            raw_path = write_raw_payload(
                payload_dir=self._payload_dir,
                kind="clearinghouse",
                key=self._run_id,
                payload=raw_clearinghouse,
                now=now,
            )
        with self._db.transaction() as conn:
            for case in report.cases:
                wrote = repo.insert_exchange_reconciliation_event(
                    conn,
                    run_id=self._run_id,
                    trigger=report.trigger,
                    case_type=case.case_type,
                    symbol=case.symbol,
                    local_value=case.local_value,
                    exchange_value=case.exchange_value,
                    action_taken=case.action_taken,
                    detail=case.detail,
                    timestamp=now,
                )
                if not wrote and case.action_taken is not None and case.exchange_value:
                    # The once-per-fact dedupe swallowed this insert, but THIS
                    # pass resolved the fact (a retry settling an order the
                    # first pass could only record) — stamp the disposition on
                    # the existing row, or the backlog permanently shows as
                    # unresolved a case that was in fact settled.
                    existing = repo.get_exchange_reconciliation_case(
                        conn,
                        self._run_id,
                        case_type=case.case_type,
                        exchange_value=case.exchange_value,
                    )
                    if existing is not None and existing["action_taken"] is None:
                        repo.set_reconciliation_action(
                            conn, existing["event_id"], case.action_taken
                        )
            if backfill_summary is not None and backfill_summary.applied > 0:
                # §12.3 row 5, resolved in-pass: fills the exchange had that
                # SQLite lacked were booked through the PR 3 path. Not deduped
                # (no exchange_value): each pass that booked something is its
                # own event.
                repo.insert_exchange_reconciliation_event(
                    conn,
                    run_id=self._run_id,
                    trigger=report.trigger,
                    case_type="exchange_fill_missing_local",
                    symbol=self._coin,
                    action_taken="backfilled",
                    detail=(
                        f"booked {backfill_summary.applied} missing fill(s) via REST "
                        f"backfill ({backfill_summary.fetched} fetched, "
                        f"{backfill_summary.duplicate} duplicate)"
                    ),
                    timestamp=now,
                )
            if snapshot is not None:
                self._write_snapshots(conn, snapshot, status, diff, raw_path, now)

    def _write_snapshots(
        self,
        conn: Any,
        snapshot: AccountSnapshot,
        status: str,
        diff: str,
        raw_path: str | None,
        now: datetime,
    ) -> None:
        """§16.3/§16.4: snapshot rows carrying the exchange view + the verdict.

        Every money column is either the local ledger's own number or the
        exchange's verbatim figure (mark derived as positionValue/|size| —
        arithmetic on exchange numbers, not a model; maintenance margin is the
        account-level ``crossMaintenanceMarginUsed``, which under the enforced
        single-symbol constraint (§25 #4) is this position's). Nothing is
        fabricated: rows are SKIPPED (with a warning) when the exchange did not
        report a figure a NOT NULL column needs — never written with a guessed 0.
        """
        ledger = repo.get_current_account_state(conn, self._run_id)
        if ledger is None or snapshot.cross_maintenance_margin_used is None:
            logger.warning(
                "skipping reconciliation snapshot rows: %s",
                "no local ledger" if ledger is None else "no crossMaintenanceMarginUsed",
            )
            return
        exch_unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
        equity = ledger.wallet_balance + exch_unrealized
        total_notional = snapshot.total_position_notional
        if total_notional is None:
            total_notional = sum(
                (p.position_value for p in snapshot.positions if p.position_value is not None),
                Decimal(0),
            )
            if snapshot.positions and total_notional == 0:
                # Every open position lacked positionValue: a 0 here would read
                # as "no exposure" on an account that demonstrably has some —
                # the same zero-looks-like-flat trap AccountSnapshot guards for
                # account_value. Skip the row rather than write a guessed 0.
                logger.warning(
                    "skipping reconciliation snapshot rows: open positions carry no "
                    "positionValue to derive total notional from"
                )
                return
        repo.insert_account_snapshot(
            conn,
            timestamp=now,
            mode="live",
            run_id=self._run_id,
            wallet_balance=ledger.wallet_balance,
            account_equity=equity,
            available_balance=snapshot.withdrawable,
            realized_pnl=ledger.realized_pnl,
            unrealized_pnl=exch_unrealized,
            total_pnl=ledger.realized_pnl + exch_unrealized,
            total_fees=ledger.total_fees,
            net_funding_pnl=ledger.net_funding_pnl,
            total_position_notional=total_notional,
            effective_leverage=None if equity <= 0 else total_notional / equity,
            used_initial_margin=snapshot.total_margin_used,
            total_maintenance_margin=snapshot.cross_maintenance_margin_used,
            margin_ratio=None if equity <= 0 else snapshot.cross_maintenance_margin_used / equity,
            exchange_account_value=snapshot.account_value,
            exchange_withdrawable=snapshot.withdrawable,
            exchange_margin_used=snapshot.total_margin_used,
            exchange_unrealized_pnl=exch_unrealized,
            exchange_raw_payload_path=raw_path,
            reconciliation_status=status,
            reconciliation_diff=diff,
        )
        exch = snapshot.position_for(self._coin)
        local = repo.get_current_position(conn, self._run_id, self._coin)
        if exch is not None:
            mark = (
                exch.position_value / abs(exch.size)
                if exch.position_value is not None and exch.size != 0
                else None
            )
            if mark is None:
                logger.warning(
                    "skipping reconciliation position snapshot: no positionValue to derive mark"
                )
                return
            protection = repo.get_position_protection(conn, self._run_id, self._coin)
            sl, tp = protection if protection is not None else (None, None)
            repo.insert_position_snapshot(
                conn,
                timestamp=now,
                mode="live",
                run_id=self._run_id,
                symbol=self._coin,
                position_size=exch.size,
                side="long" if exch.size > 0 else "short",
                entry_price=exch.entry_price,
                mark_price=mark,
                position_notional=exch.position_value,
                exposure_pct=None,
                unrealized_pnl=exch.unrealized_pnl,
                realized_pnl=Decimal(0) if local is None else local.realized_pnl,
                maintenance_margin=snapshot.cross_maintenance_margin_used,
                estimated_liquidation_price=None,
                exchange_liquidation_price=exch.liquidation_price,
                stop_loss_price=sl,
                take_profit_price=tp,
                exchange_position_size=exch.size,
                exchange_entry_price=exch.entry_price,
                exchange_unrealized_pnl=exch.unrealized_pnl,
                exchange_margin_used=exch.margin_used,
                exchange_raw_payload_path=raw_path,
                reconciliation_status=status,
                reconciliation_diff=diff,
            )
