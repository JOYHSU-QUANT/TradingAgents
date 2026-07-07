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
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext

from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import DECIMAL_CONTEXT
from . import accounting

__all__ = ["MIN_CYCLES_FOR_PHASE3", "ValidationReport", "validate_run"]

MIN_CYCLES_FOR_PHASE3 = 30

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
    total_pnl: Decimal
    total_fees: Decimal
    net_funding_pnl: Decimal
    failures: tuple[str, ...]
    # Surfaced-but-not-gating conditions (e.g. an exposure_pct identity that
    # could not be verified because duplicate same-timestamp account snapshots
    # made its companion ambiguous). Kept apart from ``failures`` so the
    # count/failure identity below stays exact.
    warnings: tuple[str, ...] = ()

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

    @property
    def phase3_ready(self) -> bool:
        """Spec §5: enough cycles and a fully consistent, traceable store."""
        return (
            self.cycle_count >= MIN_CYCLES_FOR_PHASE3
            and self.orphan_order_count == 0
            and self.orphan_fill_count == 0
            and self.snapshot_mismatch_count == 0
            and self.accounting_replay_mismatch_count == 0
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
            f"total_pnl: {self.total_pnl}",
            f"total_fees: {self.total_fees}",
            f"net_funding_pnl: {self.net_funding_pnl}",
            f"phase3_ready: {'yes' if self.phase3_ready else 'no'}",
        ]
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"warning: {reason}" for reason in self.warnings)
        return lines


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return conn.execute(sql, params).fetchone()[0]


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


def _snapshot_mismatches(conn: sqlite3.Connection, run_id: str) -> tuple[list[str], list[str]]:
    """``(mismatches, warnings)`` over the stored snapshot rows.

    Every identity below restates how the engine's snapshot writer computed the
    value (execution §6.1 / §6.6, phase2-data §11–§12); recomputation runs under
    the same pinned context, so any inequality is real corruption or writer
    drift, never rounding noise. A position snapshot whose same-instant account
    companion is *missing* is a mismatch (the writer commits both in one
    transaction); one whose companion is *ambiguous* (duplicate timestamps)
    leaves ``exposure_pct`` unverifiable and is reported as a warning instead —
    silently skipping it would let a timestamp-duplicating writer bug reduce
    validation coverage.
    """
    bad: list[str] = []
    warnings: list[str] = []
    with localcontext(DECIMAL_CONTEXT):
        # ``rowid AS row_key``: on a table with an INTEGER PRIMARY KEY, a bare
        # ``rowid`` result column takes that column's name, not "rowid".
        account_rows = conn.execute(
            "SELECT rowid AS row_key, * FROM account_snapshots WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        for row in account_rows:
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
            leverage = _dec(row["effective_leverage"])
            ratio = _dec(row["margin_ratio"])
            ok = (
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
            if not ok:
                bad.append(f"account_snapshots rowid {row['row_key']}")

        position_rows = conn.execute(
            "SELECT rowid AS row_key, * FROM position_snapshots WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        for row in position_rows:
            size = Decimal(row["position_size"])
            mark = Decimal(row["mark_price"])
            entry = _dec(row["entry_price"])
            notional = Decimal(row["position_notional"])
            unrealized = Decimal(row["unrealized_pnl"])
            maint = Decimal(row["maintenance_margin"])
            rate = _dec(row["maintenance_margin_rate"])
            deduction = _dec(row["maintenance_deduction"])
            side = row["side"]
            ok = notional == abs(size * mark) and side == (
                "long" if size > 0 else ("short" if size < 0 else "flat")
            )
            if entry is not None:
                ok = ok and unrealized == size * (mark - entry)
            if rate is not None and deduction is not None:
                ok = ok and maint == notional * rate - deduction
            # exposure_pct references the same-instant account equity.
            exposure = _dec(row["exposure_pct"])
            if exposure is not None:
                matches = conn.execute(
                    "SELECT account_equity FROM account_snapshots"
                    " WHERE run_id = ? AND timestamp = ?",
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
            if not ok:
                bad.append(f"position_snapshots rowid {row['row_key']}")
    return bad, warnings


def validate_run(db: Database, *, run_id: str) -> ValidationReport:
    """Compute the §5 acceptance report for ``run_id`` (read-only)."""
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

        max_exposure: Decimal | None = None
        for (raw,) in conn.execute(
            "SELECT exposure_pct FROM position_snapshots"
            " WHERE run_id = ? AND exposure_pct IS NOT NULL",
            (run_id,),
        ):
            value = Decimal(raw)
            if max_exposure is None or value > max_exposure:
                max_exposure = value
        max_leverage: Decimal | None = None
        for (raw,) in conn.execute(
            "SELECT effective_leverage FROM account_snapshots"
            " WHERE run_id = ? AND effective_leverage IS NOT NULL",
            (run_id,),
        ):
            value = Decimal(raw)
            if max_leverage is None or value > max_leverage:
                max_leverage = value

        last_unrealized = conn.execute(
            "SELECT unrealized_pnl FROM account_snapshots WHERE run_id = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()

        # Replay runs inside THIS snapshot (not its own) so the whole report —
        # counts, snapshot checks, and the replayed ledger the §5 totals come
        # from — describes one point in time even against a live writer.
        replayed = accounting.replay_within(conn, run_id=run_id)
    replay_mismatch_count = len(replayed.position_mismatches) + (
        0 if replayed.account_matches else 1
    )
    with localcontext(DECIMAL_CONTEXT):
        unrealized = Decimal(last_unrealized[0]) if last_unrealized is not None else Decimal(0)
        total_pnl = (
            replayed.ledger.realized_pnl
            - replayed.ledger.total_fees
            + replayed.ledger.net_funding_pnl
            + unrealized
        )

    failures: list[str] = []
    failures.extend(f"orphan order {order_id}" for order_id in orphan_orders)
    failures.extend(f"orphan fill {fill_id}" for fill_id in orphan_fills)
    failures.extend(f"snapshot identity failed: {where}" for where in snapshot_mismatches)
    failures.extend(
        f"replay position mismatch: {symbol}" for symbol in replayed.position_mismatches
    )
    if not replayed.account_matches:
        failures.append("replay ledger mismatch: current_account_state disagrees")

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
        total_pnl=total_pnl,
        total_fees=replayed.ledger.total_fees,
        net_funding_pnl=replayed.ledger.net_funding_pnl,
        failures=tuple(failures),
        warnings=tuple(warnings),
    )
