"""Live acceptance validator (§20.3 / §21.4) — metric computation over fake stores."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live import smoke
from contrib.hyperliquid_perp.live.validation import (
    MIN_LIVE_CYCLES,
    MIN_LIVE_ORDERS,
    LiveValidationReport,
    validate_live_run,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from .conftest import insert_decision_attempts

_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
_D = Decimal


def _init_live_run(db: Database, *, mode: str = "testnet_live", run_id: str = "r") -> None:
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=_D(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps({"live": {"mode": mode}}),
        created_at=_T0,
    )


def _pass_all_smoke(db: Database, *, run_id: str = "r") -> None:
    with db.transaction() as conn:
        for test in smoke.SMOKE_TESTS:
            repo.insert_smoke_test_result(
                conn,
                run_id=run_id,
                test_number=test.number,
                test_key=test.key,
                test_name=test.name,
                status="passed",
                network="testnet",
                executed_at=_T0,
            )


def _add_cycles(db: Database, n: int, *, run_id: str = "r") -> None:
    insert_decision_attempts(db, ["completed"] * n, run_id=run_id, start=_T0, mode="live")


def _add_orders(db: Database, n: int, *, run_id: str = "r") -> None:
    with db.transaction() as conn:
        for i in range(n):
            repo.insert_live_order_attempt(
                conn,
                attempt_id=f"a{i}",
                run_id=run_id,
                action="place",
                symbol="BTC",
                attempt_index=0,
                status="acknowledged",
                cloid_logical=f"smoke_r_BTC_o_p_na_000_entry_{i}",
                cloid_hex=f"0x{i:032x}",
                side="buy",
                qty=_D("0.001"),
                price=_D(60000),
                reduce_only=False,
                order_role="entry",
                requested_at=_T0,
            )


def _add_refreshes(db: Database, refreshed: int, failed: int = 0, *, run_id: str = "r") -> None:
    with db.transaction() as conn:
        for _ in range(refreshed):
            repo.insert_kill_switch_event(
                conn, run_id=run_id, event_type="kill_switch_refreshed", timestamp=_T0
            )
        for _ in range(failed):
            repo.insert_kill_switch_event(
                conn, run_id=run_id, event_type="kill_switch_refresh_failed", timestamp=_T0
            )


def _healthy(tmp_path, *, mode: str = "testnet_live") -> Database:
    """A live run that meets every §20.3 acceptance condition."""
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode=mode)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    return db


# -- happy paths -----------------------------------------------------------


def test_healthy_testnet_run_is_ready(tmp_path):
    db = _healthy(tmp_path)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "testnet_live"
    assert report.live_ready
    assert report.failures == ()
    assert report.shortfalls == ()
    assert report.restart_reconciliation_passed
    assert report.emergency_close_test_passed
    assert report.kill_switch_refresh_success_rate == Decimal(1)


def test_order_count_one_short_is_a_shortfall(tmp_path):
    # §20.3 gate boundary at exactly MIN_LIVE_ORDERS − 1: the failing side of
    # the >= comparison (every other test seeds 30) — a flipped comparison or
    # wrong constant would otherwise ship undetected.
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, MIN_LIVE_ORDERS - 1)
    _add_refreshes(db, 100, 0)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("live_order_count" in s for s in report.shortfalls)


def test_mainnet_tiny_healthy_run_is_ready(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "mainnet_tiny"
    assert report.live_ready


def test_mainnet_smoke_booleans_render_na_in_summary(tmp_path):
    # A mainnet_tiny run's live_smoke_tests is empty by design (§21.3 proves smoke
    # on the sibling testnet run); the four *_test_passed lines must render n/a —
    # not "no" — so the report can't be misread as a mainnet smoke failure.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    joined = "\n".join(report.summary_lines())
    assert "emergency_close_test_passed: n/a" in joined
    assert "restart_reconciliation_passed: n/a" in joined
    assert "startup_with_existing_position_test_passed: n/a" in joined
    assert "startup_with_stale_open_order_test_passed: n/a" in joined
    # The underlying field is unchanged — still a plain bool (False with no rows).
    assert report.emergency_close_test_passed is False


def test_testnet_smoke_booleans_render_verdict_in_summary(tmp_path):
    # On a testnet_live run the same lines render the real yes/no verdict.
    db = _healthy(tmp_path)  # testnet, all smoke passed
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    joined = "\n".join(report.summary_lines())
    assert "emergency_close_test_passed: yes" in joined


# -- shortfalls (exit 4) ---------------------------------------------------


def test_short_cycles_is_a_shortfall_not_failure(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, 10)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert report.failures == ()
    assert any("cycle_count" in s for s in report.shortfalls)


def test_missing_smoke_is_a_shortfall(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    assert any("not yet run" in s for s in report.shortfalls)
    assert not report.restart_reconciliation_passed


# -- integrity failures (exit 5) ------------------------------------------


def test_dedupe_error_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_money_drift",
            symbol="BTC",
            exchange_value="tid|1|deadbeef",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.exchange_fill_dedupe_error_count == 1
    assert not report.live_ready
    assert any("dedupe_error" in f for f in report.failures)


def test_orphan_exchange_order_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="startup",
            case_type="orphan_exchange_order",
            symbol="BTC",
            exchange_value="oid-9",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.orphan_exchange_order_count == 1
    assert any("orphan" in f for f in report.failures)


def test_position_mismatch_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="exchange_position_mismatch",
            symbol="BTC",
            exchange_value="0.5",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.local_exchange_position_mismatch_count == 1
    assert not report.live_ready


def test_low_refresh_rate_is_a_failure(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 90, 10)  # 90% < 99%
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_success_rate == Decimal(9) / Decimal(10)
    assert any("refresh_success_rate" in f for f in report.failures)


def test_no_refresh_events_is_a_shortfall(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    # no refresh events at all
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_success_rate is None
    assert report.failures == ()
    assert any("refresh events" in s for s in report.shortfalls)


def test_failed_smoke_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    # Supersede one smoke test with a FAILED result (a later result_id wins).
    with db.transaction() as conn:
        repo.insert_smoke_test_result(
            conn,
            run_id="r",
            test_number=8,
            test_key="stop_loss_create",
            test_name="SL create",
            status="failed",
            executed_at=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert any("FAILED" in f for f in report.failures)
    assert not report.live_ready


def test_unprotected_window_from_protection_events(tmp_path):
    db = _healthy(tmp_path)
    # SL repair exhausted (onset) then SL placed 12s later (close) = 12s window.
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_exhausted",
            symbol="BTC",
            timestamp=_T0,
        )
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_placed",
            symbol="BTC",
            timestamp=_T0 + timedelta(seconds=12),
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unprotected_position_seconds == Decimal(12)
    assert report.unprotected_window_count == 1
    assert not report.unresolved_unprotected_window
    assert any("unprotected_position_seconds" in f for f in report.failures)


def test_open_unprotected_window_flagged(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_blocked",
            symbol="BTC",
            timestamp=_T0,
        )
    with db:
        # No closing event → window still open, measured to `now`.
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=30))
    assert report.unresolved_unprotected_window
    assert report.unprotected_position_seconds == Decimal(30)


def test_emergency_close_event_warns_but_does_not_gate_testnet(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="emergency_close_triggered",
            symbol="BTC",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.emergency_close_event_count == 1
    # An emergency_close_triggered CLOSES any open unprotected window but is not
    # itself an onset, so with no onset there is no unprotected time and the run
    # stays ready on testnet — the event is a warning, not a gate.
    assert report.live_ready
    assert any("emergency close" in w for w in report.warnings)


# -- mainnet_tiny-specific gate -------------------------------------------


def test_mainnet_daily_loss_breach_is_a_failure(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_safe_mode_event(
            conn,
            run_id="r",
            event_type="safe_mode_entered",
            safe_mode_type="recoverable",
            reason="daily_loss",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.daily_loss_breached
    assert any("daily loss" in f for f in report.failures)


def test_mainnet_unresolved_reconciliation_is_a_failure(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="199",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 1
    assert any("unresolved_reconciliation" in f for f in report.failures)


def test_unknown_execution_mode_uses_strict_gate(tmp_path):
    db = Database(tmp_path / "live.db")
    # A live run whose genesis config does not name live.mode.
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=_D(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps({"other": 1}),
        created_at=_T0,
    )
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "unknown"
    assert any("does not name a" in f for f in report.failures)


# -- guards ----------------------------------------------------------------


def test_paper_run_is_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=_D(1000), schema_version=SCHEMA_VERSION
    )
    with db, pytest.raises(ValueError, match="paper run"):
        validate_live_run(db, run_id="r", now=_T0)


def test_missing_run_is_rejected(tmp_path):
    db = Database(tmp_path / "e.db")
    with db, pytest.raises(ValueError, match="does not exist"):
        validate_live_run(db, run_id="nope", now=_T0)


# -- §21.4 mainnet gate does NOT depend on same-run smoke (decided 2026-07-27) --


def test_mainnet_without_smoke_is_ready(tmp_path):
    # The production mainnet_tiny flow (new run-id, testnet smoke passed on the
    # separate testnet run per §21.3) has an EMPTY live_smoke_tests table — it
    # must still reach live_ready; gating it on smoke would make §21.4 unreachable.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    # deliberately NO _pass_all_smoke
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "mainnet_tiny"
    assert report.live_ready
    # The *_test_passed booleans are False (no smoke) but do not gate mainnet.
    assert not report.restart_reconciliation_passed


def test_testnet_without_smoke_is_a_shortfall(tmp_path):
    # The mirror of the above: on testnet the same empty table IS a shortfall.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="testnet_live")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("not yet run" in s for s in report.shortfalls)


# -- unprotected-window reconstruction ------------------------------------


def _protect(db, events, *, run_id="r"):
    """events: list of (event_type, symbol, offset_seconds_from_T0)."""
    with db.transaction() as conn:
        for event_type, symbol, off in events:
            repo.insert_protection_order_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                symbol=symbol,
                timestamp=_T0 + timedelta(seconds=off),
            )


def test_reonset_while_open_keeps_earliest(tmp_path):
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_repair_blocked", "BTC", 5),  # re-onset while open — ignored
            ("stop_loss_placed", "BTC", 20),  # close → window measured from t0
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(20)
    assert r.unprotected_window_count == 1


def test_two_symbols_concurrent_windows_sum(tmp_path):
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_placed", "BTC", 10),
            ("stop_loss_placed", "ETH", 30),
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(40)
    assert r.unprotected_window_count == 2


def test_close_without_open_is_a_noop(tmp_path):
    db = _healthy(tmp_path)
    _protect(db, [("stop_loss_placed", "BTC", 10)])  # close, no prior onset
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == 0
    assert r.unprotected_window_count == 0
    assert r.live_ready


def test_out_of_order_timestamps_clamp_per_window(tmp_path):
    # A BTC close BEFORE its onset (corrupt/skew) clamps that window to 0 and must
    # NOT subtract from ETH's genuine 15s (the per-window clamp, not global).
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 10),
            ("stop_loss_placed", "BTC", 5),  # 5s BEFORE onset → -5, clamped to 0
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_placed", "ETH", 15),
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(15)
    assert r.unprotected_window_count == 2


# -- kill-switch refresh-rate boundary ------------------------------------


def test_refresh_rate_exactly_99_percent_passes(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 99, 1)  # 99/100 = 0.99 == threshold; >= passes
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.kill_switch_refresh_success_rate == Decimal(99) / Decimal(100)
    assert r.live_ready


def test_refresh_rate_just_under_99_percent_fails(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 98, 2)  # 98/100 = 0.98 < 0.99
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert not r.live_ready
    assert any("refresh_success_rate" in f for f in r.failures)


# -- LiveValidationReport.__post_init__ invariants ------------------------


def _make_report(**overrides) -> LiveValidationReport:
    base = {
        "run_id": "r",
        "execution_mode": "testnet_live",
        "cycle_count": 30,
        "api_failed_count": 0,
        "live_order_count": 30,
        "fill_count": 0,
        "exchange_fill_dedupe_error_count": 0,
        "orphan_exchange_order_count": 0,
        "duplicate_fill_apply_count": 0,
        "local_exchange_position_mismatch_count": 0,
        "account_replay_mismatch_count": 0,
        "unprotected_position_seconds": Decimal(0),
        "unprotected_window_count": 0,
        "unresolved_unprotected_window": False,
        "kill_switch_refresh_success_rate": Decimal(1),
        "kill_switch_refresh_total": 100,
        "restart_reconciliation_passed": True,
        "emergency_close_test_passed": True,
        "startup_with_existing_position_test_passed": True,
        "startup_with_stale_open_order_test_passed": True,
        "unresolved_reconciliation_mismatch_count": 0,
        "daily_loss_breached": False,
        "emergency_close_event_count": 0,
        "failures": (),
        "shortfalls": (),
    }
    base.update(overrides)
    return LiveValidationReport(**base)


def test_post_init_accepts_a_consistent_report():
    _make_report()  # must not raise


def test_post_init_rejects_rate_present_with_zero_total():
    with pytest.raises(ValueError, match="None iff"):
        _make_report(kill_switch_refresh_success_rate=Decimal("0.5"), kill_switch_refresh_total=0)


def test_post_init_rejects_rate_out_of_range():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        _make_report(kill_switch_refresh_success_rate=Decimal("1.5"), kill_switch_refresh_total=10)


def test_post_init_rejects_open_window_without_count():
    with pytest.raises(ValueError, match="counted window"):
        _make_report(unresolved_unprotected_window=True, unprotected_window_count=0)


def test_post_init_rejects_negative_seconds():
    with pytest.raises(ValueError, match="must be >= 0"):
        _make_report(unprotected_position_seconds=Decimal(-1))


# -- §21.4 refresh-rate gate + stamped cases + warnings (2026-07-27) --------


def test_mainnet_low_refresh_rate_is_a_failure(tmp_path):
    # The dead man's switch matters MOST on real money: a mainnet_tiny run whose
    # refresh success rate is below 99% must exit 5, exactly as testnet does.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_refreshes(db, 85, 15)  # 85%
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("kill_switch_refresh_success_rate" in f for f in report.failures)


def test_mainnet_no_refresh_events_is_a_shortfall(tmp_path):
    # Zero refresh evidence on mainnet = "keep running", not a 0% failure —
    # mirroring the testnet reading of the same condition.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    assert any("no kill-switch refresh events" in s for s in report.shortfalls)


def test_stamped_reconciliation_case_does_not_fail_the_mainnet_gate(tmp_path):
    # A mismatch case a human already stamped (action_taken set) is RESOLVED:
    # it must drop out of unresolved_reconciliation_mismatch_count and not fail
    # §21.4 — otherwise every historically-resolved case re-fails the gate
    # forever. (equity_mismatch is unresolved-gated only, not a counted
    # integrity failure, so the stamped run stays live_ready.)
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            action_taken="reviewed: exchange settlement lag, values reconverged",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 0
    assert report.live_ready


def test_unstamped_equity_mismatch_still_fails_the_mainnet_gate(tmp_path):
    # The companion boundary: the SAME case without action_taken stays open.
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 1
    assert any("unresolved_reconciliation_mismatch_count" in f for f in report.failures)


def test_testnet_unresolved_mismatch_warns_but_does_not_gate(tmp_path):
    # On testnet an open manual case is informational (a mainnet gate condition
    # per §21.4) — the report must warn so the operator resolves it before
    # preparing mainnet, without flipping the testnet verdict.
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.live_ready
    assert any("unresolved reconciliation case" in w for w in report.warnings)


def test_mainnet_report_always_reminds_manual_shutdown_item(tmp_path):
    # §21.4's "manual shutdown/restart tested" is operator-confirmed only; the
    # mainnet report carries a fixed warning so exit 0 cannot be read as that
    # checklist item passing. Testnet reports carry no such line.
    mainnet = _healthy(tmp_path, mode="mainnet_tiny")
    with mainnet:
        mainnet_report = validate_live_run(mainnet, run_id="r", now=_T0)
    assert any("manual shutdown/restart" in w for w in mainnet_report.warnings)

    (tmp_path / "t").mkdir()
    testnet = _healthy(tmp_path / "t")
    with testnet:
        testnet_report = validate_live_run(testnet, run_id="r", now=_T0)
    assert not any("manual shutdown/restart" in w for w in testnet_report.warnings)
