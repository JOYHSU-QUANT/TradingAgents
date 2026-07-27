"""§20.2 smoke-test runner harness — driven by a fake signed client (offline)."""

from __future__ import annotations

import sqlite3
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


def test_missing_recovery_seam_aborts_a_full_suite_before_any_test(live_db):
    # A full-suite real run places probe orders, so the pre-flight recovery is
    # mandatory: no seam → the suite aborts with NOTHING recorded (an abort is
    # not a verdict).
    with live_db:
        with pytest.raises(smoke.SmokePreflightError, match="no run_recovery seam"):
            smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=None)).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 0


def test_missing_recovery_seam_fails_restart_tests(live_db):
    # A restart-only selection places no probe orders → no pre-flight → the
    # missing seam surfaces per-test as a failed verdict, not a suite abort.
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=None)).run(
            only=["restart_reconciliation"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["restart_reconciliation"]["status"] == "failed"
    assert "no run_recovery seam" in latest["restart_reconciliation"]["error_message"]


def test_unclean_preflight_aborts_and_still_disarms(live_db):
    # The pre-flight recovery may already have armed the switch before its
    # verdict came back unclean — the exit disarm must still fire.
    signed = _FakeSigned()
    with live_db:
        with pytest.raises(smoke.SmokePreflightError, match="did not pass"):
            smoke.SmokeTestRunner(
                _ctx(live_db, signed, run_recovery=lambda: _Recovery(passed=False))
            ).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 0
    assert "clear_scheduled_cancel" in signed.calls


def test_unclean_recovery_fails_restart_tests(live_db):
    with live_db:
        smoke.SmokeTestRunner(
            _ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery(passed=False))
        ).run(only=["restart_reconciliation"])
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
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
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
        smoke.SmokeTestRunner(_ctx(live_db, _CloseFails(), run_recovery=lambda: _Recovery())).run(
            only=["multi_slice_fill"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["multi_slice_fill"]
    assert row["status"] == "passed"  # the fill itself still passed
    assert "cleanup" in (row["detail"] or "") and "FAILED" in row["detail"]


def test_cleanup_refused_ack_is_surfaced_in_step_detail(live_db):
    # A best-effort close the exchange REJECTS at the per-order level returns an
    # unaccepted OrderAck (not an exception), so the ack must be checked — a
    # stray funded position must still leave a durable note, not vanish.
    class _CloseRefused(_FakeSigned):
        def place_ioc_limit(self, **k):
            self._log("place_ioc_limit")
            self.last_place_cloid = k.get("cloid_hex")
            if k.get("reduce_only"):  # the cleanup close
                return _Ack(
                    "error",
                    exchange_order_id=None,
                    filled_size=None,
                    average_price=None,
                    error="reduce-only rejected",
                )
            return self._place_ack

    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _CloseRefused(), run_recovery=lambda: _Recovery())).run(
            only=["multi_slice_fill"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["multi_slice_fill"]
    assert row["status"] == "passed"  # the fill itself still passed
    assert "cleanup" in (row["detail"] or "") and "refused" in row["detail"]


# -- kill-switch disarm on suite exit (Q2) --------------------------------


def test_suite_disarms_kill_switch_on_exit(live_db):
    # A restart test's recovery arms the dead man's switch; the suite must clear
    # it on exit so no armed scheduleCancel is left behind. Run a restart test
    # WITHOUT test 14 (which clears its own arm) so the only disarm is the
    # runner's finally — the fake recovery makes no wire calls of its own.
    signed = _FakeSigned()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=["restart_reconciliation"])
    assert signed.calls == ["clear_scheduled_cancel"]
    assert runner.kill_switch_disarm_failed is False


def test_non_arming_run_does_not_disarm(live_db):
    # A run that executed no switch-touching test never armed anything, so it must
    # NOT fire an account-wide clear (it could wipe a concurrent live --loop's own
    # arm on the same wallet).
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed)).run(only=["signed_client_init"])
    assert "clear_scheduled_cancel" not in signed.calls


def test_disarm_failure_sets_flag_without_raising(live_db):
    # A failed disarm must not raise (verdicts are already durable) but must record
    # kill_switch_disarm_failed so the CLI can warn the wallet may still be armed.
    signed = _FakeSigned(raise_on={"clear_scheduled_cancel"})
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=["restart_reconciliation"])  # does not raise
    assert "clear_scheduled_cancel" in signed.calls  # the disarm was attempted
    assert runner.kill_switch_disarm_failed is True


def test_dry_run_does_not_touch_kill_switch(live_db):
    # signed=None on a dry run: the disarm guard must skip (no crash, no wire call).
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, None, dry_run=True)).run(
            only=["restart_reconciliation"]
        )
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert [r["status"] for r in rows] == ["skipped"]


def test_insert_rejects_dry_run_row_that_is_not_skipped(live_db):
    # A dry-run row placed no orders, so its only honest verdict is "skipped" —
    # a dry_run=1/status='passed' write is rejected at the boundary.
    with (
        live_db.transaction() as conn,
        pytest.raises(ValueError, match="must be 'skipped'"),
    ):
        repo.insert_smoke_test_result(
            conn,
            run_id="live-BTC",
            test_number=1,
            test_key="signed_client_init",
            test_name="x",
            status="passed",
            dry_run=True,
            executed_at=_T0,
        )


# -- pre-flight recovery + probe booking (review round 2026-07-27) ---------


class _CountingRecovery:
    """A recovery seam that counts invocations (pre-flight vs restart tests)."""

    def __init__(self, passed: bool = True):
        self.calls = 0
        self._passed = passed

    def __call__(self):
        self.calls += 1
        return _Recovery(passed=self._passed)


def test_preflight_runs_one_recovery_ahead_of_the_restart_tests(live_db):
    recovery = _CountingRecovery()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=recovery)).run()
    # 1 pre-flight (before any probe order) + one per restart test (15/16/17).
    assert recovery.calls == 4


def test_non_order_selection_skips_the_preflight(live_db):
    recovery = _CountingRecovery()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=recovery)).run(
            only=["signed_client_init", "update_leverage", "kill_switch_arm_refresh"]
        )
    assert recovery.calls == 0


def test_dry_run_skips_the_preflight(live_db):
    # A dry run places nothing, so it needs no recovery seam even for a full
    # suite (signed=None would make a pre-flight impossible anyway).
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, None, dry_run=True)).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert {r["status"] for r in rows} == {"skipped"}


def test_probe_orders_are_booked_with_the_ack_oid(live_db):
    # The wedge fix: every IOC probe books an orders row carrying the ack's
    # exchange oid, so its fill resolves through the §14 oid→order mapping
    # instead of filing a permanent, unstampable fill_unmapped case.
    class _DistinctOids(_FakeSigned):
        # Real exchange oids are unique per order; the shared "oid-1" default
        # would (correctly) trip the mapping's ambiguity guard.
        def __init__(self):
            super().__init__()
            self._oid = 0

        def place_ioc_limit(self, **k):
            self._log("place_ioc_limit")
            self.last_place_cloid = k.get("cloid_hex")
            self._oid += 1
            return _Ack("filled", exchange_order_id=f"oid-{self._oid}")

    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _DistinctOids(), run_recovery=lambda: _Recovery())).run(
            only=["reduce_only_close"]
        )
        resolved = repo.get_order_by_exchange_order_id(live_db.conn, "oid-1")
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'"
            " ORDER BY exchange_order_id",
            ("live-BTC",),
        ).fetchall()
    assert resolved is not None
    assert resolved["run_id"] == "live-BTC" and resolved["symbol"] == "BTC"
    # Test 7 = one entry + one reduce-only close, both filled acks.
    assert len(probe_rows) == 2
    assert {r["order_role"] for r in probe_rows} == {"entry", "close"}
    assert all(r["status"] == "filled" for r in probe_rows)
    assert all(r["is_bot_owned"] == 1 for r in probe_rows)
    assert [r["exchange_order_id"] for r in probe_rows] == ["oid-1", "oid-2"]


def test_zero_fill_probe_books_as_canceled_never_open(live_db):
    # An accepted-but-unfilled IOC is terminal at the ack — booked "canceled",
    # never "open" (an open local row whose exchange order is gone would file
    # order_missing_on_exchange at the next recovery).
    signed = _FakeSigned(place_ack=_Ack("filled", filled_size=_D(0)))
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'",
            ("live-BTC",),
        ).fetchall()
    assert len(probe_rows) == 1
    assert probe_rows[0]["status"] == "canceled"


def test_error_ack_probe_books_nothing(live_db):
    # An error ack has no exchange oid — nothing existed on the exchange, so
    # nothing is booked locally either (test 3 tolerates the refused far IOC).
    signed = _FakeSigned(
        place_ack=_Ack(
            "error", exchange_order_id=None, filled_size=None, average_price=None, error="nope"
        )
    )
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'",
            ("live-BTC",),
        ).fetchall()
    assert probe_rows == []


def test_mid_suite_record_crash_still_disarms(live_db, monkeypatch):
    # The disarm lives in run()'s finally: a store failure BETWEEN tests (the
    # one seam that can raise out of the loop) must still clear the switch the
    # pre-flight armed.
    signed = _FakeSigned()
    real_insert = repo.insert_smoke_test_result
    seen = {"n": 0}

    def _boom(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] >= 2:
            raise sqlite3.OperationalError("store gone mid-suite")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(smoke.repo, "insert_smoke_test_result", _boom)
    with live_db, pytest.raises(sqlite3.OperationalError):
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run()
    assert "clear_scheduled_cancel" in signed.calls


# -- SL/TP cleanup + refusal paths (review round 2026-07-27) ----------------


def test_trigger_cleanup_cancel_exception_is_surfaced(live_db):
    # _best_effort_cancel mirrors _best_effort_close: a cleanup cancel that
    # RAISES must leave a durable note (a resting SL left live on the exchange
    # cannot vanish from the audit trail), while the create still passes.
    class _CancelBoom(_FakeSigned):
        def cancel_by_cloid(self, **k):
            self._log("cancel_by_cloid")
            raise RuntimeError("cancel boom")

    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _CancelBoom(), run_recovery=lambda: _Recovery())).run(
            only=["stop_loss_create"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["stop_loss_create"]
    assert row["status"] == "passed"
    assert "cleanup" in (row["detail"] or "") and "FAILED" in row["detail"]


def test_trigger_cleanup_cancel_refused_is_surfaced(live_db):
    # A cleanup cancel the exchange refuses (unsuccessful ack, no exception)
    # must leave the same durable note.
    with live_db:
        smoke.SmokeTestRunner(
            _ctx(
                live_db,
                _FakeSigned(cancel=_Cancel(False, "order not found")),
                run_recovery=lambda: _Recovery(),
            )
        ).run(only=["take_profit_modify"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["take_profit_modify"]
    assert row["status"] == "passed"
    assert "cleanup" in (row["detail"] or "") and "refused" in row["detail"]


def test_trigger_create_refusal_is_a_failed_verdict(live_db):
    # place_trigger_order returning an unaccepted ack must fail the test (an
    # exchange refusal, not a harness error) — the _require_accepted guard.
    signed = _FakeSigned(
        trigger_ack=_Ack(
            "error", exchange_order_id=None, filled_size=None, average_price=None, error="margin"
        )
    )
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["stop_loss_create"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["stop_loss_create"]
    assert row["status"] == "failed"
    assert "refused by the exchange" in row["error_message"]


def test_preflight_recovery_exception_is_a_preflight_abort(live_db):
    # The seam's third failure mode: run_recovery RAISES (e.g. §18 arming API
    # failure, which propagates by contract) instead of returning an unclean
    # result. It must become the same SmokePreflightError abort — exit 4 at the
    # CLI — not an uncaught crash into main()'s generic exit 2; the original
    # cause stays in the message, nothing is recorded, and the disarm fires.
    signed = _FakeSigned()

    def _boom():
        raise RuntimeError("scheduleCancel API down")

    with live_db:
        with pytest.raises(smoke.SmokePreflightError, match="scheduleCancel API down"):
            smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=_boom)).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 0
    assert "clear_scheduled_cancel" in signed.calls
