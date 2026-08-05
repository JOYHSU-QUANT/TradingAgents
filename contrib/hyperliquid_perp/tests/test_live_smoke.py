"""§20.2 smoke-test runner harness — driven by a fake signed client (offline)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.live import smoke
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState
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


def test_a_net_short_account_is_named_not_failed_seven_times(live_db):
    """Every probe the suite opens is long-shaped and closes reduce-only SELL.

    On a net-short account the opening BUY only SHRINKS the short and each
    reduce-only SELL would grow it, so the venue refuses: tests 5, 7, 8–13 and 18
    all go red as "the exchange refused" — which the RUNBOOK triage table reads
    as a config/market problem — and the cleanup then reports a staged-long
    residual that does not exist, telling the operator to close it by hand (a
    post-genesis manual fill books fill_unmapped and pins validate at exit 5).

    Reachable without any mistake: §3.2 asks for a small position before
    ``--create`` without saying which DIRECTION, and §4 suggests re-running the
    suite after a code change on a run that may be holding a short
    (2026-08-01 lifecycle review).
    """
    with live_db:
        with live_db.transaction() as conn:
            repo.upsert_current_position(
                conn,
                "live-BTC",
                PositionState(coin="BTC", size=_D("-0.05"), entry_price=_D(60000)),
                updated_at=_T0,
            )
        signed = _FakeSigned()
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run()
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")

    long_shaped = [k for k, row in latest.items() if row["status"] == "failed"]
    assert long_shaped, "the long-shaped probes must fail on a net-short account"
    # ...but every one of them says WHY, instead of reading as an exchange refusal.
    for key in long_shaped:
        assert "net SHORT" in (latest[key]["error_message"] or "")
    # And the guard fired BEFORE anything reached the wire, so there is no staged
    # position to warn about. Asserted on the wire, not on the residual flag: the
    # fake accepts every close, so `staged_long_residual is None` would hold with
    # or without the guard (2026-08-01 exit check).
    assert runner.staged_long_residual is None
    # NARROW: only the long-shaped probes are affected. The tests that do not open
    # a position still pass, so a net-short account does not fail the whole suite
    # — which is what makes the named abort more useful than seven red rows.
    assert latest["signed_client_init"]["status"] == "passed"
    assert latest["update_leverage"]["status"] == "passed"


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


def _ks_rows(db, run_id="live-BTC"):
    with db.transaction() as conn:
        return [(r["event_type"], r["detail"]) for r in repo.iter_kill_switch_events(conn, run_id)]


def test_the_preflight_refresh_records_the_cover_it_installed(live_db):
    """The suite's HIGHEST-VOLUME writer, which had no test at all.

    ``_refresh_kill_switch`` runs before EVERY selected test once the selection
    needed the pre-flight, not only before the order-placing ones (11 of 18) —
    18 of the ~22 kill-switch rows a full suite writes, and the ONLY writer of the
    ``deadline=max(config,120)s`` token the validator parses. Both round-14 smoke
    tests drove ``only=[...]`` selections that are not in ``_ORDER_PLACING_TESTS``,
    so ``needs_preflight`` was False and this path never ran: restoring its
    pre-round-14 body (wire call, no row) left the whole suite green
    (2026-08-01 round-15 mutation probe).
    """
    from contrib.hyperliquid_perp.live.kill_switch import is_suite_authored
    from contrib.hyperliquid_perp.live.validation import _stated_deadline_seconds

    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        rows = _ks_rows(live_db)
    refreshes = [detail for event, detail in rows if event == "kill_switch_refreshed"]
    assert refreshes, f"the pre-flight refresh wrote no row; got {rows}"
    # It states the cover it installed — max(config, 120s) — because on a run
    # configured below 120 this row is the ONLY evidence of the longer cover.
    assert _stated_deadline_seconds(refreshes[0]) == _D(120)
    # ...and it is marked as the SUITE's, so it cannot buy §20.3 sample credit.
    assert is_suite_authored(refreshes[0])


def test_a_failed_preflight_refresh_is_recorded_before_it_propagates(live_db):
    # Recording only the success path left an aborted suite reading as
    # "...refreshed, disarmed" over a sub-deadline gap: zero outage seconds for a
    # wallet whose cover had actually lapsed. The manager writes
    # kill_switch_refresh_failed for exactly this, and the measure opens an
    # episode on it. Still fail-closed: probes must not go out under an uncertain
    # switch, so the raise still propagates.
    signed = _FakeSigned(raise_on={"schedule_cancel"})
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        with pytest.raises(RuntimeError, match="boom in schedule_cancel"):
            runner.run(only=["slice_order_submit"])
        rows = _ks_rows(live_db)
    # Recorded BEFORE it propagated, so the lapse survives the abort.
    failures = [detail for event, detail in rows if event == "kill_switch_refresh_failed"]
    assert failures
    # ...and MARKED. The first version passed only ``error=``, which lands in
    # error_message while the tally reads detail — so the exclusion branch written
    # for suite failures could never fire, and the RUNBOOK's claim that it did was
    # false (2026-08-01 round-16 review).
    from contrib.hyperliquid_perp.live.kill_switch import is_suite_authored

    assert is_suite_authored(failures[0])


def test_the_exit_disarm_leaves_the_row_the_acceptance_measure_reads(live_db):
    """The suite's own wire actions have to appear in the §18.5 log.

    The suite drives ``scheduleCancel`` through the signed client rather than
    through a KillSwitchManager, so nothing else writes these rows — and
    ``validation._kill_switch_tally`` charges any silence longer than the run's
    deadline as an outage on the SAME run_id the acceptance verdict is computed
    over. With the suite silent, its own duration and the operator-paced gap
    before ``live --loop`` were billed as exposure: a flawless 120h run failed at
    exit 5 once that gap passed ~73 minutes, while RUNBOOK §3 promises smoke
    passes never expire (2026-08-01 round-14 review).

    ``kill_switch_disarmed`` specifically: it is the ONE event the validator's
    clean-ending exemption is keyed on, and it must be LAST, because that is what
    makes the silence after the suite a stopped process rather than a bare wallet.
    """
    signed = _FakeSigned()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=["restart_reconciliation"])
        rows = _ks_rows(live_db)
    assert runner.kill_switch_disarm_failed is False
    assert rows[-1][0] == "kill_switch_disarmed"


def test_a_failed_disarm_is_recorded_durably_not_only_in_a_flag(live_db):
    # The in-memory flag dies with the process; the wallet may still be armed. The
    # row is the only trace an operator (or the validator) can read afterwards —
    # and it must still not raise, because the verdicts are already durable.
    signed = _FakeSigned(raise_on={"clear_scheduled_cancel"})
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=["restart_reconciliation"])
        events = [event for event, _ in _ks_rows(live_db)]
    assert runner.kill_switch_disarm_failed is True
    assert events[-1] == "kill_switch_disarm_failed"
    # NOT the clean ending: a wallet that may still be armed must not buy the
    # exemption that says "there is nothing left to fire".
    assert "kill_switch_disarmed" not in events


def test_test_14_records_every_transition_it_makes(live_db):
    """Three real wallet-state changes; three rows, each after its own wire call.

    Test 14 arms, refreshes and clears the account-wide trigger. Recording none of
    them left the run's log claiming a cover that had been released and a silence
    that had been covered. The armed row also has to state its deadline in the
    form the validator parses, since that number sizes the stretch that follows.
    """
    from contrib.hyperliquid_perp.live.kill_switch import is_suite_authored
    from contrib.hyperliquid_perp.live.validation import _stated_deadline_seconds

    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed)).run(only=["kill_switch_arm_refresh"])
        rows = _ks_rows(live_db)
    # Four, not three: test 14 flags the wallet as possibly-armed before its first
    # arm, so the runner's exit disarm fires too and issues a SECOND real
    # clear_scheduled_cancel. That double-clear predates these rows — recording it
    # only makes it visible — and it is harmless to the measure, because a stretch
    # opening on a clean ending is exempt on both sides.
    assert [event for event, _ in rows] == [
        "kill_switch_armed",
        "kill_switch_refreshed",
        "kill_switch_disarmed",
        "kill_switch_disarmed",
    ]
    # The writer/reader contract, checked end to end rather than by eye — on the
    # armed row AND the refreshed one. Only rows[0] was inspected before, so the
    # refresh row (which is what buys §20.3 sample credit) could lose its marker
    # and its deadline token with the suite green (2026-08-01 round-16 probe).
    assert _stated_deadline_seconds(rows[0][1]) == _D(120)
    assert _stated_deadline_seconds(rows[1][1]) == _D(120)
    assert all(is_suite_authored(detail) for _, detail in rows), rows
    # SEPARATED from what the row already said. `is_suite_authored` uses a
    # substring test and the deadline regex parses the front, so both tolerate
    # `...refresh)writer=live-smoke` — which defeats the hand-reading this
    # column exists for (2026-08-01 round-17 probe).
    from contrib.hyperliquid_perp.live.kill_switch import _SUITE_AUTHORED_TOKEN

    assert all(detail.endswith(f" {_SUITE_AUTHORED_TOKEN}") for _, detail in rows), rows


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


def test_trigger_probes_are_sized_to_the_staged_position(live_db):
    """A reduce-only probe must carry the STAGED size, not a re-derived one.

    Live sizes an SL to floor_to_step(abs(position.size)), and staging a real long
    exists so each probe is that exact §17 shape. Re-deriving from _probe_size()
    (ceil(11 USDC / mark)) made the probe an unrelated quantity — here 0.00019
    against a staged 0.001 — and in the oversized direction the venue refuses the
    reduce-only order outright, the failure staging was introduced to prevent.
    """
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
    staged = _Ack().filled_size  # what the staging entry actually filled
    assert signed.trigger_calls and all(c["size"] == staged for c in signed.trigger_calls)
    assert signed.modify_calls and all(c["size"] == staged for c in signed.modify_calls)
    # The control: the old re-derived size at this mark is a DIFFERENT number, so
    # this assertion cannot pass by coincidence.
    assert staged != _D("0.00019")


def test_a_moving_mark_cannot_resize_the_modified_trigger(live_db):
    """§17.4 is modify IN PLACE — the modify must not change the order's size.

    _trigger_modify used to call _probe_size() separately for the place and the
    modify, each re-reading the mark, so a mark move between them silently
    resized the order and the test proved something other than an in-place update.
    """
    marks = iter([_D(60000)] * 3 + [_D(30000)] * 40)  # the mark halves mid-test
    signed = _FakeSigned()
    ctx = _ctx(live_db, signed, run_recovery=lambda: _Recovery())
    with live_db:
        smoke.SmokeTestRunner(replace(ctx, mark_price=lambda: next(marks))).run(
            only=["stop_loss_modify"]
        )
    assert signed.trigger_calls and signed.modify_calls
    assert signed.trigger_calls[0]["size"] == signed.modify_calls[0]["size"]


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


def test_a_probe_whose_cancel_is_refused_is_swept_and_reported(live_db):
    # Trigger probes book no orders row, so before this the runner held no
    # handle on them at all — each was cancelled inline and nothing survived the
    # call frame. A refused cancel left the probe resting while the verdict
    # stayed PASSED, the gate opened, and the only trace was the detail column
    # of a green row. Not cosmetic: §19.3's sweep validates rather than cancels
    # protective roles, so the next `live` start files it as an
    # orphan_exchange_order and pins this run-id's validate at exit 5.
    signed = _FakeSigned(cancel=_Cancel(False, error="already filled"))
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    with live_db:
        runner.run(only=["stop_loss_create"])
    assert runner.probe_residual is not None
    assert "orphan_exchange_order" in runner.probe_residual
    # The exit sweep RETRIED before giving up: one cancel in-test, one on exit.
    assert len(signed.cancelled_cloids) >= 2


def test_a_probe_the_exchange_refused_is_not_reported_as_a_residual(live_db):
    # The other direction, and the one a naive handle-tracking design gets
    # wrong: an exchange that ANSWERS "rejected" is positive evidence that no
    # order exists under that cloid. Keeping the handle would have the sweep
    # chase a phantom and warn about a probe that was never placed.
    refused = _Ack(
        "error", exchange_order_id=None, filled_size=None, average_price=None, error="nope"
    )
    # The cancel must ALSO fail here, or the exit sweep quietly cancels the
    # retained phantom handle and the assertion below passes even when the
    # retirement is missing — the sweep's success would mask it (2026-07-31).
    signed = _FakeSigned(trigger_ack=refused, cancel=_Cancel(False, error="already filled"))
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    with live_db:
        runner.run(only=["stop_loss_create"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert latest["stop_loss_create"]["status"] == "failed"
    assert runner.probe_residual is None


def test_a_clean_suite_leaves_no_residual_of_either_kind(live_db):
    # Negative control: the happy path must not warn, or the warning stops
    # meaning anything. Paired with the refused-cancel test above, which is what
    # gives this one its meaning — alone it only asserts a default.
    signed = _FakeSigned()
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    with live_db:
        runner.run(only=["stop_loss_create"])
    assert runner.probe_residual is None
    assert runner.position_residuals == []
    # And the tracking really was exercised: the probe was registered and then
    # retired by a confirmed cancel, rather than never tracked at all.
    assert signed.cancelled_cloids


def test_one_probes_residual_is_not_erased_by_another_probes_clean_close(live_db):
    # position_residuals is runner-level and each caller cleans up a DIFFERENT
    # probe, so clearing it on a successful close dropped an earlier, still-open
    # position — the CLI then printed nothing and the operator never learned a
    # funded position was live. Append-only is the correct shape.
    signed = _FakeSigned()
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    runner.position_residuals.append("0.0005 left after cleanup — earlier probe")
    runner._best_effort_close(_D("0.001"))  # a LATER probe closing cleanly
    assert runner.position_residuals == ["0.0005 left after cleanup — earlier probe"]


def test_a_lost_ioc_ack_is_recovered_by_cloid_and_booked(live_db):
    # The suite bypasses LiveOrderSubmitter, so no intent row is written before
    # the wire call. A transport failure whose order actually landed therefore
    # left NO local trace: the backfill maps by exchange_order_id, finds no
    # orders row, books fill_unmapped, and pins this run's validate at exit 5.
    # The cloid is in hand and orderStatus is an ungated read — ask.
    signed = _FakeSigned(raise_on={"place_ioc_limit"})
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    with live_db:
        runner.run(only=["multi_slice_fill"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
        booked = live_db.conn.execute(
            "SELECT cloid_hex, exchange_order_id FROM orders WHERE run_id = ?", ("live-BTC",)
        ).fetchall()
    row = latest["multi_slice_fill"]
    assert row["status"] == "error"
    assert "DID reach the exchange" in row["error_message"]
    assert signed.queried_cloid is not None  # it asked rather than assuming
    assert any(b["exchange_order_id"] == "1" for b in booked)  # and booked what it learned


def test_a_lost_ioc_ack_the_exchange_never_saw_books_nothing(live_db):
    # unknownOid is the documented "we never saw it" marker: nothing landed, so
    # there is nothing to book and the original transport error stands unchanged.
    signed = _FakeSigned(raise_on={"place_ioc_limit"}, query_payload={"status": "unknownOid"})
    runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
    with live_db:
        runner.run(only=["multi_slice_fill"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
        booked = live_db.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE run_id = ?", ("live-BTC",)
        ).fetchone()[0]
    assert "DID reach the exchange" not in (latest["multi_slice_fill"]["error_message"] or "")
    assert booked == 0


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
    # The between-test refresh keys on ``needs_preflight`` (an order-placing
    # selection) — NOT on the wider arm-evidence flag the exit disarm keys on:
    # an ``--only kill_switch_arm_refresh`` run arms, gets the exit disarm, and
    # still must not be re-armed between tests. This selection arms nothing at
    # all, so no schedule_cancel may go out.
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


def test_both_trigger_probes_put_the_limit_on_the_marketable_side(live_db):
    # Every probe is a SELL, and live derives a sell's limit BELOW its trigger
    # (protection._protected_limit: trigger * (1 - slip)) so it is marketable
    # when it fires. The SL pair got that for free — further from the mark is
    # also lower — but the TP pair was mirrored instead, putting the limit
    # ABOVE the trigger: the one shape live never emits, on a §20.2 gate item
    # whose whole purpose is to exercise the real §17 wire shape.
    runner = smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned()))
    for tpsl in ("sl", "tp"):
        trigger, limit = runner._trigger_prices(tpsl)
        assert limit < trigger, f"{tpsl}: a SELL's limit must sit below its trigger"
    # And each pair still sits far enough from the mark never to fire: the SL
    # well below it, the TP well above.
    sl_trigger, _ = runner._trigger_prices("sl")
    tp_trigger, tp_limit = runner._trigger_prices("tp")
    assert sl_trigger < _D(60000) < tp_limit < tp_trigger


def test_a_context_must_pair_dry_run_with_the_absence_of_a_signed_client(live_db):
    # _execute gates the test bodies on dry_run alone while _disarm_kill_switch
    # separately defends with `signed is None` — the pairing was assumed
    # everywhere and enforced nowhere, so a real run with no signed client
    # walked into a test body and died on an opaque NoneType attribute error
    # that the broad except relabelled as a harness `error`.
    with pytest.raises(ValueError, match="dry run"):
        _ctx(live_db, None, dry_run=False)
    with pytest.raises(ValueError, match="dry run"):
        _ctx(live_db, _FakeSigned(), dry_run=True)
    # Control: both valid pairings construct.
    assert _ctx(live_db, None, dry_run=True) is not None
    assert _ctx(live_db, _FakeSigned(), dry_run=False) is not None


def test_an_accepted_ack_without_an_oid_never_reaches_the_modify_wire(live_db):
    # The OrderAck contract gives every resting/filled ack an exchange_order_id,
    # so this is unreachable in production — but the modify's `target` was fed
    # straight from it, so a None slipping through would put a malformed modify
    # on the wire against a REAL resting order instead of failing here. (Typing
    # SmokeContext.signed concretely is what surfaced this; under `Any` the
    # str|None never had to line up with modify_trigger_order's `str`.)
    runner = smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned()))
    # AssertionError, not _SmokeAbort: a contract violation is a harness bug, so
    # it must record `error` and stop the suite, not `failed` (= "the exchange
    # refused"), which lets the suite keep placing wire actions.
    with pytest.raises(AssertionError, match="without an exchange order id"):
        runner._require_oid(_Ack(status="resting", exchange_order_id=None), "probe")
    # Controls: a refused ack still reports the refusal as an abort (failed),
    # and a well-formed one returns its id.
    with pytest.raises(smoke._SmokeAbort, match="refused"):
        runner._require_oid(_Ack(status="error", exchange_order_id=None, error="nope"), "probe")
    assert runner._require_oid(_Ack(exchange_order_id="oid-9"), "probe") == "oid-9"


def test_an_unresolvable_test_key_is_contained_as_an_error_verdict(live_db):
    # The dispatch is reflective, so it is the one mapping with no compile-time
    # link to the registry. Resolved outside the try it raised AttributeError
    # before any containment, escaping run()'s loop past _record() — no
    # live_smoke_tests row for the attempted test, breaking the append-only
    # audit promise, mid-suite. (The import-time guard makes this unreachable
    # in practice; this pins the containment behind it.)
    runner = smoke.SmokeTestRunner(_ctx(live_db, _FakeSigned()))
    result = runner._execute(smoke.SmokeTest(99, "no_such_probe", "fabricated"))
    assert result.status == "error"
    assert "AttributeError" in (result.error_message or "")


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
    # One per test, PLUS one final re-check in the exit path: the cleanup there
    # places a real reduce-only IOC and can clear the account-wide switch, so it
    # positively re-verifies ownership rather than trusting that some earlier
    # heartbeat happened to raise (2026-07-30 concurrency review).
    assert beats["n"] == len(smoke.SMOKE_TESTS) + 1


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


# -- ownership re-check before the exit cleanup (2026-07-30 concurrency review) --
#
# The exit path's two actions are both externally visible on a SHARED wallet — a
# real reduce-only IOC and an account-wide scheduleCancel clear — so both are
# gated on still owning the run. The pair below fixes the lease's state at the
# exit re-check itself (not at some earlier in-loop beat), which is the case
# `_lease_superseded` alone could never see: it is only ever set when a heartbeat
# happens to RAISE, so a suite that stops for any other reason reaches the
# cleanup with the flag still False even though the lease may be long gone.
# `raise_on={"modify_trigger_order"}` stops the suite on an error verdict with a
# later trigger test still selected, which is what leaves the staged long open
# across the finally (same staging as
# test_error_verdict_stops_the_suite_and_flattens_the_staged_long, whose healthy
# heartbeat is the "cleanup happens" baseline these two vary from).
_TAKEOVER_SELECTION = [
    "stop_loss_create",
    "stop_loss_modify",
    "take_profit_create",
    "kill_switch_arm_refresh",
]


def test_a_takeover_at_the_exit_recheck_hands_the_staged_long_over_unclosed(live_db):
    # C1: the successor took the run over WITHOUT this process ever noticing —
    # its §19.1 recovery has already adopted this wallet's position and armed its
    # own dead-man cover. Closing "our" staged long here would flatten ITS
    # position, and clearing the switch would strip ITS cover. So: no close, no
    # disarm, and the real exposure is handed over loudly on the operator surface
    # rather than dropped silently.
    from contrib.hyperliquid_perp.paper.run_lock import RunLockError

    signed = _FakeSigned(raise_on={"modify_trigger_order"})
    beats = {"n": 0}

    def _beat():
        beats["n"] += 1
        # Beats 1-2 are the two tests that run; beat 3 is the exit re-check.
        if beats["n"] >= 3:
            raise RunLockError("superseded by pid 4242")

    ctx = _ctx(live_db, signed, run_recovery=lambda: _Recovery())
    ctx.heartbeat = _beat
    with live_db:
        runner = smoke.SmokeTestRunner(ctx)
        runner.run(only=_TAKEOVER_SELECTION)
    assert beats["n"] == 3  # the supersession really landed on the exit re-check
    # NO reduce-only close reached the wire: every place_ioc_limit on record is
    # the staging entry, none is a close leg.
    assert [c for c in signed.place_calls if c.get("reduce_only")] == []
    assert "clear_scheduled_cancel" not in signed.calls
    # ...and the position is not silently abandoned: the operator is told the
    # size, that it was NOT closed, and who owns it now.
    residual = runner.staged_long_residual
    assert residual is not None
    assert "was NOT closed" in residual and "taken over" in residual
    assert str(_D("0.001")) in residual


def test_a_transient_failure_at_the_exit_recheck_still_cleans_up(live_db):
    # The paired control, and the reason the re-check is not simply "any error
    # means stop": a transient store failure is NOT evidence of supersession. The
    # lease is most likely still ours and the cleanup still matters — a real
    # funded long and an armed account-wide scheduleCancel are the two things
    # that must never be left behind on a hunch. Fail-open on ambiguity is
    # deliberate; only a real RunLockError gives the run away.
    signed = _FakeSigned(raise_on={"modify_trigger_order"})
    beats = {"n": 0}

    def _beat():
        beats["n"] += 1
        if beats["n"] >= 3:
            raise sqlite3.OperationalError("store busy")

    ctx = _ctx(live_db, signed, run_recovery=lambda: _Recovery())
    ctx.heartbeat = _beat
    with live_db:
        runner = smoke.SmokeTestRunner(ctx)
        runner.run(only=_TAKEOVER_SELECTION)  # the re-check absorbs it: no raise
    assert beats["n"] == 3
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 1 and closes[0]["size"] == _D("0.001")
    assert "clear_scheduled_cancel" in signed.calls
    assert runner.staged_long_residual is None  # closed cleanly, nothing to hand over


def test_a_preflight_that_dies_before_arming_never_clears_the_switch(live_db):
    # M5: the exit disarm used to be PREDICTED from the selection ("this run
    # would have armed"), which over-claims. A pre-flight that dies on the way in
    # — no run_recovery seam wired, or any raise before the arming call — armed
    # nothing, yet still fired an account-wide scheduleCancel clear that can wipe
    # a concurrent `live --loop`'s own dead-man cover on the same wallet. The flag
    # is now EVIDENCE, set at the arming paths, so this run disarms nothing.
    signed = _FakeSigned()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=None))
        with pytest.raises(smoke.SmokePreflightError, match="no run_recovery seam"):
            runner.run(only=["multi_slice_fill"])  # order-placing → pre-flight required
    assert "clear_scheduled_cancel" not in signed.calls
    # Nothing at all reached the wire, which is the point: there was no arm to
    # clear. (The counterpart is test_unclean_preflight_aborts_and_still_disarms:
    # a pre-flight that DID arm and then failed its verdict still disarms.)
    assert signed.calls == []


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


# -- staged-long cleanup: convergence + the operator-facing residual flag ----

# The trigger block's staged long is closed BETWEEN tests, so its cleanup has
# no step row to land in: the runner carries the outcome on the public
# ``staged_long_residual`` (None = flat) and the CLI prints it. These four
# fakes drive the three shapes that attribute exists for.
_REFUSED_CLOSE = _Ack(
    "error", exchange_order_id=None, filled_size=None, average_price=None, error="no liquidity"
)

# The trigger block's own tests, in canonical order — enough of it that the
# in-loop close fires after the LAST one (nothing later needs the position).
_TRIGGER_BLOCK = ["stop_loss_create", "stop_loss_modify", "stop_loss_cancel"]


class _PartialClose(_FakeSigned):
    """Fills reduce-only closes against a REAL position, the first one by half.

    Modelling the position is the point: a retry that re-sent the ORIGINAL
    (oversized) size shows up verbatim in :attr:`close_requests`, and the
    venue could only fill what is actually left — so the residual would never
    converge.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.position = _D(0)
        self.close_requests: list[Decimal] = []
        self._halve_next_close = True

    def place_ioc_limit(self, **k):
        ack = super().place_ioc_limit(**k)
        if not k.get("reduce_only"):
            self.position += ack.filled_size or _D(0)
            return ack
        self.close_requests.append(k["size"])
        fillable = min(k["size"], self.position)
        filled = fillable / 2 if self._halve_next_close else fillable
        self._halve_next_close = False
        self.position -= filled
        return _Ack("filled", filled_size=filled)


class _RefusesCloses(_FakeSigned):
    """Every reduce-only close is refused at the per-order level (no raise)."""

    def place_ioc_limit(self, **k):
        ack = super().place_ioc_limit(**k)
        return _REFUSED_CLOSE if k.get("reduce_only") else ack


class _RefusesFirstClose(_RefusesCloses):
    """Refuses only the in-loop close; the finally backstop's retry fills."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._refuse_next_close = True

    def place_ioc_limit(self, **k):
        ack = _FakeSigned.place_ioc_limit(self, **k)
        if not k.get("reduce_only") or not self._refuse_next_close:
            return ack
        self._refuse_next_close = False
        return _REFUSED_CLOSE


def test_partially_closed_staged_long_retries_the_true_residual(live_db):
    # A partial close leaves a SMALLER position: the retry must target that
    # residual, never re-send the original size (an oversized reduce-only some
    # venues refuse outright — and which never converges on the remainder).
    signed = _PartialClose()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=_TRIGGER_BLOCK)
    # 0.001 staged; the in-loop close fills half, the finally backstop asks for
    # exactly what is left. The second entry is the whole guard.
    assert signed.close_requests == [_D("0.001"), _D("0.0005")]
    assert signed.position == _D(0)  # genuinely flat, not "flat because we asked twice"
    assert runner.staged_long_residual is None


class _AlwaysPartialClose(_FakeSigned):
    """Every reduce-only close fills HALF, including the last one.

    :class:`_PartialClose` halves only the FIRST close, so the finally backstop
    flattens and the partial branch's effect is erased before anything is
    asserted about it.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.position = _D(0)
        self.close_requests: list[Decimal] = []

    def place_ioc_limit(self, **k):
        ack = super().place_ioc_limit(**k)
        if not k.get("reduce_only"):
            self.position += ack.filled_size or _D(0)
            return ack
        self.close_requests.append(k["size"])
        filled = min(k["size"], self.position) / 2
        self.position -= filled
        return _Ack("filled", filled_size=filled)


def test_a_cleanup_that_only_half_filled_is_not_reported_as_flat(live_db):
    """A partial cleanup close must not read as a flattened position.

    The existing partial test asserts the residual flag only AFTER a successful
    backstop close, so the partial branch could be deleted outright with the suite
    still green (mutation-verified, 2026-08-01 round-13 review). With that branch
    gone ``_attempt_close`` returns no note, ``_close_staged_long`` logs
    "flattened", the residual flag is cleared over a still-open position and the
    CLI prints no warning at all — a real funded position surviving a suite the
    RUNBOOK advertises as self-cleaning.
    """
    signed = _AlwaysPartialClose()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=_TRIGGER_BLOCK)
    # The position genuinely never reached zero...
    assert signed.position > _D(0)
    # ...so the operator has to be told, with the remainder named.
    assert runner.staged_long_residual is not None
    assert "filled only" in runner.staged_long_residual


def test_refused_staged_close_flags_a_residual_without_reddening_the_tests(live_db):
    # A cleanup that never flattened must reach the operator — but it is not a
    # test verdict: the trigger tests themselves already round-tripped the wire
    # actions they exist to prove, and stay green.
    signed = _RefusesCloses()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=_TRIGGER_BLOCK)
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    assert runner.staged_long_residual is not None
    assert "refused (no liquidity)" in runner.staged_long_residual
    assert {latest[key]["status"] for key in _TRIGGER_BLOCK} == {"passed"}
    # A refused close closed NOTHING, so the residual must not shrink either:
    # both attempts ask for the whole staged size.
    closes = [c["size"] for c in signed.place_calls if c.get("reduce_only")]
    assert closes == [_D("0.001"), _D("0.001")]


def test_backstop_close_clears_the_staged_long_residual(live_db):
    # The flag is a live state, not a scar: the in-loop close is refused (flag
    # set), the finally backstop's retry flattens, and the CLI must NOT then
    # warn about a position that no longer exists.
    signed = _RefusesFirstClose()
    with live_db:
        runner = smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery()))
        runner.run(only=_TRIGGER_BLOCK)
    closes = [c for c in signed.place_calls if c.get("reduce_only")]
    assert len(closes) == 2  # the refusal happened, so the flag WAS set
    assert runner.staged_long_residual is None


# -- probe sizing: wire fidelity, zero-floor abort, non-positive mark guard --
# (test-coverage pass 2026-07-30: no test asserted _probe_size()'s computed
# size actually reaches the wire call, drove _staged_probe_size()'s
# zero-floor abort, or exercised _probe_size()'s own non-positive mark guard.)


def test_probe_size_reaches_the_wire(live_db):
    """_probe_size()'s ceil-to-step notional must be the SIZE the wire call carries.

    Hand-computed independently of calling _probe_size() itself — ceil(11 USDC
    / 60000 mark / 0.00001 step) is 19 steps, i.e. 0.00019 (the same value the
    "old re-derived size" control in test_trigger_probes_are_sized_to_the_staged_position
    already pins for this exact mark/step pair) — so a regression in the
    rounding mode or a dropped max(step, ...) floor would under-size the wire
    call and this test would catch it even though the same bug would also
    corrupt a `_probe_size()`-derived expectation.
    """
    signed = _FakeSigned()
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
    entries = [c for c in signed.place_calls if not c.get("reduce_only")]
    assert len(entries) == 1
    assert entries[0]["size"] == _D("0.00019")


def test_staged_long_that_floors_to_zero_aborts_the_trigger_probe(live_db):
    """A staging fill too small for a coarse qty_step must abort, not size a zero probe.

    _staged_probe_size() floors the staged long to the exchange's qty step; a
    very small partial staging fill against a coarse step floors to exactly
    0 — no reduce-only probe can be sized against a zero-size position, so the
    guard must fail the test loudly instead of sending a zero (or negative)
    order.
    """
    signed = _FakeSigned(place_ack=_Ack("filled", filled_size=_D("0.0001")))
    ctx = replace(_ctx(live_db, signed, run_recovery=lambda: _Recovery()), qty_step=_D("0.01"))
    with live_db:
        smoke.SmokeTestRunner(ctx).run(only=["stop_loss_create"])
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["stop_loss_create"]
    assert row["status"] == "failed"
    assert "floors to zero" in row["error_message"]


def test_non_positive_mark_price_aborts_the_probe_size_guard(live_db):
    """_probe_size()'s own `mark <= 0` guard must be a failed verdict, not a crash.

    A stale/misbehaving mark_price seam returning zero or negative must never
    reach the ceil-to-step division — the guard turns it into the same
    controlled _SmokeAbort every other exchange-refusal path uses, for both a
    zero and a negative mark.
    """
    with live_db:
        for bad_mark in (_D(0), _D(-100)):
            signed = _FakeSigned()
            ctx = replace(
                _ctx(live_db, signed, run_recovery=lambda: _Recovery()),
                mark_price=lambda m=bad_mark: m,
            )
            smoke.SmokeTestRunner(ctx).run(only=["slice_order_submit"])
            latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
            row = latest["slice_order_submit"]
            assert row["status"] == "failed"
            assert "non-positive" in row["error_message"]


# -- SmokeStepResult's status/error_message invariant ----------------------


def test_a_red_verdict_without_an_error_message_is_rejected():
    """A ``failed``/``error`` verdict MUST carry the reason it is red.

    ``live_smoke_tests`` is the append-only record an operator triages a
    real-money go/no-go from; a red row with a NULL error_message is a dead
    end there, and the constructor is the only place that can still refuse it
    before it becomes durable.
    """
    for status in ("failed", "error"):
        with pytest.raises(ValueError, match="must carry error_message"):
            smoke.SmokeStepResult(status)
        # A detail is not a substitute — the triage surface reads error_message.
        with pytest.raises(ValueError, match="must carry error_message"):
            smoke.SmokeStepResult(status, detail="something happened")


def test_a_green_verdict_carrying_an_error_message_is_rejected():
    """The other half: a ``passed``/``skipped`` verdict must NOT carry one.

    An error_message on a green row is a leftover from whatever failed before
    it, misleadingly attached to a passing test — the operator would triage a
    failure that did not happen.
    """
    for status in ("passed", "skipped"):
        with pytest.raises(ValueError, match="must not carry error_message"):
            smoke.SmokeStepResult(status, error_message="stale reason")
        with pytest.raises(ValueError, match="must not carry error_message"):
            smoke.SmokeStepResult(status, detail="ok", error_message="stale reason")


def test_the_four_legal_verdict_shapes_still_construct():
    """Positive control: the invariant must not reject what the runner emits.

    All four shapes the runner actually returns — green with a detail, red
    with a reason — construct unchanged, so the guard cannot be over-tight.
    """
    assert smoke.SmokeStepResult("passed", detail="d").error_message is None
    assert smoke.SmokeStepResult("skipped", detail="dry run").error_message is None
    assert smoke.SmokeStepResult("failed", error_message="exchange refused").detail is None
    assert smoke.SmokeStepResult("error", error_message="boom").detail is None
    # A red verdict may carry BOTH (the reason plus context), which is legal.
    assert smoke.SmokeStepResult("failed", detail="d", error_message="e").detail == "d"


# -- test 3's durable detail carries the exchange's own rejection text ------


def test_submit_folds_the_exchange_rejection_text_into_the_detail(live_db):
    """A per-order refusal's TEXT must survive into the persisted row.

    Test 3's verdict stays green either way (the action reached the matching
    engine, which is all it proves), so the durable ``detail`` is the ONLY
    place a real bug — bad size, off-tick price, insufficient margin — can
    still be seen. Carrying only the coarse ``status=error`` dropped the
    exchange's own explanation and left the operator with nothing to diagnose.
    """
    signed = _FakeSigned(
        place_ack=_Ack(
            "error",
            exchange_order_id=None,
            filled_size=None,
            average_price=None,
            error="Order price cannot be more than 80% away from the reference price",
        )
    )
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_submit"]
    assert row["status"] == "passed"  # the wire round-trip is what test 3 proves
    assert "80% away from the reference price" in row["detail"]


def test_a_clean_submit_detail_carries_no_error_clause(live_db):
    """Control for the test above: no rejection ⇒ no `error=` clause at all.

    A detail that appended ``error=None`` unconditionally would read as a
    refusal on every healthy run and make the clause worthless as a signal.
    """
    signed = _FakeSigned(place_ack=_Ack("filled", filled_size=_D(0)))  # clean, unfilled
    with live_db:
        smoke.SmokeTestRunner(_ctx(live_db, signed, run_recovery=lambda: _Recovery())).run(
            only=["slice_order_submit"]
        )
        latest = repo.latest_smoke_test_results(live_db.conn, "live-BTC")
    row = latest["slice_order_submit"]
    assert row["status"] == "passed"
    assert "error=" not in row["detail"]
    assert row["detail"].endswith(")")  # the envelope still closes properly
