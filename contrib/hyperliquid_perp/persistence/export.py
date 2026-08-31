"""CSV export of one run's full dataset (phase2-data §1.1 / §4–§12).

CSV is a *view* of SQLite, never a second source of truth: each export dumps the
complete ``run_id`` dataset for the eight logical tables, one CSV per table,
with the column set and order pinned to the phase2-data field tables (§5.2,
§6.2, §7.2, §8.2, §9.2, §10, §11.2, §12.2). Columns the data doc added to the
SQLite schemas after those tables were written (§1.2: ``slice_id`` 等增補 —
``fills``' slice/plan provenance, ``input_payload_hash``, the ``updated_at``
stamps) are appended *after* the spec columns so the documented prefix never
shifts.

Atomicity (§1.1): each CSV is written to ``<name>.csv.tmp`` in the same
directory, flushed and closed, then moved over ``<name>.csv`` with
``os.replace`` — a reader can never observe a half-written official CSV. All
eight table reads run inside one ``read_transaction`` snapshot, so an export
taken while the engine is writing is internally consistent across files.

Failure contract (§1.1): any failure raises :class:`ExportError`; the caller
records ``export_failed`` and carries on — an export must never roll back
committed trading state or stop the monitor / protection loops. Values are
written exactly as stored (Decimal TEXT, ISO-8601 UTC timestamps, 0/1
booleans), so exporting is lossless and repeatable.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..common.atomic_io import atomic_write_text
from .db import Database
from .repository import get_run

__all__ = ["EXPORT_SPECS", "MANIFEST_NAME", "ExportError", "export_run"]

# Written LAST, after every CSV landed: per-file atomicity (§1.1) cannot make
# the eight-file *set* atomic, so the manifest is the set-coherence marker a
# reader checks before cross-file joins.
MANIFEST_NAME = "manifest.json"

logger = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """A CSV export failed; committed DB state is untouched (phase2-data §1.1)."""


# (table, spec columns §5–§12 in documented order, appended schema-augmentation
# columns). Explicit lists — never SELECT * — so a future schema migration
# cannot silently reorder or leak a column into the export contract.
EXPORT_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "ai_inputs",
        (
            "timestamp",
            "mode",
            "run_id",
            "input_id",
            "symbol",
            "candle_start",
            "candle_end",
            "mark_price",
            "mid_price",
            "funding_rate",
            "wallet_balance",
            "account_equity",
            "available_balance",
            "realized_pnl",
            "unrealized_pnl",
            "total_fees",
            "net_funding_pnl",
            "effective_leverage",
            "margin_ratio",
            "current_position_side",
            "current_position_size",
            "entry_price",
            "position_notional",
            "current_margin_pct",
            "configured_leverage",
            "estimated_liquidation_price",
            "stop_loss_price",
            "take_profit_price",
            "active_twap",
            "remaining_twap_qty",
            "last_fill_time",
            "max_target_margin_pct",
            "input_payload_path",
            "prompt_version",
            "model",
        ),
        ("input_payload_hash", "context_shape", "format_fingerprint"),
    ),
    (
        "decision_attempts",
        (
            "timestamp",
            "mode",
            "run_id",
            "decision_attempt_id",
            "scheduled_at",
            "input_id",
            "output_id",
            "attempt_count",
            "first_attempt_at",
            "last_attempt_at",
            "status",
            "error_type",
            "error_message",
            "next_decision_at",
        ),
        (),
    ),
    (
        "ai_outputs",
        (
            "timestamp",
            "mode",
            "run_id",
            "input_id",
            "decision_attempt_id",
            "output_id",
            "symbol",
            "decision_mode",
            "target_side",
            "requested_target_margin_pct",
            "approved_target_margin_pct",
            "risk_action",
            "risk_reason",
            "target_margin",
            "configured_leverage",
            "target_notional",
            "target_signed_notional",
            "current_signed_notional",
            "delta_notional",
            "mark_price",
            "account_equity",
            "confidence",
            "decision_reason",
            "key_risks",
            "order_created",
            "no_order_reason",
        ),
        (),
    ),
    (
        "orders",
        (
            "timestamp",
            "mode",
            "run_id",
            "order_id",
            "output_id",
            "exchange_order_id",
            "client_order_id",
            "parent_order_id",
            "flip_plan_id",
            "flip_leg",
            "symbol",
            "order_role",
            "side",
            "type",
            "price",
            "trigger_price",
            "qty",
            "filled_qty",
            "remaining_qty",
            "status",
            "status_reason",
            "reduce_only",
            "active_from",
        ),
        ("updated_at",),
    ),
    (
        "fills",
        (
            "timestamp",
            "mode",
            "run_id",
            "fill_id",
            "order_id",
            "exchange_fill_id",
            "exchange_order_id",
            "symbol",
            "side",
            "fill_qty",
            "fill_price",
            "fill_notional",
            "fee",
            "fee_rate",
            "realized_pnl_delta",
            "liquidity_type",
        ),
        ("slice_id", "plan_id", "flip_leg", "slice_index", "fill_reason"),
    ),
    (
        "funding_events",
        (
            "recorded_at",
            "funding_timestamp",
            "mode",
            "run_id",
            "funding_event_id",
            "symbol",
            "position_size",
            "mark_price",
            "signed_position_notional",
            "funding_rate",
            "funding_pnl",
            "status",
            "source",
        ),
        ("updated_at",),
    ),
    (
        "account_snapshots",
        (
            "timestamp",
            "mode",
            "run_id",
            "wallet_balance",
            "account_equity",
            "available_balance",
            "realized_pnl",
            "unrealized_pnl",
            "total_pnl",
            "total_fees",
            "net_funding_pnl",
            "total_position_notional",
            "effective_leverage",
            "used_initial_margin",
            "total_maintenance_margin",
            "margin_ratio",
        ),
        (),
    ),
    (
        "position_snapshots",
        (
            "timestamp",
            "mode",
            "run_id",
            "symbol",
            "position_size",
            "side",
            "entry_price",
            "mark_price",
            "position_notional",
            "exposure_pct",
            "unrealized_pnl",
            "realized_pnl",
            "maintenance_margin",
            "estimated_liquidation_price",
            "exchange_liquidation_price",
            "margin_tier_id",
            "maintenance_margin_rate",
            "maintenance_deduction",
            "liquidation_model_version",
            "stop_loss_price",
            "take_profit_price",
        ),
        (),
    ),
)


# Import-time contract check: a duplicated column (a spec column accidentally
# repeated in the augmentation list) would produce a CSV with a doubled header —
# SQLite would happily execute the SELECT, so nothing later catches it.
for _table, _spec, _extra in EXPORT_SPECS:
    _cols = (*_spec, *_extra)
    if len(set(_cols)) != len(_cols):
        _dupes = sorted({c for c in _cols if _cols.count(c) > 1})
        raise ValueError(f"EXPORT_SPECS[{_table!r}] lists duplicate column(s): {_dupes}")
del _table, _spec, _extra, _cols


def _write_csv_atomic(path: Path, header: tuple[str, ...], rows: list[tuple]) -> None:
    def body(fh) -> None:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)

    # The §1.1 tmp-then-replace dance lives in common.atomic_io; ``newline=""``
    # so the csv module owns the line endings.
    atomic_write_text(path, body, newline="")


def export_run(db: Database, *, run_id: str, output_dir: str | Path) -> list[Path]:
    """Export ``run_id``'s complete dataset as eight CSVs under ``output_dir``.

    Returns the written paths. Raises :class:`ExportError` on any failure —
    including an unknown ``run_id``, which would otherwise "succeed" with eight
    empty files and read as a run that produced no data.
    """
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        row_counts: dict[str, int] = {}
        # One read snapshot across all eight tables: an export racing the
        # engine's writer loops must not show a fill whose account snapshot
        # is missing (or vice versa) across files.
        with db.read_transaction() as conn:
            if get_run(conn, run_id) is None:
                raise ValueError(f"run {run_id!r} does not exist; nothing to export")
            for table, spec_cols, extra_cols in EXPORT_SPECS:
                columns = (*spec_cols, *extra_cols)
                rows = conn.execute(
                    f"SELECT {', '.join(columns)} FROM {table} WHERE run_id = ? ORDER BY rowid",
                    (run_id,),
                ).fetchall()
                path = out / f"{table}.csv"
                _write_csv_atomic(path, columns, [tuple(r) for r in rows])
                written.append(path)
                row_counts[f"{table}.csv"] = len(rows)
        # The set-coherence marker, written only after every CSV replaced
        # successfully: a torn set (mid-set crash, disk full at table 5) leaves
        # the previous manifest in place, whose row counts then disagree with
        # the newer files — a detectable signal instead of phantom orphans.
        manifest_path = out / MANIFEST_NAME
        manifest = {
            "run_id": run_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "files": row_counts,
        }
        atomic_write_text(
            manifest_path,
            lambda fh: fh.write(json.dumps(manifest, ensure_ascii=False, indent=2)),
            newline="",
        )
        written.append(manifest_path)
        return written
    except Exception as exc:
        # Uniform failure surface (§1.1): callers log export_failed and keep
        # trading; the wrapped cause stays on __cause__ for the post-mortem.
        raise ExportError(f"CSV export for run {run_id!r} failed: {exc}") from exc
