"""Tests for the CSV export (phase2-data §1.1 timing/atomicity + §5–§12 columns)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.common import atomic_io
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.export import (
    EXPORT_SPECS,
    MANIFEST_NAME,
    ExportError,
    export_run,
)

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)

_EXPECTED_TABLES = (
    "ai_inputs",
    "decision_attempts",
    "ai_outputs",
    "orders",
    "fills",
    "funding_events",
    "account_snapshots",
    "position_snapshots",
)


def _init(tmp_path) -> Database:
    db = Database(tmp_path / "e.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return db


def _post_one_fill(db) -> None:
    accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|o1|0",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=D("0.001"),
        price=D(50000),
        fee_rate=D("0.00045"),
        timestamp=_T0,
    )


def test_exports_all_eight_csvs_with_contract_headers(tmp_path):
    db = _init(tmp_path)
    _post_one_fill(db)
    out = tmp_path / "exports"
    paths = export_run(db, run_id="r", output_dir=out)

    # Eight CSVs plus the set-coherence manifest, written (and returned) LAST.
    assert [p.name for p in paths] == [*(f"{t}.csv" for t in _EXPECTED_TABLES), MANIFEST_NAME]
    for (table, spec_cols, extra_cols), path in zip(EXPORT_SPECS, paths[:-1], strict=True):
        with path.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh))
        assert tuple(header) == (*spec_cols, *extra_cols), table
        assert not path.with_name(path.name + ".tmp").exists()

    # The fills CSV carries the posted fill with its stored-string values.
    with (out / "fills.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["fill_id"] == "r|o1|0"
    assert rows[0]["fill_qty"] == "0.001"
    assert rows[0]["fill_notional"] == "50.000"
    # Documented column prefix, then the schema-augmentation columns (§1.2).
    assert list(rows[0])[:3] == ["timestamp", "mode", "run_id"]
    assert "slice_id" in rows[0] and "fill_reason" in rows[0]
    # ai_inputs closes with the three segmentation-side augmentation columns:
    # the payload hash (v1), the prompt's shape (v10, issue #97) and the
    # format block's fingerprint (v11, issue #129).
    with (out / "ai_inputs.csv").open(encoding="utf-8", newline="") as fh:
        ai_header = next(csv.reader(fh))
    assert ai_header[-3:] == ["input_payload_hash", "context_shape", "format_fingerprint"]
    db.close()


def test_export_is_repeatable_and_scoped_to_run(tmp_path):
    db = _init(tmp_path)
    _post_one_fill(db)
    out = tmp_path / "exports"
    export_run(db, run_id="r", output_dir=out)
    # Re-export replaces the files in place (same content, no stray tmp files);
    # the only non-CSV in the directory is the manifest itself.
    export_run(db, run_id="r", output_dir=out)
    leftovers = [p.name for p in Path(out).iterdir() if p.suffix != ".csv"]
    assert leftovers == [MANIFEST_NAME]
    db.close()


def test_manifest_records_run_and_row_counts(tmp_path):
    db = _init(tmp_path)
    _post_one_fill(db)
    out = tmp_path / "exports"
    paths = export_run(db, run_id="r", output_dir=out)
    assert paths[-1] == out / MANIFEST_NAME
    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["run_id"] == "r"
    exported_at = datetime.fromisoformat(manifest["exported_at"])
    assert exported_at.tzinfo is not None and exported_at.utcoffset() == timedelta(0)
    # One entry per exported CSV, counting data rows (header excluded).
    assert set(manifest["files"]) == {f"{t}.csv" for t in _EXPECTED_TABLES}
    assert manifest["files"]["fills.csv"] == 1
    for name, count in manifest["files"].items():
        with (out / name).open(encoding="utf-8", newline="") as fh:
            assert count == sum(1 for _ in csv.reader(fh)) - 1, name
    db.close()


def test_failed_manifest_write_leaves_no_tmp(tmp_path, monkeypatch):
    db = _init(tmp_path)
    _post_one_fill(db)
    out = tmp_path / "exports"
    real_replace = atomic_io.os.replace

    def _boom_on_manifest(src, dst):
        if Path(dst).name == MANIFEST_NAME:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", _boom_on_manifest)
    with pytest.raises(ExportError, match="disk full"):
        export_run(db, run_id="r", output_dir=out)
    # The eight CSVs landed; the torn set has no manifest and no stray .tmp.
    assert sorted(p.name for p in Path(out).iterdir()) == sorted(
        f"{t}.csv" for t in _EXPECTED_TABLES
    )
    db.close()


def test_unknown_run_raises_and_writes_nothing(tmp_path):
    db = _init(tmp_path)
    out = tmp_path / "exports"
    with pytest.raises(ExportError, match="ghost"):
        export_run(db, run_id="ghost", output_dir=out)
    assert not any(Path(out).glob("*.csv"))
    db.close()


def test_failed_replace_leaves_no_half_written_csv(tmp_path, monkeypatch):
    db = _init(tmp_path)
    _post_one_fill(db)
    out = tmp_path / "exports"

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", _boom)
    with pytest.raises(ExportError, match="disk full"):
        export_run(db, run_id="r", output_dir=out)
    # §1.1: readers can never observe a partial official CSV — and the failed
    # attempt cleans up its tmp file too.
    assert not any(Path(out).glob("*.csv"))
    assert not any(Path(out).glob("*.tmp"))
    db.close()


def test_export_failure_does_not_touch_db_state(tmp_path, monkeypatch):
    db = _init(tmp_path)
    _post_one_fill(db)
    before = db.conn.execute("SELECT wallet_balance FROM current_account_state").fetchone()[0]
    monkeypatch.setattr(atomic_io.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ExportError):
        export_run(db, run_id="r", output_dir=tmp_path / "exports")
    after = db.conn.execute("SELECT wallet_balance FROM current_account_state").fetchone()[0]
    assert before == after
    db.close()
