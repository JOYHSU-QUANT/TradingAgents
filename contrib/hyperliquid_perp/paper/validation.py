"""Acceptance validator for one paper run (phase2-spec §5).

Read-only: recomputes what the spec says must be recomputable and counts what
must be traceable, entirely from the SQLite store —

- the 13 summary metrics (§5 驗收輸出指標);
- chain integrity: every order traces to a persisted ``output_id`` (system
  reduce-only orders — SL / TP / liquidation / emergency closes — instead must
  correspond to a position that existed, i.e. an earlier fill or a seed
  position), and every fill references a persisted order;
- snapshot recomputability: each ``account_snapshots`` / ``position_snapshots``
  row must satisfy its own arithmetic identities under the pinned
  ``DECIMAL_CONTEXT`` (the writers computed them under the same pin, so
  equality is exact, not approximate);
- accounting replay consistency (:func:`accounting.replay`).

The Phase-3 gate (§5 可以進 Phase 3 的條件): ``cycle_count >= 30`` and zero
orphans / snapshot mismatches / replay mismatches. ``total_pnl > 0`` is
explicitly *not* a criterion.

A checker that *raises* on a corrupt store (a cell ``Decimal()`` cannot read,
a replay that cannot complete) is contained as an integrity outcome — a
counted failure line and the CLI's exit-5 lane, never a generic crash —
extending :func:`reconcile.classify_replay`'s "unverifiable books are an
outcome" rule to the acceptance report. Metrics the raise makes underivable
print as ``n/a`` (never fabricated); everything computed before it survives.

Non-gating ``warnings`` also surface store-persisted completeness signals the
operator would otherwise only find in a dead process's log: funding events
still pending long past settlement (their P&L is uncounted by design) and a
config drift recorded at the last resume.

One deliberate exception to "read-only": this module also owns the shared
stale-feed escalation policy (issue #50) — the streak threshold, the store
query behind it, the shortfall wording, and the per-cycle log escalation the
two RUNNING loops call (:func:`note_stale_feed_refusal`). It lives here
because the paper and live *validators* are its other two consumers and this
is the module they already share; a reader should not take the "read-only"
sentence above as covering that one function.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from ..common.constants import STALE_MARKET_DATA_ERROR
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import DECIMAL_CONTEXT
from . import accounting
from .reconcile import STALE_PENDING_FUNDING
from .scheduler import CYCLE_INTERVAL, parse_instant

__all__ = [
    "MIN_CYCLES_FOR_PHASE3",
    "STALE_FEED_STREAK_THRESHOLD",
    "ValidationReport",
    "note_stale_feed_refusal",
    "stale_feed_refusal_streak",
    "stale_feed_shortfall",
    "validate_run",
]

logger = logging.getLogger(__name__)

MIN_CYCLES_FOR_PHASE3 = 30

# Consecutive trailing stale-feed refusals (api_failed whose message carries
# STALE_CONTEXT_MARKER) at or past which the run is "not at the gate": its
# decisions have been blocked by a stalled candle feed or a slow host clock for
# this many cycles with nothing but a per-cycle log warning to show for it
# (issue #50). Three = 12h of the 4h cadence, the same "three decision cycles"
# the freshness guard itself bounds candle age at, and the count the repo's
# other escalations use (funding-source ERROR, repeated-mismatch manual safe
# mode). A shortfall (exit 4), never an integrity failure: the store is sound,
# the FEED is not, and the streak clears by itself the moment a cycle reaches
# a decision again — so an exchange maintenance window reads as "not yet", not
# as a verdict that survives it.
STALE_FEED_STREAK_THRESHOLD = 3

# Cycles that actually produced (or fail-closed) a decision. api_failed is
# deliberately NOT counted toward cycle_count / the ≥30 gate: spec §3.1 grants
# "此 cycle 視為已完成" to invalid_output only, and an api_failed cycle never
# exercised the decision→order→fill chain the 30-cycle run exists to validate.
# It is reported separately as api_failed_count.
_COMPLETED_CYCLE_STATUSES = ("completed", "invalid_output")

# Import-time completeness guard: this subset must be exactly "every terminal
# attempt status except api_failed". A new terminal status added to the
# canonical vocabulary fails here, forcing an explicit decision on whether it
# counts toward the ≥30-cycle gate instead of being silently missed.
if set(repo.TERMINAL_ATTEMPT_STATUSES) - set(_COMPLETED_CYCLE_STATUSES) != {"api_failed"}:
    raise ValueError(
        "_COMPLETED_CYCLE_STATUSES must cover every terminal attempt status "
        "except api_failed; the vocabulary drifted"
    )


@dataclass(frozen=True)
class ValidationReport:
    """The §5 summary metrics plus the Phase-3 verdict for one run."""

    run_id: str
    cycle_count: int
    api_failed_count: int
    order_count: int
    fill_count: int
    rejected_order_count: int
    orphan_order_count: int
    orphan_fill_count: int
    snapshot_mismatch_count: int
    accounting_replay_mismatch_count: int
    max_exposure_pct: Decimal | None
    max_effective_leverage: Decimal | None
    # The three replay-derived ledger totals are None — printed ``n/a`` — when
    # the accounting replay itself raised (books unverifiable, a counted
    # failure); a fabricated 0 would misread as a verified flat ledger.
    realized_pnl: Decimal | None
    # Unrealized leg of ``total_pnl``, valued at ``unrealized_as_of`` (the last
    # account snapshot's mark, up to ~one cycle stale) — surfaced separately so
    # the headline ``total_pnl`` doesn't silently fold a stale/absent valuation.
    # ``unrealized_as_of`` is None when the run has no account snapshot at all,
    # in which case the unrealized leg is 0 (an open position reads as flat for
    # PnL — the staleness the split makes explicit). The leg itself is None
    # when the stored cell is unreadable (that row is already a counted
    # snapshot failure): unavailable, not zero.
    unrealized_pnl: Decimal | None
    unrealized_as_of: str | None
    total_pnl: Decimal | None
    total_fees: Decimal | None
    net_funding_pnl: Decimal | None
    failures: tuple[str, ...]
    # Surfaced-but-not-gating conditions (e.g. an exposure_pct identity that
    # could not be verified because duplicate same-timestamp account snapshots
    # made its companion ambiguous). Kept apart from ``failures`` so the
    # count/failure identity below stays exact.
    warnings: tuple[str, ...] = ()
    # How many of the run's most recent terminal cycles — counted back from
    # the newest, stopping at the first that is not one — were api_failed with
    # a stale-feed refusal (issue #50). At or past STALE_FEED_STREAK_THRESHOLD
    # it blocks ``phase3_ready`` and prints as a ``shortfall:`` line.
    stale_feed_refusal_streak: int = 0

    def __post_init__(self) -> None:
        # Every gating count must have exactly one printed failure line and
        # vice versa — a future category added to one side but not the other
        # would print a report whose counts and reasons disagree.
        expected = (
            self.orphan_order_count
            + self.orphan_fill_count
            + self.snapshot_mismatch_count
            + self.accounting_replay_mismatch_count
        )
        if len(self.failures) != expected:
            raise ValueError(
                f"ValidationReport.failures has {len(self.failures)} entries but the "
                f"gating counts sum to {expected}"
            )
        # No account snapshot means the unrealized leg was valued at 0 (the split
        # the docstring above promises). A non-zero (or absent) unrealized_pnl
        # with a None as_of would print a self-contradicting summary
        # ("unrealized: 5" next to "as_of: n/a … valued at 0"); enforce the
        # documented pairing. (None != 0, so the absent case is rejected too.)
        if self.unrealized_as_of is None and self.unrealized_pnl != 0:
            raise ValueError(
                "ValidationReport.unrealized_pnl must be 0 when unrealized_as_of is None "
                f"(no account snapshot), got {self.unrealized_pnl}"
            )
        # The ledger trio comes from one replay: absent only together, and only
        # as a *counted* integrity failure — an n/a ledger beside a clean
        # replay count would claim the books verified while hiding the totals.
        ledger = (self.realized_pnl, self.total_fees, self.net_funding_pnl)
        if any(v is None for v in ledger):
            if not all(v is None for v in ledger):
                raise ValueError(
                    "ValidationReport ledger fields (realized_pnl, total_fees, "
                    "net_funding_pnl) come from one replay and must be absent together"
                )
            if self.accounting_replay_mismatch_count == 0:
                raise ValueError(
                    "ValidationReport ledger fields are absent (replay raised) but "
                    "accounting_replay_mismatch_count is 0 — an uncounted integrity failure"
                )
        # total_pnl is derivable exactly when every component is.
        component_missing = self.realized_pnl is None or self.unrealized_pnl is None
        if (self.total_pnl is None) != component_missing:
            raise ValueError(
                "ValidationReport.total_pnl must be absent exactly when a component "
                "(ledger trio or unrealized leg) is absent"
            )

    @property
    def shortfalls(self) -> tuple[str, ...]:
        """Not-yet-at-the-gate reasons that get a printed cause (exit 4).

        Derived, not stored: today the stale-feed streak is the only one, and
        a second field holding what ``phase3_ready`` already computes from
        ``stale_feed_refusal_streak`` could only ever disagree with it. (The
        other exit-4 cause, ``cycle_count < 30``, is legible from its own
        summary line and has never carried a reason line.) The live report's
        field is stored because six independent conditions feed it.
        """
        if self.stale_feed_refusal_streak >= STALE_FEED_STREAK_THRESHOLD:
            return (stale_feed_shortfall(self.stale_feed_refusal_streak),)
        return ()

    @property
    def phase3_ready(self) -> bool:
        """Spec §5: enough cycles and a fully consistent, traceable store.

        ...and a feed that is currently delivering decisions: a run whose last
        three cycles were all refused as stale is not ready however many
        cycles it accumulated before the feed (or the host clock) went wrong.
        """
        return (
            self.cycle_count >= MIN_CYCLES_FOR_PHASE3
            and self.orphan_order_count == 0
            and self.orphan_fill_count == 0
            and self.snapshot_mismatch_count == 0
            and self.accounting_replay_mismatch_count == 0
            and self.stale_feed_refusal_streak < STALE_FEED_STREAK_THRESHOLD
        )

    def summary_lines(self) -> list[str]:
        """The report as printable lines (the CLI's output shape)."""

        def _fmt(value: Decimal | None) -> str:
            return "n/a" if value is None else str(value)

        lines = [
            f"run_id: {self.run_id}",
            f"cycle_count: {self.cycle_count}",
            f"api_failed_count: {self.api_failed_count}",
            f"order_count: {self.order_count}",
            f"fill_count: {self.fill_count}",
            f"rejected_order_count: {self.rejected_order_count}",
            f"orphan_order_count: {self.orphan_order_count}",
            f"orphan_fill_count: {self.orphan_fill_count}",
            f"snapshot_mismatch_count: {self.snapshot_mismatch_count}",
            f"accounting_replay_mismatch_count: {self.accounting_replay_mismatch_count}",
            f"max_exposure_pct: {_fmt(self.max_exposure_pct)}",
            f"max_effective_leverage: {_fmt(self.max_effective_leverage)}",
            f"realized_pnl: {_fmt(self.realized_pnl)}",
            f"unrealized_pnl: {_fmt(self.unrealized_pnl)}",
            "unrealized_as_of: "
            + (self.unrealized_as_of or "n/a (no account snapshot; valued at 0)"),
            f"total_pnl: {_fmt(self.total_pnl)}",
            f"total_fees: {_fmt(self.total_fees)}",
            f"net_funding_pnl: {_fmt(self.net_funding_pnl)}",
            f"stale_feed_refusal_streak: {self.stale_feed_refusal_streak}",
            f"phase3_ready: {'yes' if self.phase3_ready else 'no'}",
        ]
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"shortfall: {reason}" for reason in self.shortfalls)
        lines.extend(f"warning: {reason}" for reason in self.warnings)
        return lines


def stale_feed_refusal_streak(conn: sqlite3.Connection, run_id: str) -> int:
    """Trailing consecutive stale-feed refusals, newest cycle first (issue #50).

    Walks the run's TERMINAL attempts from the most recent backwards and counts
    while each is an ``api_failed`` classed
    :data:`~...common.constants.STALE_MARKET_DATA_ERROR`; the first cycle that
    is anything else — a decision, an unparseable output, an api_failed from a
    network blip or an LLM timeout — ends the streak. So it measures "how long
    has the feed been blocking decisions RIGHT NOW", not "how often has it
    ever": a feed that recovers clears it at the next decided cycle, and
    RUNBOOK §7's expected occasional ``api_failed`` never counts. An
    ``in_progress`` attempt is skipped, not a break — it has not said anything
    yet. Shared by the paper and live validators.

    ``status`` is checked as well as ``error_type``, and not merely for
    belt-and-braces: a cycle whose FIRST try was refused as stale and whose
    retry then succeeded keeps the ``error_type`` from that try (the §3.1
    ladder patches the row, it never clears the column), so keying on the
    class alone would count a cycle that did produce a decision.

    The reverse scan is index-driven, not a sort: ``decision_attempts`` has
    ``UNIQUE (run_id, scheduled_at)``, whose implicit index satisfies this
    ORDER BY directly, so the cursor stays lazy and the early ``break`` really
    does stop reading. Dropping that constraint would silently turn this into
    a full sort of the run's history.
    """
    rows = conn.execute(
        "SELECT status, error_type FROM decision_attempts"
        " WHERE run_id = ? AND status != 'in_progress'"
        " ORDER BY scheduled_at DESC, rowid DESC",
        (run_id,),
    )
    streak = 0
    for row in rows:
        if row["status"] != "api_failed" or row["error_type"] != STALE_MARKET_DATA_ERROR:
            break
        streak += 1
    return streak


def _streak_hours(streak: int) -> int:
    """How long ``streak`` refused cycles have left the run without a decision.

    Derived from the scheduler's own cadence rather than a literal 4: the live
    driver already accepts a non-default ``cycle_interval``, and an operator
    reading "~12h with no decision" must not be told a number the schedule
    stopped agreeing with.
    """
    return int(streak * CYCLE_INTERVAL.total_seconds() // 3600)


def note_stale_feed_refusal(streak: int, error_type: str | None, *, run_id: str) -> int:
    """Advance (or reset) a loop's in-process stale-refusal streak and log it.

    The running half of the issue #50 escalation: ``validate`` judges by the
    durable streak in the store; this keeps the live one and turns the
    per-cycle WARNING into an ERROR from the threshold on — the same 3-strike
    log escalation the funding source uses — so a log scraper sees the change
    without a store query. Any api_failed that is NOT classed
    ``stale_market_data`` (a network blip, an LLM timeout, a non-retryable bug
    whose type is ``None``) resets it, exactly as it breaks the store-derived
    streak. Returns the new streak; the caller keeps it. In-process only: a
    restart starts the count over, and the store's streak is the one the
    verdict reads.
    """
    if error_type != STALE_MARKET_DATA_ERROR:
        return 0
    streak += 1
    if streak >= STALE_FEED_STREAK_THRESHOLD:
        logger.error(
            "decision cycle for %s refused as stale market data — %d consecutive "
            "(~%dh with no decision): the candle feed stopped advancing or this "
            "host's clock is off (the refusal message says which); `validate` "
            "reports this as a shortfall until a cycle decides again",
            run_id,
            streak,
            _streak_hours(streak),
        )
    else:
        logger.warning(
            "decision cycle for %s refused as stale market data (%d consecutive; "
            "escalates to ERROR and a validate shortfall at %d)",
            run_id,
            streak,
            STALE_FEED_STREAK_THRESHOLD,
        )
    return streak


def stale_feed_shortfall(streak: int) -> str:
    """The ``shortfall:`` line for a streak at or past the threshold (shared wording)."""
    return (
        f"stale_feed_refusal_streak = {streak} (the last {streak} cycles, "
        f"~{_streak_hours(streak)}h, were all refused as stale market data — the candle "
        f"feed stopped advancing or this host's clock is off; the refusal messages say "
        f"which. Decisions are blocked until the feed advances; the streak clears by "
        f"itself at the next decided cycle (need < {STALE_FEED_STREAK_THRESHOLD}))"
    )


def _dec_or_none(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _max_readable(conn: sqlite3.Connection, sql: str, run_id: str) -> Decimal | None:
    """Max of a single-column numeric scan, skipping cells ``Decimal()`` rejects
    — an unreadable cell's row is already a counted failure in the identity
    phase, so the maximum simply omits it."""
    best: Decimal | None = None
    for (raw,) in conn.execute(sql, (run_id,)):
        try:
            value = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if best is None or value > best:
            best = value
    return best


def _orphan_orders(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Orders that trace to nothing (spec §5: Decision → Order).

    Three orphan shapes: an ``output_id`` that resolves to no ``ai_outputs``
    row; a position-*opening* order with no ``output_id`` at all (only the AI
    decision path may open exposure); and a system reduce-only order (SL / TP /
    liquidation / emergency) for a symbol that never had a position to reduce —
    no fill at or before the order's timestamp and no seed position.
    """
    orphans: list[str] = []
    seeded = {p.coin for p in repo.get_run_seed_positions(conn, run_id)}
    for order in repo.iter_orders(conn, run_id):
        output_id = order["output_id"]
        if output_id is not None:
            found = conn.execute(
                "SELECT 1 FROM ai_outputs WHERE output_id = ?", (output_id,)
            ).fetchone()
            if found is None:
                orphans.append(order["order_id"])
            continue
        if not order["reduce_only"]:
            orphans.append(order["order_id"])
            continue
        if order["symbol"] in seeded:
            continue
        prior_fill = conn.execute(
            "SELECT 1 FROM fills WHERE run_id = ? AND symbol = ? AND timestamp <= ? LIMIT 1",
            (run_id, order["symbol"], order["timestamp"]),
        ).fetchone()
        if prior_fill is None:
            orphans.append(order["order_id"])
    return orphans


def _account_row_identities_ok(row: sqlite3.Row) -> bool:
    """One account_snapshots row's arithmetic identities (execution §6.1)."""
    wallet = Decimal(row["wallet_balance"])
    equity = Decimal(row["account_equity"])
    available = Decimal(row["available_balance"])
    unrealized = Decimal(row["unrealized_pnl"])
    used_im = Decimal(row["used_initial_margin"])
    maint = Decimal(row["total_maintenance_margin"])
    notional = Decimal(row["total_position_notional"])
    total_pnl = Decimal(row["total_pnl"])
    expected_pnl = (
        Decimal(row["realized_pnl"])
        + unrealized
        - Decimal(row["total_fees"])
        + Decimal(row["net_funding_pnl"])
    )
    leverage = _dec_or_none(row["effective_leverage"])
    ratio = _dec_or_none(row["margin_ratio"])
    return (
        equity == wallet + unrealized
        and available == equity - used_im
        and total_pnl == expected_pnl
        and (
            (leverage is None and equity <= 0)
            or (leverage is not None and equity > 0 and leverage == notional / equity)
        )
        and (
            (ratio is None and maint == 0)
            or (ratio is not None and maint != 0 and ratio == equity / maint)
        )
    )


def _position_row_identities_ok(
    conn: sqlite3.Connection, run_id: str, row: sqlite3.Row, warnings: list[str]
) -> bool:
    """One position_snapshots row's identities, incl. the exposure companion."""
    size = Decimal(row["position_size"])
    mark = Decimal(row["mark_price"])
    entry = _dec_or_none(row["entry_price"])
    notional = Decimal(row["position_notional"])
    unrealized = Decimal(row["unrealized_pnl"])
    maint = Decimal(row["maintenance_margin"])
    rate = _dec_or_none(row["maintenance_margin_rate"])
    deduction = _dec_or_none(row["maintenance_deduction"])
    side = row["side"]
    ok = notional == abs(size * mark) and side == (
        "long" if size > 0 else ("short" if size < 0 else "flat")
    )
    if entry is not None:
        ok = ok and unrealized == size * (mark - entry)
    if rate is not None and deduction is not None:
        ok = ok and maint == notional * rate - deduction
    # exposure_pct references the same-instant account equity.
    exposure = _dec_or_none(row["exposure_pct"])
    if exposure is not None:
        matches = conn.execute(
            "SELECT account_equity FROM account_snapshots WHERE run_id = ? AND timestamp = ?",
            (run_id, row["timestamp"]),
        ).fetchall()
        if len(matches) == 1:
            equity = Decimal(matches[0][0])
            ok = ok and equity > 0 and exposure == notional / equity * 100
        elif not matches:
            # The writer commits both snapshots in one transaction; a
            # missing companion is store corruption, not a data gap.
            ok = False
        else:
            warnings.append(
                f"exposure_pct unverifiable for position_snapshots rowid "
                f"{row['row_key']}: {len(matches)} account snapshots share "
                f"timestamp {row['timestamp']}"
            )
    return ok


def _snapshot_mismatches(conn: sqlite3.Connection, run_id: str) -> tuple[list[str], list[str]]:
    """``(mismatches, warnings)`` over the stored snapshot rows.

    Every identity above restates how the engine's snapshot writer computed the
    value (execution §6.1 / §6.6, phase2-data §11–§12); recomputation runs under
    the same pinned context, so any inequality is real corruption or writer
    drift, never rounding noise. A position snapshot whose same-instant account
    companion is *missing* is a mismatch (the writer commits both in one
    transaction); one whose companion is *ambiguous* (duplicate timestamps)
    leaves ``exposure_pct`` unverifiable and is reported as a warning instead —
    silently skipping it would let a timestamp-duplicating writer bug reduce
    validation coverage.

    A row the checker cannot even read (a stored cell ``Decimal()`` rejects)
    is contained *per row* as a mismatch — corruption is exactly what the
    identity check exists to catch, so it must count toward the exit-5 verdict
    rather than crash the report (classify_replay's rule), and every other row
    still gets checked.
    """
    bad: list[str] = []
    warnings: list[str] = []
    with localcontext(DECIMAL_CONTEXT):
        for table, check in (
            ("account_snapshots", _account_row_identities_ok),
            (
                "position_snapshots",
                lambda row: _position_row_identities_ok(conn, run_id, row, warnings),
            ),
        ):
            # ``rowid AS row_key``: on a table with an INTEGER PRIMARY KEY, a
            # bare ``rowid`` result column takes that column's name, not "rowid".
            rows = conn.execute(
                f"SELECT rowid AS row_key, * FROM {table} WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    ok = check(row)
                except Exception as exc:  # noqa: BLE001 — unreadable row = corruption, an outcome
                    bad.append(f"{table} rowid {row['row_key']} (identity check raised: {exc})")
                    continue
                if not ok:
                    bad.append(f"{table} rowid {row['row_key']}")
    return bad, warnings


def validate_run(db: Database, *, run_id: str, now: datetime | None = None) -> ValidationReport:
    """Compute the §5 acceptance report for ``run_id`` (read-only).

    ``now`` (default: wall clock) is used only to age still-pending funding
    events for the non-gating staleness warning — every gating metric is
    derived purely from the store, so the report's verdict stays reproducible.
    """
    with db.read_transaction() as conn:
        if repo.get_run(conn, run_id) is None:
            raise ValueError(f"run {run_id!r} does not exist; nothing to validate")

        placeholders = ", ".join("?" for _ in _COMPLETED_CYCLE_STATUSES)
        cycle_count = _count(
            conn,
            f"SELECT COUNT(*) FROM decision_attempts WHERE run_id = ?"
            f" AND status IN ({placeholders})",
            (run_id, *_COMPLETED_CYCLE_STATUSES),
        )
        api_failed_count = _count(
            conn,
            "SELECT COUNT(*) FROM decision_attempts WHERE run_id = ? AND status = 'api_failed'",
            (run_id,),
        )
        stale_streak = stale_feed_refusal_streak(conn, run_id)
        order_count = _count(conn, "SELECT COUNT(*) FROM orders WHERE run_id = ?", (run_id,))
        fill_count = _count(conn, "SELECT COUNT(*) FROM fills WHERE run_id = ?", (run_id,))
        rejected_order_count = _count(
            conn,
            "SELECT COUNT(*) FROM orders WHERE run_id = ? AND status = 'rejected'",
            (run_id,),
        )
        orphan_orders = _orphan_orders(conn, run_id)
        orphan_fills = [
            row["fill_id"]
            for row in conn.execute(
                "SELECT f.fill_id AS fill_id FROM fills f"
                " LEFT JOIN orders o ON o.order_id = f.order_id"
                " WHERE f.run_id = ? AND o.order_id IS NULL",
                (run_id,),
            ).fetchall()
        ]
        snapshot_mismatches, warnings = _snapshot_mismatches(conn, run_id)

        max_exposure = _max_readable(
            conn,
            "SELECT exposure_pct FROM position_snapshots"
            " WHERE run_id = ? AND exposure_pct IS NOT NULL",
            run_id,
        )
        max_leverage = _max_readable(
            conn,
            "SELECT effective_leverage FROM account_snapshots"
            " WHERE run_id = ? AND effective_leverage IS NOT NULL",
            run_id,
        )

        last_snapshot = conn.execute(
            "SELECT unrealized_pnl, timestamp FROM account_snapshots WHERE run_id = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()

        # Store-persisted completeness signals for the non-gating warnings
        # below — read inside the same snapshot as everything else.
        pending_events = list(repo.iter_funding_events(conn, run_id, status="pending"))
        sched_state = repo.get_scheduler_state(conn, run_id)

        # Replay runs inside THIS snapshot (not its own) so the whole report —
        # counts, snapshot checks, and the replayed ledger the §5 totals come
        # from — describes one point in time even against a live writer. A
        # replay that *raises* (a stored value it cannot read) is an
        # unverifiable-books outcome — classify_replay's rule — counted as a
        # gating failure, never a crash: exit-5's "investigate the store" is
        # for exactly this operator.
        replay_raised: str | None = None
        try:
            replayed = accounting.replay_within(conn, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — unverifiable books are an outcome, not a crash
            replayed = None
            replay_raised = f"{type(exc).__name__}: {exc}"
    if replayed is None:
        replay_mismatch_count = 1
        realized = fees = funding = None
    else:
        replay_mismatch_count = len(replayed.position_mismatches) + (
            0 if replayed.account_matches else 1
        )
        realized = replayed.ledger.realized_pnl
        fees = replayed.ledger.total_fees
        funding = replayed.ledger.net_funding_pnl
    # The unrealized leg is valued at the LAST account snapshot's mark (up to
    # ~one cycle stale): an offline validator has no fresh mark, and fetching one
    # would make the report irreproducible against the store. The valuation
    # instant is surfaced as ``unrealized_as_of`` and the leg is split out so a
    # run ended holding a position can't misread as flat.
    if last_snapshot is not None:
        unrealized_as_of: str | None = last_snapshot[1]
        try:
            unrealized: Decimal | None = Decimal(last_snapshot[0])
        except (InvalidOperation, TypeError, ValueError):
            # Unreadable stored cell: that snapshot row is already a counted
            # failure above; the leg is unavailable, not zero.
            unrealized = None
    else:
        unrealized = Decimal(0)
        unrealized_as_of = None
    if realized is None or fees is None or funding is None or unrealized is None:
        total_pnl = None
    else:
        with localcontext(DECIMAL_CONTEXT):
            total_pnl = realized - fees + funding + unrealized

    failures: list[str] = []
    failures.extend(f"orphan order {order_id}" for order_id in orphan_orders)
    failures.extend(f"orphan fill {fill_id}" for fill_id in orphan_fills)
    failures.extend(f"snapshot identity failed: {where}" for where in snapshot_mismatches)
    if replayed is None:
        failures.append(f"accounting replay raised: {replay_raised} — books unverifiable")
    else:
        failures.extend(
            f"replay position mismatch: {symbol}" for symbol in replayed.position_mismatches
        )
        if not replayed.account_matches:
            failures.append("replay ledger mismatch: current_account_state disagrees")

    # Non-gating completeness warnings (surface, never gate — the exposure_pct
    # precedent): a funding hour still pending long past settlement will likely
    # never resolve, and its P&L is deliberately uncounted rather than
    # fabricated — the acceptance reader must see that the totals are partial.
    if now is None:
        now = datetime.now(timezone.utc)
    stale_pending = 0
    corrupt_pending = 0
    for event in pending_events:
        try:
            settlement = parse_instant(event["funding_timestamp"])
        except ValueError:
            # backfill_pending_funding's vocabulary: an unparseable stored
            # timestamp is a *corrupt* row, not a stale one — it resolves only
            # by repairing the store, so it gets its own warning line.
            corrupt_pending += 1
            continue
        if now - settlement >= STALE_PENDING_FUNDING:
            stale_pending += 1
    if stale_pending:
        warnings.append(
            f"{stale_pending} funding event(s) still pending more than "
            f"{STALE_PENDING_FUNDING} after settlement — their funding P&L is "
            "uncounted in net_funding_pnl (stored basis only, never fabricated)"
        )
    if corrupt_pending:
        warnings.append(
            f"{corrupt_pending} pending funding event(s) have an unparseable "
            "funding_timestamp — corrupt stored row(s); their funding P&L stays "
            "uncounted until the store is repaired"
        )
    if sched_state is not None and sched_state["last_config_drift_status"] == "drift":
        warnings.append(
            "config drift recorded at last resume "
            f"({sched_state['last_config_drift_at']}): "
            f"{sched_state['last_config_drift_error']} — cycles before that resume "
            "ran under different parameters than this aggregate implies"
        )

    return ValidationReport(
        run_id=run_id,
        cycle_count=cycle_count,
        api_failed_count=api_failed_count,
        order_count=order_count,
        fill_count=fill_count,
        rejected_order_count=rejected_order_count,
        orphan_order_count=len(orphan_orders),
        orphan_fill_count=len(orphan_fills),
        snapshot_mismatch_count=len(snapshot_mismatches),
        accounting_replay_mismatch_count=replay_mismatch_count,
        max_exposure_pct=max_exposure,
        max_effective_leverage=max_leverage,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        unrealized_as_of=unrealized_as_of,
        total_pnl=total_pnl,
        total_fees=fees,
        net_funding_pnl=funding,
        failures=tuple(failures),
        warnings=tuple(warnings),
        stale_feed_refusal_streak=stale_streak,
    )
