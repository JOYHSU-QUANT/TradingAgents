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

    def __init__(
        self,
        *,
        place_ack=None,
        place_acks=None,
        trigger_ack=None,
        modify_ack=None,
        cancel=None,
        raise_on=None,
        query_payload=None,
    ):
        # The REAL Info vocabulary (live/orders.py parse_order_status): a hit
        # is {"status": "order", ...}; the miss shape is {"status": "unknownOid"}.
        self._query_payload = query_payload or {
            "status": "order",
            "order": {"order": {"oid": 1}, "status": "filled"},
        }
        self._place_ack = place_ack or _Ack("filled")
        # Optional per-call sequence, consumed first (then _place_ack repeats):
        # lets a test make slice 0 fill and slice 1 fail.
        self._place_acks = list(place_acks or [])
        self._trigger_ack = trigger_ack or _Ack("resting", filled_size=None, average_price=None)
        self._modify_ack = modify_ack  # None → same ack as a fresh trigger place
        self._cancel = cancel or _Cancel(True)
        self._raise_on = raise_on or set()
        self.calls: list[str] = []
        self.leverage_calls: list[dict] = []
        self.last_place_cloid: str | None = None
        self.queried_cloid: str | None = None
        self.place_calls: list[dict] = []
        self.trigger_calls: list[dict] = []
        self.modify_calls: list[dict] = []
        self.cancelled_cloids: list[str] = []

    def _log(self, name: str) -> None:
        self.calls.append(name)
        if name in self._raise_on:
            raise RuntimeError(f"boom in {name}")

    def health_check(self):
        self._log("health_check")

    def update_leverage(self, *, coin, leverage, is_cross=True):
        self._log("update_leverage")
        self.leverage_calls.append({"coin": coin, "leverage": leverage, "is_cross": is_cross})

    def place_ioc_limit(self, **k):
        self._log("place_ioc_limit")
        self.last_place_cloid = k.get("cloid_hex")
        self.place_calls.append(k)
        if self._place_acks:
            return self._place_acks.pop(0)
        return self._place_ack

    def place_trigger_order(self, **k):
        self._log("place_trigger_order")
        self.trigger_calls.append(k)
        return self._trigger_ack

    def modify_trigger_order(self, **k):
        self._log("modify_trigger_order")
        self.modify_calls.append(k)
        return self._modify_ack if self._modify_ack is not None else self._trigger_ack

    def cancel_by_cloid(self, **k):
        self._log("cancel_by_cloid")
        self.cancelled_cloids.append(k.get("cloid_hex"))
        return self._cancel

    def cancel_by_oid(self, **k):
        self._log("cancel_by_oid")
        return self._cancel

    def query_order_by_cloid(self, h):
        self._log("query_order_by_cloid")
        self.queried_cloid = h
        return self._query_payload

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
    # The status test must query the exact cloid the submit test registered —
    # place_calls[0] is the submit (the default filled ack makes test 3 place a
    # cleanup close afterwards, so last_place_cloid is the CLOSE's cloid).
    assert signed.queried_cloid is not None
    assert signed.queried_cloid == signed.place_calls[0]["cloid_hex"]


def test_status_without_prior_submit_is_an_error_verdict(live_db):
    # Test 3 never left its handle in this process: a dependency cascade, not an
    # exchange refusal — the verdict is "error" (harness/selection bucket), so
    # the operator is pointed at test 3, never at the venue (decision 2026-07-29).
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned())).run(only=["slice_order_status"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_status"]
    assert row["status"] == "error"
    assert "no prior submit" in row["error_message"]
    assert "not an exchange refusal" in row["error_message"]


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


# -- round-1 review-loop fixes (2026-07-28) ---------------------------------


def test_trigger_probes_are_sell_side(live_db):
    # A BUY-SL below the mark (short protection) is already in its fired region
    # at placement — the exchange rejects it or instant-triggers it. Every
    # trigger probe is the long-protection SELL shape, both pairs.
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=[
                "slice_plan_cancel",
                "stop_loss_create",
                "stop_loss_modify",
                "take_profit_create",
                "take_profit_modify",
            ]
        )
    assert signed.trigger_calls and all(c["is_buy"] is False for c in signed.trigger_calls)
    assert signed.modify_calls and all(c["is_buy"] is False for c in signed.modify_calls)


def test_multi_slice_refusal_after_partial_fill_closes_the_filled_leg(live_db):
    # Slice 0 fills, slice 1 is refused → the verdict is failed AND the slice-0
    # fill is best-effort closed (test 6 must not strand a position on the
    # failure path either), with the cleanup noted durably.
    refused = _Ack(
        "error", exchange_order_id=None, filled_size=None, average_price=None, error="margin blip"
    )
    signed = _FakeSigned(place_acks=[_Ack("filled"), refused])
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["multi_slice_fill"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["multi_slice_fill"]
    assert row["status"] == "failed"
    assert "slice 1 was refused" in row["error_message"]
    assert "cleanup" in row["error_message"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1 and closes[0]["size"] == _D("0.001")


def test_multi_slice_exception_after_partial_fill_still_closes(live_db):
    # The harness-error lane (→ ``error`` verdict) must flatten the slice-0
    # fill too, not just the refusal lane.
    class _SecondSliceBoom(_FakeSigned):
        def place_ioc_limit(self, **k):
            entries = [c for c in self.place_calls if not c.get("reduce_only")]
            if not k.get("reduce_only") and len(entries) == 1:
                self.place_calls.append(k)
                raise RuntimeError("wire boom")
            return super().place_ioc_limit(**k)

    signed = _SecondSliceBoom()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["multi_slice_fill"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["multi_slice_fill"]
    assert row["status"] == "error"
    assert "wire boom" in row["error_message"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1 and closes[0]["size"] == _D("0.001")


def test_trigger_modify_refusal_cancels_the_original_order(live_db):
    # A refused modify leaves the ORIGINAL order resting under create_cloid
    # (§17.4) — the cleanup must cancel THAT cloid, not the never-bound
    # modify_cloid, and say so in the failure detail.
    refused = _Ack(
        "error", exchange_order_id=None, filled_size=None, average_price=None, error="bad modify"
    )
    signed = _FakeSigned(modify_ack=refused)
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["stop_loss_modify"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["stop_loss_modify"]
    assert row["status"] == "failed"
    assert "SL modify was refused" in row["error_message"]
    assert "cancelled the original" in row["error_message"]
    assert signed.cancelled_cloids == [signed.trigger_calls[0]["cloid_hex"]]


def test_resting_ioc_probe_is_cancelled_and_fails_the_test(live_db):
    # The OrderAck contract admits "resting" even for an IOC. Booking it as
    # "canceled" would lie to the audit trail while the order sits live —
    # fail-safe: cancel it, book the TRUE state, fail the test.
    resting = _Ack("resting", exchange_order_id="oid-9", filled_size=None, average_price=None)
    signed = _FakeSigned(place_acks=[resting])
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'",
            ("live-BTC",),
        ).fetchall()
    row = latest["slice_order_submit"]
    assert row["status"] == "failed"
    assert "came back 'resting'" in row["error_message"]
    assert "cancel_by_cloid" in signed.calls
    assert len(probe_rows) == 1
    assert probe_rows[0]["status"] == "canceled"  # the cancel succeeded → true state


def test_resting_ioc_probe_with_refused_cancel_books_open(live_db):
    # If the fail-safe cancel is itself refused, the order is STILL resting —
    # an "open" row is the honest state (the §12.3 lane then tracks it), never
    # a false "canceled".
    resting = _Ack("resting", exchange_order_id="oid-9", filled_size=None, average_price=None)
    signed = _FakeSigned(place_acks=[resting], cancel=_Cancel(False, "not found"))
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'",
            ("live-BTC",),
        ).fetchall()
    assert len(probe_rows) == 1
    assert probe_rows[0]["status"] == "open"


def test_reduce_only_close_partial_fill_fails_and_closes_residual(live_db):
    # Q4 2026-07-28: accepted-but-partial must not record passed on partial
    # evidence — the verdict fails, the residual is best-effort closed, and
    # both facts land in the message.
    partial = _Ack("filled", filled_size=_D("0.0004"))
    signed = _FakeSigned(place_acks=[_Ack("filled"), partial])
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["reduce_only_close"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["reduce_only_close"]
    assert row["status"] == "failed"
    assert "filled 0.0004 of 0.001" in row["error_message"]
    assert "cleanup" in row["error_message"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert closes[-1]["size"] == _D("0.0006")  # the residual


def test_emergency_close_partial_fill_fails_and_closes_residual(live_db):
    partial = _Ack("filled", filled_size=_D("0.0004"))
    signed = _FakeSigned(place_acks=[_Ack("filled"), partial])
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["emergency_close"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["emergency_close"]
    assert row["status"] == "failed"
    assert "emergency close filled 0.0004 of 0.001" in row["error_message"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert closes[-1]["size"] == _D("0.0006")


def test_zero_filled_accepted_entry_aborts_the_close_tests(live_db):
    # An IOC the exchange accepts but fills 0 of must abort — the close tests
    # would otherwise "pass" with nothing opened to close.
    signed = _FakeSigned(place_ack=_Ack("filled", filled_size=_D(0)))
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["multi_slice_fill", "reduce_only_close", "emergency_close"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["multi_slice_fill"]["status"] == "failed"
    assert "neither slice filled" in latest["multi_slice_fill"]["error_message"]
    assert latest["reduce_only_close"]["status"] == "failed"
    assert "did not fill" in latest["reduce_only_close"]["error_message"]
    assert latest["emergency_close"]["status"] == "failed"
    assert "did not fill" in latest["emergency_close"]["error_message"]


def test_trigger_probes_book_no_orders_rows(live_db):
    # Pins D2 (2026-07-27): trigger create/modify/cancel probes write NO orders
    # rows — only IOC probes are booked. Since the staged long (2026-07-29) the
    # block's two IOC legs (staging open + reduce-only close) ARE booked — real
    # money that must reconcile — but no row ever carries a trigger role.
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=[
                "slice_plan_cancel",
                "stop_loss_create",
                "stop_loss_modify",
                "stop_loss_cancel",
                "take_profit_create",
                "take_profit_modify",
                "take_profit_cancel",
            ]
        )
        probe_rows = live_db.conn.execute(
            "SELECT * FROM orders WHERE run_id = ? AND status_reason = 'smoke_probe'",
            ("live-BTC",),
        ).fetchall()
    assert len(probe_rows) == 2
    assert {r["order_role"] for r in probe_rows} == {"entry", "close"}
    assert all(r["type"] == "ioc_limit" for r in probe_rows)


def test_status_test_fails_when_a_booked_cloid_reads_unknown(live_db):
    # A truthy payload is not proof: unknownOid for a cloid the exchange
    # booked must FAIL, not pass as "resolved" (exit-check 2026-07-28).
    signed = _FakeSigned(query_payload={"status": "unknownOid"})
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit", "slice_order_status"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_status"]
    assert row["status"] == "failed"
    assert "unknownOid for a cloid the exchange" in row["error_message"]


def test_status_test_negative_leg_passes_when_submit_was_never_booked(live_db):
    # Test 3's far IOC refused per-order (no oid): the CORRECT orderStatus
    # answer is unknownOid — the §19.2 confirmed-absent leg. A venue that
    # RESOLVES a never-booked cloid must fail instead.
    refused = _Ack(
        "error", exchange_order_id=None, filled_size=None, average_price=None, error="not filled"
    )
    with live_db:
        signed = _FakeSigned(place_acks=[refused], query_payload={"status": "unknownOid"})
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit", "slice_order_status"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
        assert latest["slice_order_status"]["status"] == "passed"
        assert "never-booked" in latest["slice_order_status"]["detail"]

        signed2 = _FakeSigned(place_acks=[refused])  # resolves despite no booking
        smoke.SmokeTestRunner(_ctx(live_db, signed2, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit", "slice_order_status"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_status"]
    assert row["status"] == "failed"
    assert "refused to book" in row["error_message"]


def test_preflight_arm_is_refreshed_before_every_test(live_db):
    # The pre-flight's scheduleCancel (120s) must not fire mid-suite: each
    # order-placing-selection test re-arms it before running (exit-check
    # 2026-07-28). Two tests → two refreshes (the fake recovery arms nothing).
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit", "slice_plan_cancel"]
        )
    assert signed.calls.count("schedule_cancel") == 2


def test_no_preflight_selection_never_touches_the_switch_between_tests(live_db):
    # A selection without order-placing tests (no pre-flight arm) must not
    # re-arm the wallet-wide switch — same scoping rule as the exit disarm.
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed)).run(only=["signed_client_init"])
    assert signed.calls.count("schedule_cancel") == 0


def test_only_status_without_submit_is_refused():
    # Q3 2026-07-28: a selection that can never pass is refused up front, so
    # the append-only audit never records the slip as "exchange refused".
    with pytest.raises(ValueError, match="select both"):
        smoke.validate_only_keys(["slice_order_status"])
    both = smoke.validate_only_keys(("slice_order_submit", "slice_order_status"))
    assert both == ("slice_order_submit", "slice_order_status")


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


# -- run-lease heartbeat (exit check 2026-07-27) ----------------------------


def test_heartbeat_fires_once_per_test(live_db):
    beats = {"n": 0}

    def _beat():
        beats["n"] += 1

    ctx = _ctx(live_db, _FakeSigned(), run_recovery=lambda: _Recovery())
    ctx.heartbeat = _beat
    with live_db:
        smoke.SmokeTestRunner(ctx).run()
    assert beats["n"] == len(smoke.SMOKE_TESTS)


def test_superseded_lease_aborts_and_suppresses_disarm(live_db):
    # RunLockError from the heartbeat = a successor legitimately owns the run
    # (and the wallet's kill switch): the suite must stop before the next wire
    # action and must NOT fire the account-wide clear (it would strip the
    # successor's dead-man cover). Completed verdicts stay durable.
    from contrib.hyperliquid_perp.paper.run_lock import RunLockError

    signed = _FakeSigned()
    beats = {"n": 0}

    def _beat():
        beats["n"] += 1
        if beats["n"] >= 3:
            raise RunLockError("superseded by pid 4242")

    ctx = _ctx(live_db, signed, run_recovery=lambda: _Recovery())
    ctx.heartbeat = _beat
    with live_db:
        with pytest.raises(RunLockError):
            smoke.SmokeTestRunner(ctx).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 2  # tests 1 and 2 recorded before the third beat
    assert "clear_scheduled_cancel" not in signed.calls


def test_transient_heartbeat_failure_aborts_but_still_disarms(live_db):
    # A non-RunLockError heartbeat failure (transient store error) also stops
    # the suite (orders must not continue under an uncertain lease), but no
    # successor exists — the normal exit disarm still runs.
    signed = _FakeSigned()

    def _beat():
        raise sqlite3.OperationalError("store busy")

    ctx = _ctx(live_db, signed, run_recovery=lambda: _Recovery())
    ctx.heartbeat = _beat
    with live_db, pytest.raises(sqlite3.OperationalError):
        smoke.SmokeTestRunner(ctx).run()
    assert "clear_scheduled_cancel" in signed.calls


# -- round-4 review-loop sync (2026-07-29): staged long, stop-on-error, regime --


def test_missing_agent_address_fails_client_init(live_db):
    # Test 1's own assertion: a signed client with no bound agent address after
    # init is an exchange-side refusal shape → "failed", by name.
    class _NoAgent(_FakeSigned):
        agent_address = ""

    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _NoAgent())).run(only=["signed_client_init"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["signed_client_init"]
    assert row["status"] == "failed"
    assert "no agent_address" in row["error_message"]


def test_update_leverage_writes_the_configured_regime(live_db):
    # Test 2 writes the RUN'S OWN regime (decision 2026-07-29): a probe context
    # (3x isolated ≠ the 1x-cross defaults) must land verbatim in the wire call
    # AND the durable detail — a hardcoded spec literal would fail both legs.
    signed = _FakeSigned()
    ctx = _ctx(live_db, signed)
    ctx.leverage = 3
    ctx.is_cross = False
    with live_db:
        smoke.SmokeTestRunner(ctx).run(only=["update_leverage"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert signed.leverage_calls == [{"coin": "BTC", "leverage": 3, "is_cross": False}]
    row = latest["update_leverage"]
    assert row["status"] == "passed"
    assert "leverage set to 3x isolated" in row["detail"]


def test_submit_unexpected_fill_is_flattened_and_noted(live_db):
    # Test 3's far price is an assumption, not a guarantee: a filled far IOC is
    # a real funded long the suite must not strand — flattened best-effort, the
    # anomaly carried in the durable detail, verdict stays with the wire
    # round-trip (decision 2026-07-29).
    signed = _FakeSigned()  # the default ack fills 0.001
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_submit"]
    assert row["status"] == "passed"
    assert "unexpectedly FILLED 0.001" in row["detail"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1
    assert closes[0]["is_buy"] is False and closes[0]["size"] == _D("0.001")


def test_submit_unfilled_far_ioc_places_no_cleanup(live_db):
    # The mirror: a far IOC that (correctly) fills nothing must neither close
    # anything nor carry the FILLED note — the cleanup lane is fill-gated.
    signed = _FakeSigned(place_ack=_Ack("filled", filled_size=_D(0)))
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_submit"]
    assert row["status"] == "passed"
    assert "unexpectedly FILLED" not in (row["detail"] or "")
    assert not [c for c in signed.place_calls if c.get("reduce_only")]


def test_refused_cancel_says_probe_still_resting(live_db):
    # Tests 5/10/13: the cancel IS the tested action — a refusal fails the test
    # AND must say the probe trigger is still resting on the exchange (the one
    # shared wording), never retry silently.
    signed = _FakeSigned(cancel=_Cancel(False, "cancel rejected"))
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_plan_cancel", "stop_loss_cancel", "take_profit_cancel"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    for key in ("slice_plan_cancel", "stop_loss_cancel", "take_profit_cancel"):
        assert latest[key]["status"] == "failed"
        assert "still resting on the exchange" in latest[key]["error_message"]


def test_kill_switch_refresh_failure_propagates_and_still_disarms(live_db):
    # The per-test re-arm is fail-closed: a refresh failure must abort the
    # suite RAW (no probe goes out under an uncertain switch — not one test's
    # error verdict), while the finally still runs the exit disarm.
    signed = _FakeSigned(raise_on={"schedule_cancel"})
    with live_db:
        with pytest.raises(RuntimeError, match="boom in schedule_cancel"):
            smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
                only=["slice_order_submit"]
            )
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 0  # the refresh sits before the test body — no verdict
    assert "clear_scheduled_cancel" in signed.calls


def test_trigger_modify_exception_sweeps_both_cloids(live_db):
    # An unknown modify outcome (raise) must sweep BOTH cloids best-effort —
    # exactly one rests on the exchange, the other cancel refuses harmlessly —
    # and land as an "error" verdict (harness lane), not a "failed".
    signed = _FakeSigned(raise_on={"modify_trigger_order"})
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["stop_loss_modify"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["stop_loss_modify"]
    assert row["status"] == "error"
    assert "boom in modify_trigger_order" in row["error_message"]
    create_cloid = signed.trigger_calls[0]["cloid_hex"]
    assert len(signed.cancelled_cloids) == 2
    assert create_cloid in signed.cancelled_cloids
    # The other swept cloid is the modify cloid (registered before the raise).
    assert any(c != create_cloid for c in signed.cancelled_cloids)


def test_error_verdict_stops_the_suite_and_flattens_the_staged_long(live_db):
    # Stop-on-error (decision 2026-07-29): after a harness error the account
    # state is unknown — the remaining selected tests must NOT execute and get
    # NO rows (a written row would supersede a prior pass), while the finally
    # backstop still flattens the staged long and the exit disarm still runs.
    signed = _FakeSigned(raise_on={"modify_trigger_order"})
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        executed = runner.run(
            only=[
                "stop_loss_create",
                "stop_loss_modify",
                "take_profit_create",
                "kill_switch_arm_refresh",
            ]
        )
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert [t.key for t in executed] == ["stop_loss_create", "stop_loss_modify"]
    assert {r["test_key"] for r in rows} == {"stop_loss_create", "stop_loss_modify"}
    # take_profit_create was still selected when the suite stopped, so the
    # in-loop close was skipped — the finally backstop flattened the long.
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1 and closes[0]["size"] == _D("0.001")
    assert "clear_scheduled_cancel" in signed.calls


def test_failed_submit_cascades_test_4_to_error_and_stops(live_db):
    # Test 3 aborts before leaving its handle (resting-IOC anomaly): test 4 is
    # a dependency cascade — "error" (fix test 3 first, suite stops), never a
    # "failed" that files the slip in the exchange-refused triage bucket.
    resting = _Ack("resting", exchange_order_id="oid-9", filled_size=None, average_price=None)
    signed = _FakeSigned(place_acks=[resting])
    with live_db:
        executed = smoke.SmokeTestRunner(
            _ctx(live_db, signed, run_recovery=lambda: _Recovery())
        ).run(only=["slice_order_submit", "slice_order_status", "slice_plan_cancel"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["slice_order_submit"]["status"] == "failed"
    row = latest["slice_order_status"]
    assert row["status"] == "error"
    assert "did not complete in this process" in row["error_message"]
    assert [t.key for t in executed] == ["slice_order_submit", "slice_order_status"]
    assert "slice_plan_cancel" not in latest  # stopped before test 5 — no row


def test_staged_long_opens_once_and_closes_before_the_restart_tests(live_db):
    # One staged long under the whole trigger block (decision 2026-07-29):
    # opened lazily by the FIRST trigger test, reduce-only closed after the
    # LAST — before a restart test's clean-restart recovery could find a
    # position nobody staged.
    timeline: list[str] = []

    class _Timeline(_FakeSigned):
        def place_ioc_limit(self, **k):
            timeline.append("close" if k.get("reduce_only") else "open")
            return super().place_ioc_limit(**k)

    def _recovery():
        timeline.append("recovery")
        return _Recovery()

    signed = _Timeline()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=_recovery)).run(
            only=[
                "slice_plan_cancel",
                "stop_loss_create",
                "stop_loss_modify",
                "stop_loss_cancel",
                "take_profit_create",
                "take_profit_modify",
                "take_profit_cancel",
                "restart_reconciliation",
            ]
        )
    # Pre-flight recovery, ONE staging open, its close, THEN the restart test.
    assert timeline == ["recovery", "open", "close", "recovery"]
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1
    assert closes[0]["is_buy"] is False and closes[0]["size"] == _D("0.001")


def test_recovery_seam_without_passed_attr_is_an_error_verdict(live_db):
    # RecoveryResult contract: a seam wired to the wrong object must surface as
    # a loud AttributeError → "error" (a harness bug), never be absorbed by a
    # getattr default into a "failed" that reads as an exchange refusal.
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned(), run_recovery=lambda: object())).run(
            only=["restart_reconciliation"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["restart_reconciliation"]
    assert row["status"] == "error"
    assert "AttributeError" in row["error_message"]


def test_preflight_recovery_wrong_object_raises_loudly(live_db):
    # The pre-flight reads .passed OUTSIDE its exception net on purpose: a
    # wrong seam object is a harness bug that must crash raw — not be dressed
    # as "recovery did not pass" — while the finally still disarms.
    signed = _FakeSigned()
    with live_db:
        with pytest.raises(AttributeError):
            smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: object())).run()
        rows = repo.iter_smoke_test_results(live_db.conn, "live-BTC")
    assert len(rows) == 0
    assert "clear_scheduled_cancel" in signed.calls
