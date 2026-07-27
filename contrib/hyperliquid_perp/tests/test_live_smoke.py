"""§20.2 smoke-test runner harness — driven by a fake signed client (offline)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.live import smoke
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_T0 = datetime(2026, 7, 27, 3, 52, tzinfo=timezone.utc)
_D = Decimal


@dataclass
class _Ack:
    status: str = "filled"
    exchange_order_id: str | None = "oid-1"
    filled_size: Decimal | None = _D("0.001")
    average_price: Decimal | None = _D(60000)
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status in ("resting", "filled")

    @property
    def is_duplicate(self) -> bool:
        return False


@dataclass
class _Cancel:
    success: bool = True
    error: str | None = None


class _FakeSigned:
    """Records every wire action; behaviour tunable per method for failure tests."""

    agent_address = "0x" + "cc" * 20

    def __init__(self, *, place_ack=None, trigger_ack=None, cancel=None, raise_on=None):
        self._place_ack = place_ack or _Ack("filled")
        self._trigger_ack = trigger_ack or _Ack("resting", filled_size=None, average_price=None)
        self._cancel = cancel or _Cancel(True)
        self._raise_on = raise_on or set()
        self.calls: list[str] = []
        self.last_place_cloid: str | None = None
        self.queried_cloid: str | None = None

    def _log(self, name: str) -> None:
        self.calls.append(name)
        if name in self._raise_on:
            raise RuntimeError(f"boom in {name}")

    def health_check(self):
        self._log("health_check")

    def update_leverage(self, *, coin, leverage, is_cross=True):
        self._log("update_leverage")

    def place_ioc_limit(self, **k):
        self._log("place_ioc_limit")
        self.last_place_cloid = k.get("cloid_hex")
        return self._place_ack

    def place_trigger_order(self, **k):
        self._log("place_trigger_order")
        return self._trigger_ack

    def modify_trigger_order(self, **k):
        self._log("modify_trigger_order")
        return self._trigger_ack

    def cancel_by_cloid(self, **k):
        self._log("cancel_by_cloid")
        return self._cancel

    def cancel_by_oid(self, **k):
        self._log("cancel_by_oid")
        return self._cancel

    def query_order_by_cloid(self, h):
        self._log("query_order_by_cloid")
        self.queried_cloid = h
        return {"status": "open"}

    def schedule_cancel(self, *, cancel_at):
        self._log("schedule_cancel")

    def clear_scheduled_cancel(self):
        self._log("clear_scheduled_cancel")


@dataclass
class _Recovery:
    passed: bool = True


def _ctx(db, signed, *, dry_run=False, run_recovery=None, run_id="live-BTC") -> smoke.SmokeContext:
    return smoke.SmokeContext(
        signed=signed,
        db=db,
        run_id=run_id,
        coin="BTC",
        network="testnet",
        payload_dir=Path("."),
        owner_prefix="hta",
        mark_price=lambda: _D(60000),
        qty_step=_D("0.00001"),
        tick_size=_D(1),
        now=lambda: _T0,
        dry_run=dry_run,
        run_recovery=run_recovery,
    )


@pytest.fixture
def live_db(tmp_path):
    db = Database(tmp_path / "live.db")
    with db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="live-BTC",
            mode="live",
            created_at=_T0,
            initial_balance_usdc=_D(200),
            config_json="{}",
            schema_version=SCHEMA_VERSION,
        )
    return db


# -- happy path ------------------------------------------------------------


def test_full_suite_passes_and_gate_opens(live_db):
    with live_db:
        runner = smoke.SmokeTestRunner(
            _ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery())
        )
        executed = runner.run()
        assert [t.number for t in executed] == list(range(1, 19))
        passed, missing, failed, errored = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert passed
    assert missing == () and failed == ()


def test_results_are_persisted_per_test(live_db):
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery())).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == len(smoke.SMOKE_TESTS)
    assert {r["status"] for r in rows} == {"passed"}
    assert all(r["dry_run"] == 0 for r in rows)


# -- failure containment ---------------------------------------------------


def test_refused_order_is_a_failed_verdict(live_db):
    # place_ioc_limit returns an error ack → the fill test fails (not crash).
    signed = _FakeSigned(
        place_ack=_Ack(
            "error", exchange_order_id=None, filled_size=None, average_price=None, error="rejected"
        )
    )
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run()
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
        passed, _missing, failed, _errored = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert latest["multi_slice_fill"]["status"] == "failed"
    assert "multi_slice_fill" in failed
    assert not passed


def test_exception_in_action_is_an_error_verdict(live_db):
    signed = _FakeSigned(raise_on={"update_leverage"})
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run()
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["update_leverage"]
    assert row["status"] == "error"
    assert "boom" in row["error_message"]


def test_missing_recovery_seam_fails_restart_tests(live_db):
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=None)).run()
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["restart_reconciliation"]["status"] == "failed"
    assert "no run_recovery seam" in latest["restart_reconciliation"]["error_message"]


def test_unclean_recovery_fails_restart_tests(live_db):
    with live_db:
        smoke.SmokeTestRunner(
            _ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery(passed=False))
        ).run()
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["restart_reconciliation"]["status"] == "failed"


# -- dry-run ---------------------------------------------------------------


def test_dry_run_records_skipped_and_gate_stays_closed(live_db):
    with live_db:
        # signed=None: a dry run must never touch the client.
        smoke.SmokeTestRunner(_ctx(live_db, None, dry_run=True)).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
        passed, missing, failed, errored = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert {r["status"] for r in rows} == {"skipped"}
    assert all(r["dry_run"] == 1 for r in rows)
    # Dry-run rows never satisfy the gate: every test reads as "not yet run".
    assert not passed
    assert len(missing) == len(smoke.SMOKE_TESTS)
    assert failed == ()


def test_dry_run_then_real_run_supersedes(live_db):
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, None, dry_run=True)).run()
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery())).run()
        passed, _m, _f, _e = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert passed  # the real pass supersedes the earlier dry-run skips


# -- selection -------------------------------------------------------------


def test_only_runs_subset_in_canonical_order(live_db):
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned()))
        # Pass out of order; the runner restores canonical order.
        executed = runner.run(only=["update_leverage", "signed_client_init"])
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert [t.key for t in executed] == ["signed_client_init", "update_leverage"]
    assert len(rows) == 2


def test_validate_only_keys_rejects_unknown():
    with pytest.raises(ValueError, match="unknown smoke test key"):
        smoke.validate_only_keys(["signed_client_init", "bogus"])
    assert smoke.validate_only_keys(["emergency_close"]) == ("emergency_close",)


# -- gate report -----------------------------------------------------------


def test_gate_report_distinguishes_missing_from_failed(live_db):
    with live_db.transaction() as conn:
        repo.insert_smoke_test_result(
            conn,
            run_id="live-BTC",
            test_number=1,
            test_key="signed_client_init",
            test_name="x",
            status="passed",
            executed_at=_T0,
        )
        repo.insert_smoke_test_result(
            conn,
            run_id="live-BTC",
            test_number=2,
            test_key="update_leverage",
            test_name="x",
            status="failed",
            executed_at=_T0,
        )
    with live_db:
        passed, missing, failed, errored = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert not passed
    assert "update_leverage" in failed
    assert "signed_client_init" not in missing and "signed_client_init" not in failed
    # The 16 never-run tests are all missing.
    assert len(missing) == len(smoke.SMOKE_TESTS) - 2


def test_gate_report_splits_error_from_failed(live_db):
    # A harness/code bug (error verdict) must be triageable apart from an
    # exchange refusal (failed verdict) at the gate surface.
    signed = _FakeSigned(raise_on={"update_leverage"})  # → "error" verdict
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run()
        passed, _missing, failed, errored = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert "update_leverage" in errored
    assert "update_leverage" not in failed
    assert not passed


def test_failed_then_fixed_rerun_passes_the_key(live_db):
    # The RUNBOOK's fix-and-retry flow: a failed test rerun to passed unblocks.
    with live_db.transaction() as conn:
        for status in ("failed", "passed"):
            repo.insert_smoke_test_result(
                conn,
                run_id="live-BTC",
                test_number=2,
                test_key="update_leverage",
                test_name="x",
                status=status,
                executed_at=_T0,
            )
    with live_db:
        _p, _m, failed, _e = smoke.smoke_gate_report(live_db.conn, "live-BTC")
    assert "update_leverage" not in failed  # latest passed supersedes the earlier fail


# -- cross-test cloid handoff (test 3 → 4) --------------------------------


def test_status_test_queries_the_submitted_cloid(live_db):
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed)).run(
            only=["slice_order_submit", "slice_order_status"]
        )
    # The status test must query the exact cloid the submit test registered.
    assert signed.queried_cloid is not None
    assert signed.queried_cloid == signed.last_place_cloid


def test_status_without_prior_submit_aborts(live_db):
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned())).run(only=["slice_order_status"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["slice_order_status"]["status"] == "failed"
    assert "no prior submit" in latest["slice_order_status"]["error_message"]


def test_cleanup_failure_is_surfaced_in_step_detail(live_db):
    # A best-effort close that FAILS must leave a durable note in the step's
    # detail (a stray funded position cannot vanish from the audit trail).
    class _CloseFails(_FakeSigned):
        def place_ioc_limit(self, **k):
            self._log("place_ioc_limit")
            self.last_place_cloid = k.get("cloid_hex")
            if k.get("reduce_only"):  # the cleanup close
                raise RuntimeError("close boom")
            return self._place_ack

    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _CloseFails())).run(only=["multi_slice_fill"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["multi_slice_fill"]
    assert row["status"] == "passed"  # the fill itself still passed
    assert "cleanup" in (row["detail"] or "") and "FAILED" in row["detail"]
