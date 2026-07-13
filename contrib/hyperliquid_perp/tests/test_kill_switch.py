"""Tests for the §18 dead man's switch manager (fake client, manual clock)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import CancelAck
from contrib.hyperliquid_perp.live.config import ExecutionMode, KillSwitchConfig
from contrib.hyperliquid_perp.live.kill_switch import KillSwitchManager
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import live_order_attempt_id

_NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
_HEX = "0x" + "ab" * 16
_HEX2 = "0x" + "cd" * 16
# The caller's WORST-CASE wall time between two tick() calls — not its sleep
# interval (the real loop runs the AI decision synchronously in-cycle, so those
# differ by minutes). With the KillSwitchConfig defaults (refresh 30s, deadline
# 120s) the worst-case gap between refreshes is 30 + 30 = 60s, well inside the
# deadline — so the manager's construction-time invariant holds.
_MAX_TICK_GAP_S = 30.0


class _FakeClient:
    """Mirrors the real client: the bound §4.1 gate judges every mutation."""

    def __init__(self, gate):
        self._gate = gate
        self.schedule_calls: list[datetime] = []
        self.schedule_error: Exception | None = None
        self.clear_calls: int = 0
        self.clear_error: Exception | None = None
        self.open_orders_result: list | Exception = []
        self.open_orders_calls: int = 0
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_results: dict[str, CancelAck | Exception] = {}

    def schedule_cancel(self, *, cancel_at):
        self._gate.require_exchange_action()
        if self.schedule_error is not None:
            raise self.schedule_error
        self.schedule_calls.append(cancel_at)

    def clear_scheduled_cancel(self):
        self._gate.require_exchange_action()
        if self.clear_error is not None:
            raise self.clear_error
        self.clear_calls += 1

    def open_orders(self):
        self.open_orders_calls += 1
        if isinstance(self.open_orders_result, Exception):
            raise self.open_orders_result
        return self.open_orders_result

    def cancel_by_cloid(self, *, coin, cloid_hex):
        self._gate.require_exchange_action()
        self.cancel_calls.append((coin, cloid_hex))
        result = self.cancel_results.get(cloid_hex, CancelAck(success=True))
        if isinstance(result, Exception):
            raise result
        return result


def _gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
    )


@pytest.fixture
def env():
    db = Database(":memory:")
    gate = _gate()
    client = _FakeClient(gate)
    clock = ManualClock(_NOW)
    manager = KillSwitchManager(
        client=client,
        gate=gate,
        db=db,
        run_id="r",
        config=KillSwitchConfig(),
        max_tick_gap_seconds=_MAX_TICK_GAP_S,
        clock=clock,
    )
    yield db, client, gate, clock, manager
    db.close()


def _event_types(db):
    return [e["event_type"] for e in repo.iter_kill_switch_events(db.conn, "r")]


def _register_cloid(db, *, logical, hex_id):
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical=logical,
            cloid_hex=hex_id,
            run_id="r",
            symbol="BTC",
            order_role="entry",
        )


def test_disabled_config_cannot_build_a_manager():
    gate = _gate()
    with pytest.raises(ValueError, match="enabled"):
        KillSwitchManager(
            client=_FakeClient(gate),
            gate=gate,
            db=Database(":memory:"),
            run_id="r",
            config=KillSwitchConfig(enabled=False),
            max_tick_gap_seconds=_MAX_TICK_GAP_S,
        )


def _manager_with(config: KillSwitchConfig, *, max_tick_gap_seconds: float):
    gate = _gate()
    return KillSwitchManager(
        client=_FakeClient(gate),
        gate=gate,
        db=Database(":memory:"),
        run_id="r",
        config=config,
        max_tick_gap_seconds=max_tick_gap_seconds,
    )


def test_a_caller_too_slow_to_refresh_in_time_cannot_build_a_manager():
    # The manager only refreshes when its owner ticks it, so the caller's TICK
    # GAP — not the configured interval — bounds how late a refresh can land.
    # The config guard cannot see that number: 60/30 passes it (60 >= 2 x 30),
    # yet a 60s tick gap leaves 30 + 60 = 90s between refreshes against a 60s
    # deadline. The dead man's switch would then fire during normal operation and
    # cancel every order on the wallet. Enforced at construction, not assumed.
    cfg = KillSwitchConfig(schedule_cancel_seconds=60, refresh_interval_seconds=30)
    with pytest.raises(ValueError, match="cannot be refreshed in time"):
        _manager_with(cfg, max_tick_gap_seconds=60.0)


def test_a_caller_fast_enough_to_beat_the_deadline_builds():
    cfg = KillSwitchConfig(schedule_cancel_seconds=120, refresh_interval_seconds=30)
    assert _manager_with(cfg, max_tick_gap_seconds=60.0) is not None  # 30 + 60 < 120


def test_the_worst_case_gap_must_be_strictly_inside_the_deadline():
    # Landing exactly ON the deadline is a race, not a margin.
    cfg = KillSwitchConfig(schedule_cancel_seconds=60, refresh_interval_seconds=30)
    with pytest.raises(ValueError, match="cannot be refreshed in time"):
        _manager_with(cfg, max_tick_gap_seconds=30.0)  # 30 + 30 == 60


def test_a_nonpositive_tick_gap_is_a_wiring_error():
    with pytest.raises(ValueError, match="max_tick_gap_seconds"):
        _manager_with(KillSwitchConfig(), max_tick_gap_seconds=0)


def test_arm_schedules_the_120s_deadline_and_records(env):
    db, client, gate, clock, manager = env
    manager.arm()
    assert manager.armed
    assert gate.kill_switch_active
    # §18.1 default: deadline = now + 120s.
    assert client.schedule_calls == [_NOW + timedelta(seconds=120)]
    assert _event_types(db) == ["kill_switch_armed"]


def test_arm_failure_propagates_hard(env):
    db, client, gate, clock, manager = env
    client.schedule_error = ExchangeRequestError("down")
    with pytest.raises(ExchangeRequestError):
        manager.arm()
    assert not manager.armed
    assert not gate.kill_switch_active


def test_refresh_cadence_is_30s(env):
    db, client, gate, clock, manager = env
    manager.arm()
    assert not manager.refresh_due()
    clock.advance(29)
    assert not manager.refresh_due()
    manager.tick()
    assert len(client.schedule_calls) == 1  # not due yet — no extra call
    clock.advance(1)
    assert manager.refresh_due()
    manager.tick()
    assert len(client.schedule_calls) == 2
    # The refreshed deadline is measured from the refresh instant.
    assert client.schedule_calls[1] == _NOW + timedelta(seconds=30 + 120)
    assert _event_types(db) == ["kill_switch_armed", "kill_switch_refreshed"]


def test_a_tick_a_hair_early_still_refreshes(env):
    # A caller ticking at the same period as refresh_interval_seconds (the
    # intended 30s/30s wiring) lands a few ms later in each cycle than the
    # schedule it is measured against. Under a bare `elapsed >= interval` test
    # that shortfall skipped the refresh and halved the real cadence to every
    # OTHER cycle — silently, since only a gap in the event timestamps would ever
    # show it. The slack absorbs the jitter: a tick a hair early is still due.
    db, client, _, clock, manager = env
    manager.arm()
    clock.advance(29.9)
    assert manager.refresh_due()
    manager.tick()
    assert len(client.schedule_calls) == 2
    assert _event_types(db) == ["kill_switch_armed", "kill_switch_refreshed"]


def test_a_genuinely_early_tick_does_not_refresh(env):
    # The slack is a jitter budget, not a licence to refresh on every tick.
    _, client, _, clock, manager = env
    manager.arm()
    clock.advance(25)
    assert not manager.refresh_due()
    manager.tick()
    assert len(client.schedule_calls) == 1


def test_arming_twice_is_a_wiring_error(env):
    # arm() is startup-only. A second arm() used to set kill_switch_active=True
    # unconditionally, which would silently reopen the §4.1 gate that a refresh
    # failure had latched shut — releasing safe mode by luck instead of through
    # PR 4's §13.4 reconciliation.
    _, _, _, _, manager = env
    manager.arm()
    with pytest.raises(RuntimeError, match="already armed"):
        manager.arm()


def test_refresh_before_arm_is_a_wiring_error(env):
    _, _, _, _, manager = env
    with pytest.raises(RuntimeError, match="before arm"):
        manager.refresh()


def test_refresh_failure_records_flags_and_does_not_raise(env):
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    assert manager.refresh() is False
    assert manager.stop_new_orders
    assert not gate.kill_switch_active  # §4.1: no new order can pass
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert _event_types(db) == ["kill_switch_armed", "kill_switch_refresh_failed"]
    assert "timeout" in events[1]["error_message"]


def test_gate_rejection_during_refresh_is_a_recorded_failure_not_a_crash(env):
    # The client's bound gate raising LiveOrderGateRejected (not an
    # ExchangeError) must land in the same recorded-failure lane.
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    gate.agent_authorized = False  # the client's own gate now refuses
    assert manager.refresh() is False
    assert manager.stop_new_orders
    assert _event_types(db) == ["kill_switch_armed", "kill_switch_refresh_failed"]


def test_recovered_refresh_rearms_exchange_but_gate_stays_closed(env):
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()
    clock.advance(30)
    client.schedule_error = None
    assert manager.refresh() is True
    # The exchange-side switch is re-armed (a second schedule call landed)...
    assert len(client.schedule_calls) == 2
    # ...but §13.4 requires a reconciliation pass (PR 4) before trading
    # resumes: one lucky refresh must NOT re-open the §4.1 gate.
    assert manager.stop_new_orders
    assert not gate.kill_switch_active
    assert _event_types(db) == [
        "kill_switch_armed",
        "kill_switch_refresh_failed",
        "kill_switch_refreshed",
    ]


def test_release_safe_mode_reopens_the_gate_and_survives_the_next_refresh(env):
    # §13.4's release must move BOTH pieces of state. Clearing the latch alone
    # would leave the gate shut until the next successful refresh; opening the
    # gate alone would be reverted by that refresh (it recomputes the gate from
    # the latch). One door, not two knobs — and the release must still be in
    # force one refresh later.
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()
    assert manager.stop_new_orders and not gate.kill_switch_active

    client.schedule_error = None
    manager.release_safe_mode()
    assert not manager.stop_new_orders
    assert gate.kill_switch_active  # new orders may pass again

    clock.advance(30)
    assert manager.refresh() is True
    assert gate.kill_switch_active  # the refresh did NOT revert the release
    assert _event_types(db) == [
        "kill_switch_armed",
        "kill_switch_refresh_failed",
        "kill_switch_refreshed",  # the release itself
        "kill_switch_refreshed",
    ]


def test_release_safe_mode_without_a_latch_is_a_noop(env):
    db, _, gate, _, manager = env
    manager.arm()
    assert manager.release_safe_mode() is True
    assert gate.kill_switch_active
    assert _event_types(db) == ["kill_switch_armed"]  # nothing to release


def test_a_refresh_after_the_deadline_lapsed_knows_the_switch_already_fired(env):
    # max_tick_gap_seconds is a promise the caller makes at construction; this is
    # the only thing that checks it was kept. If more than schedule_cancel_seconds
    # has passed (a decision cycle that blocked for minutes), the exchange HAS
    # cancelled every order on the wallet. Silently re-arming and reopening the
    # gate would leave the engine trading against a book it thinks is still there.
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(200)  # > the 120s deadline: the switch fired while we were away

    assert manager.refresh() is True  # the exchange-side switch is re-armed...
    assert manager.stop_new_orders  # ...but we are latched: our orders are gone
    assert not gate.kill_switch_active  # §4.1 shut until reconciliation
    assert _event_types(db) == [
        "kill_switch_armed",
        "kill_switch_cancel_triggered",
        "kill_switch_refreshed",
    ]


def test_one_lapsed_deadline_is_reported_once(env):
    # Keyed on the SCHEDULE EPOCH, so repeated refreshes against the same lapsed
    # deadline report once...
    db, client, _, clock, manager = env
    manager.arm()
    client.schedule_error = ExchangeRequestError("api down")
    clock.advance(200)  # deadline lapsed; refreshes keep failing, epoch frozen
    manager.refresh()
    clock.advance(30)
    manager.refresh()
    assert _event_types(db).count("kill_switch_cancel_triggered") == 1


def test_a_second_lapsed_deadline_is_reported_again(env):
    # ...but a NEW deadline that lapses is a second, genuine firing.
    db, client, _, clock, manager = env
    manager.arm()
    clock.advance(200)
    manager.refresh()  # reports firing #1, and re-schedules (new epoch)
    clock.advance(200)
    manager.refresh()  # that new deadline lapsed too — firing #2
    assert _event_types(db).count("kill_switch_cancel_triggered") == 2


def test_a_firing_during_an_api_outage_is_still_reported(env, caplog):
    # THE most likely real firing: the API goes down, refreshes fail (which
    # raises the latch), and the deadline lapses mid-outage. Keying the dedupe on
    # the latch would have gone completely silent here — no event, no ERROR —
    # exactly when the wallet was actually swept.
    db, client, gate, clock, manager = env
    caplog.set_level(logging.ERROR, logger="contrib.hyperliquid_perp.live.kill_switch")
    manager.arm()
    client.schedule_error = ExchangeRequestError("api down")
    for _ in range(3):  # refreshes fail across the 120s deadline
        clock.advance(30)
        manager.refresh()
    assert manager.stop_new_orders  # latched by the FAILURES

    clock.advance(60)  # now past the deadline: the exchange swept the wallet
    client.schedule_error = None
    manager.refresh()  # the API is back — this must NOT be a quiet recovery

    assert "kill_switch_cancel_triggered" in _event_types(db)
    assert any("kill switch FIRED" in r.message for r in caplog.records)
    assert manager.stop_new_orders  # still latched
    assert not gate.kill_switch_active


def test_a_failed_disarm_write_does_not_swallow_the_disarm_event(env, monkeypatch):
    # The disarm verdict is what tells an operator (and PR 4) whether the
    # wallet-wide backstop is still armed. _record is fail-loud, so if the flags
    # that suppress a retry (_shutdown_completed, _armed) were set BEFORE that
    # write, one busy-DB moment would lose the kill_switch_disarmed event
    # forever: the signal-handler/finally retry would early-return over it.
    db, client, _, _, manager = env
    manager.arm()
    client.open_orders_result = []  # nothing to cancel: a clean sweep

    real_record = manager._record

    def _flaky_record(event_type, **kw):
        if event_type == "kill_switch_disarmed":
            monkeypatch.setattr(manager, "_record", real_record)  # fail only once
            raise sqlite3.OperationalError("database is locked")
        return real_record(event_type, **kw)

    monkeypatch.setattr(manager, "_record", _flaky_record)
    with pytest.raises(sqlite3.OperationalError):
        manager.shutdown()
    assert "kill_switch_disarmed" not in _event_types(db)

    manager.shutdown()  # the retry must still land the disarm event
    assert _event_types(db).count("kill_switch_disarmed") == 1
    assert not manager.armed


def test_a_disarm_that_physically_happened_is_never_revoked_by_a_retry(env, monkeypatch, caplog):
    # The wire fact outranks a fresh sweep. If clear_scheduled_cancel() SUCCEEDED
    # and only its audit write died, the trigger is already gone from the
    # exchange. A retry whose open_orders() then fails must NOT skip the disarm
    # block and announce "left ARMED — the trigger will fire at its deadline":
    # that statement would be false, and it would be latched forever.
    db, client, _, _, manager = env
    caplog.set_level(logging.WARNING, logger="contrib.hyperliquid_perp.live.kill_switch")
    manager.arm()
    client.open_orders_result = []  # clean sweep

    real_record = manager._record

    def _flaky_record(event_type, **kw):
        if event_type == "kill_switch_disarmed":
            monkeypatch.setattr(manager, "_record", real_record)
            raise sqlite3.OperationalError("database is locked")
        return real_record(event_type, **kw)

    monkeypatch.setattr(manager, "_record", _flaky_record)
    with pytest.raises(sqlite3.OperationalError):
        manager.shutdown()
    assert client.clear_calls == 1  # the exchange HAS forgotten the trigger

    # The retry's enumeration now fails — this sweep is "not clean".
    client.open_orders_result = ExchangeRequestError("api wobble")
    manager.shutdown()

    assert client.clear_calls == 1  # not re-issued: the wire fact was latched
    assert _event_types(db).count("kill_switch_disarmed") == 1
    assert not manager.armed
    assert not any("left ARMED" in r.message for r in caplog.records)


def test_a_failed_audit_write_does_not_swallow_the_firing(env, monkeypatch):
    # The event write is fail-loud by design (a busy DB raises). If the "already
    # reported" mark were set BEFORE it, that single failure would permanently
    # lose the one record that the switch fired: the retry would compute
    # already_reported and skip both the log and the write. Mark after the write.
    db, client, _, clock, manager = env
    manager.arm()
    clock.advance(200)

    real_record = manager._record

    def _flaky_record(event_type, **kw):
        if event_type == "kill_switch_cancel_triggered":
            monkeypatch.setattr(manager, "_record", real_record)  # only fail once
            raise sqlite3.OperationalError("database is locked")
        return real_record(event_type, **kw)

    monkeypatch.setattr(manager, "_record", _flaky_record)
    with pytest.raises(sqlite3.OperationalError):
        manager.refresh()
    assert "kill_switch_cancel_triggered" not in _event_types(db)
    assert manager.stop_new_orders  # but the fail-safe latch is already up

    manager.refresh()  # the retry must still record the firing
    assert _event_types(db).count("kill_switch_cancel_triggered") == 1


def test_shutdown_notices_the_switch_already_fired(env):
    # After a lapsed deadline the exchange has swept the wallet, so open_orders()
    # comes back empty and the sweep would otherwise record a "perfectly clean
    # shutdown, nothing to cancel" — the most misleading trail of all.
    db, client, _, clock, manager = env
    manager.arm()
    clock.advance(200)
    client.open_orders_result = []  # the exchange already cancelled everything
    manager.shutdown()
    assert "kill_switch_cancel_triggered" in _event_types(db)


def test_a_release_on_a_switch_that_already_fired_does_not_resume_trading(env):
    # The release runs a proving refresh, and that refresh detects the lapsed
    # deadline and RE-latches. So refresh() itself succeeds (the exchange-side
    # switch re-arms), but the release must still report failure: our orders were
    # just swept off the book, and resuming trading before reconciliation — on
    # local rows that still say 'open' — is exactly wrong. The latch, not the
    # round-trip, is the verdict.
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()  # latched; the deadline is now ticking down unrefreshed
    client.schedule_error = None

    clock.advance(200)  # the deadline lapses: the exchange fired the switch
    assert manager.release_safe_mode() is False
    assert manager.stop_new_orders  # re-latched by the expiry detection
    assert not gate.kill_switch_active
    assert "kill_switch_cancel_triggered" in _event_types(db)

    # That failed release still re-armed the switch, so the deadline is fresh
    # again — a release retried after reconciliation now succeeds.
    clock.advance(10)
    assert manager.release_safe_mode() is True
    assert gate.kill_switch_active


def _safe_mode_errors(caplog) -> int:
    return sum("entering safe mode" in r.message for r in caplog.records if r.levelname == "ERROR")


def test_entering_safe_mode_is_announced_once_not_on_every_retry(env, caplog):
    # The ERROR is the operator's one signal that new orders are BLOCKED. It must
    # fire on the transition — and NOT again on each failed release attempt of an
    # unchanged state, or a PR 4 reconciliation loop retrying on a cadence would
    # bury the real signal under its own repetitions.
    _, client, _, clock, manager = env
    manager.arm()
    caplog.set_level(logging.ERROR, logger="contrib.hyperliquid_perp.live.kill_switch")

    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()
    assert _safe_mode_errors(caplog) == 1

    clock.advance(30)
    manager.refresh()  # still failing, still safe mode — no new transition
    assert manager.release_safe_mode() is False  # a failed release is not one either
    assert _safe_mode_errors(caplog) == 1

    # A LUCKY successful refresh does not leave safe mode either — the latch is
    # sticky until §13.4 releases it — so it must not clear the announcement.
    # Were it to, the next failure would re-announce a transition that never
    # happened.
    client.schedule_error = None
    clock.advance(30)
    assert manager.refresh() is True
    assert manager.stop_new_orders  # still latched
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()
    assert _safe_mode_errors(caplog) == 1


def test_a_second_entry_into_safe_mode_is_announced_again(env, caplog):
    # The flag tracks "an announcement is outstanding", so a genuine re-entry
    # after a successful release must speak up again.
    _, client, _, clock, manager = env
    manager.arm()
    caplog.set_level(logging.ERROR, logger="contrib.hyperliquid_perp.live.kill_switch")

    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()
    assert _safe_mode_errors(caplog) == 1

    client.schedule_error = None
    assert manager.release_safe_mode() is True

    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout again")
    manager.refresh()
    assert _safe_mode_errors(caplog) == 2


def test_release_safe_mode_fails_closed_when_the_switch_still_cannot_refresh(env):
    # The release is EARNED: it can only be called from a state where the last
    # refresh failed, so the exchange-side deadline is stale and may be seconds
    # from firing. Reopening the gate there would admit new orders against a
    # switch about to cancel them. If the refresh still fails, nothing is
    # released.
    db, client, gate, clock, manager = env
    manager.arm()
    clock.advance(30)
    client.schedule_error = ExchangeRequestError("timeout")
    manager.refresh()

    assert manager.release_safe_mode() is False
    assert manager.stop_new_orders  # re-latched
    assert not gate.kill_switch_active  # gate stays shut
    assert _event_types(db) == [
        "kill_switch_armed",
        "kill_switch_refresh_failed",
        "kill_switch_refresh_failed",
    ]


def test_shutdown_is_idempotent_once_completed(env):
    # A signal handler plus a `finally` will plausibly both reach shutdown().
    # The second call must not re-sweep: re-cancelling already-cancelled orders
    # would record their "unknown order" rejects as fresh failures and write a
    # second started/completed pair into the §18.5 audit trail.
    db, client, _, _, manager = env
    manager.arm()
    manager.shutdown()
    events_after_one = _event_types(db)
    calls_after_one = client.open_orders_calls

    manager.shutdown()
    assert _event_types(db) == events_after_one
    assert client.open_orders_calls == calls_after_one


def test_a_shutdown_that_died_partway_is_retried_not_skipped(env, monkeypatch):
    # The other half of idempotency, and the one that costs money to get wrong.
    # The started-event write is deliberately unguarded (audit loss must fail
    # loud), so a locked DB can blow up shutdown() AFTER it has latched itself.
    # Suppressing the retry on "started" would let the signal-handler/finally
    # pair swallow the ONLY call that cancels our live orders — leaving them
    # resting for the wallet-wide trigger, which also takes out the non-bot
    # orders §19.3 says never to touch. Only a COMPLETED sweep is a no-op.
    db, client, _, _, manager = env
    manager.arm()
    client.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _HEX}]
    _register_cloid(db, logical="log-1", hex_id=_HEX)

    boom = {"n": 0}
    real_record = manager._record

    def _flaky_record(event_type, **kw):
        if event_type == "shutdown_cancel_orders_started" and boom["n"] == 0:
            boom["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real_record(event_type, **kw)

    monkeypatch.setattr(manager, "_record", _flaky_record)
    with pytest.raises(sqlite3.OperationalError):
        manager.shutdown()
    assert client.open_orders_calls == 0  # never got to the sweep

    manager.shutdown()  # the retry must actually sweep
    assert client.open_orders_calls == 1
    assert client.cancel_calls == [("BTC", _HEX)]
    assert "shutdown_cancel_orders_completed" in _event_types(db)


def test_shutdown_cancels_bot_owned_only_with_evidence_rows(env):
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    client.open_orders_result = [
        {"oid": 1, "coin": "BTC", "cloid": _HEX},  # bot-owned → cancel
        {"oid": 2, "coin": "BTC", "cloid": "0x" + "ff" * 16},  # unknown → skip
        {"oid": 3, "coin": "BTC"},  # no cloid → skip
    ]
    manager.shutdown()
    assert client.cancel_calls == [("BTC", _HEX)]
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert _event_types(db) == [
        "kill_switch_armed",
        "shutdown_cancel_orders_started",
        "shutdown_cancel_orders_completed",
        "kill_switch_disarmed",  # §18.2 rule 6: clean sweep unsets the trigger
    ]
    detail = json.loads(events[-2]["detail"])
    assert detail == {"canceled": ["1"], "skipped_non_bot": ["2", "3"], "failures": []}
    # Skipped non-bot orders must NOT block the disarm — protecting them from
    # the wallet-wide trigger is the very point of rule 6.
    assert client.clear_calls == 1
    assert not manager.armed
    # §16.5: the cancel round-trip left its own evidence row.
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [(a["action"], a["status"]) for a in attempts] == [("cancel_by_cloid", "acknowledged")]


def test_shutdown_settles_the_local_order_row(env):
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id="o1",
            mode="live",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("0.01"),
            status="open",
            cloid_logical="log-1",
            cloid_hex=_HEX,
        )
    client.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _HEX}]
    manager.shutdown()
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "canceled"
    assert order["cancel_reason"] == "shutdown_cancel"
    assert order["canceled_at"] is not None


def test_shutdown_survives_per_order_cancel_failures(env):
    db, client, gate, clock, manager = env
    manager.arm()
    for i, hx in enumerate((_HEX, _HEX2)):
        _register_cloid(db, logical=f"log-{i}", hex_id=hx)
    client.open_orders_result = [
        {"oid": 1, "coin": "BTC", "cloid": _HEX},
        {"oid": 2, "coin": "BTC", "cloid": _HEX2},
    ]
    client.cancel_results[_HEX] = ExchangeRequestError("boom")
    manager.shutdown()
    # The failure did not stop the sweep; order 2 still got its cancel.
    assert [c[1] for c in client.cancel_calls] == [_HEX, _HEX2]
    detail = json.loads(repo.iter_kill_switch_events(db.conn, "r")[-1]["detail"])
    assert detail["canceled"] == ["2"]
    assert len(detail["failures"]) == 1 and "boom" in detail["failures"][0]
    # Both round-trips are on the evidence trail, each with its outcome.
    failed = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in failed] == ["failed"]
    ok = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX2)
    assert [a["status"] for a in ok] == ["acknowledged"]
    # A failed cancel keeps the wallet-wide backstop armed (§18.2 rule 6).
    assert client.clear_calls == 0
    assert manager.armed


def test_shutdown_records_a_rejected_cancel_as_a_failure(env):
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    client.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _HEX}]
    client.cancel_results[_HEX] = CancelAck(success=False, error="Order already canceled")
    manager.shutdown()
    detail = json.loads(repo.iter_kill_switch_events(db.conn, "r")[-1]["detail"])
    assert detail["canceled"] == []
    assert "already canceled" in detail["failures"][0]
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["rejected"]


def test_shutdown_survives_non_exchange_failures_too(env):
    # A persistence-layer or unexpected error (NOT an ExchangeError) inside one
    # order's cancel round-trip must not abort the sweep or lose the completed
    # event — the remaining orders still get their attempt.
    db, client, gate, clock, manager = env
    manager.arm()
    for i, hx in enumerate((_HEX, _HEX2)):
        _register_cloid(db, logical=f"log-{i}", hex_id=hx)
    client.open_orders_result = [
        {"oid": 1, "coin": "BTC", "cloid": _HEX},
        {"oid": 2, "coin": "BTC", "cloid": _HEX2},
    ]
    client.cancel_results[_HEX] = RuntimeError("db lock boom")
    manager.shutdown()
    assert [c[1] for c in client.cancel_calls] == [_HEX, _HEX2]
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    detail = json.loads(events[-1]["detail"])
    assert detail["canceled"] == ["2"]
    assert "RuntimeError" in detail["failures"][0]


def test_shutdown_survives_ownership_lookup_failure(env, monkeypatch):
    # The §19.3 ownership lookup (get_cloid_by_hex) sits INSIDE the per-order
    # guard: a repo-layer error identifying ONE order's owner must not abort
    # the sweep — the next order still gets its cancel attempt and the
    # completed event is still written.
    db, client, gate, clock, manager = env
    manager.arm()
    for i, hx in enumerate((_HEX, _HEX2)):
        _register_cloid(db, logical=f"log-{i}", hex_id=hx)
    client.open_orders_result = [
        {"oid": 1, "coin": "BTC", "cloid": _HEX},
        {"oid": 2, "coin": "BTC", "cloid": _HEX2},
    ]
    real_lookup = repo.get_cloid_by_hex

    def flaky_lookup(conn, cloid_hex):
        if cloid_hex == _HEX:
            raise sqlite3.OperationalError("database is locked")
        return real_lookup(conn, cloid_hex)

    monkeypatch.setattr(repo, "get_cloid_by_hex", flaky_lookup)
    manager.shutdown()
    # Order 1 died at the lookup (no cancel round-trip); order 2 completed.
    assert [c[1] for c in client.cancel_calls] == [_HEX2]
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    detail = json.loads(events[-1]["detail"])
    assert detail["canceled"] == ["2"]
    assert detail["skipped_non_bot"] == []
    assert len(detail["failures"]) == 1
    assert "OperationalError" in detail["failures"][0]


def test_shutdown_gate_rejection_does_not_blow_through_the_sweep(env):
    db, client, gate, clock, manager = env
    manager.arm()
    for i, hx in enumerate((_HEX, _HEX2)):
        _register_cloid(db, logical=f"log-{i}", hex_id=hx)
    client.open_orders_result = [
        {"oid": 1, "coin": "BTC", "cloid": _HEX},
        {"oid": 2, "coin": "BTC", "cloid": _HEX2},
    ]
    gate.agent_authorized = False  # the client's bound gate now refuses
    manager.shutdown()
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    detail = json.loads(events[-1]["detail"])
    assert detail["canceled"] == []
    assert len(detail["failures"]) == 2  # both got their attempt, both recorded


def test_empty_sweep_is_clean_and_disarms(env):
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = []
    manager.shutdown()
    assert client.clear_calls == 1
    assert not manager.armed
    assert _event_types(db)[-1] == "kill_switch_disarmed"


def test_disarm_failure_is_recorded_and_stays_armed(env):
    # Disarm is best-effort: its failure must never raise out of the shutdown
    # path, and staying armed is the fail-safe direction.
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = []
    client.clear_error = ExchangeRequestError("disarm down")
    manager.shutdown()
    assert manager.armed
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "kill_switch_disarm_failed"
    assert "disarm down" in events[-1]["error_message"]


def test_post_ack_persistence_failure_counts_as_failure_and_keeps_armed(env, monkeypatch):
    # The dangerous window: the exchange CONFIRMED the cancel, then the local
    # orders-row patch raises. The outcome transaction rolls back whole — the
    # attempt stays 'submitted', the order row stays 'open' (PR 4
    # reconciliation aligns it), the sweep records a failure, and the switch
    # must stay armed (not-clean sweep).
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id="o1",
            mode="live",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("0.01"),
            status="open",
            cloid_logical="log-1",
            cloid_hex=_HEX,
        )
    client.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _HEX}]

    def exploding_update_order(conn, order_id, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "update_order", exploding_update_order)
    manager.shutdown()
    detail = json.loads(repo.iter_kill_switch_events(db.conn, "r")[-1]["detail"])
    assert detail["canceled"] == []
    assert "OperationalError" in detail["failures"][0]
    # The rollback preserved the pre-ack evidence state.
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["submitted"]
    assert repo.get_order(db.conn, "o1")["status"] == "open"
    assert client.clear_calls == 0
    assert manager.armed


def test_shutdown_with_unreachable_exchange_still_completes_the_audit_trail(env):
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = ExchangeRequestError("dns down")
    manager.shutdown()
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    assert "dns down" in events[-1]["error_message"]
    assert json.loads(events[-1]["detail"]) == {
        "canceled": [],
        "skipped_non_bot": [],
        "failures": [],
    }
    # An enumeration failure is NOT a clean sweep: the trigger stays armed —
    # it is the only thing still covering whatever we could not see.
    assert client.clear_calls == 0
    assert manager.armed


def test_shutdown_closes_the_gate_at_sweep_start_independent_of_disarm(env):
    # The §4.1 gate drops at the FIRST statement of shutdown(), before any
    # cancel — not as a consequence of the disarm outcome. Even a clean sweep
    # with a successful disarm must find the gate already closed.
    db, client, gate, clock, manager = env
    manager.arm()
    assert gate.kill_switch_active
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    client.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _HEX}]
    gate_seen_at_disarm: list[bool] = []
    original_clear = client.clear_scheduled_cancel

    def spying_clear():
        gate_seen_at_disarm.append(gate.kill_switch_active)
        return original_clear()

    client.clear_scheduled_cancel = spying_clear
    manager.shutdown()
    # Clean sweep, disarm succeeded...
    assert client.cancel_calls == [("BTC", _HEX)]
    assert client.clear_calls == 1
    assert not manager.armed
    # ...yet the gate was already down when the disarm ran, and stays down.
    assert gate_seen_at_disarm == [False]
    assert not gate.kill_switch_active


def test_tick_after_clean_shutdown_is_a_lifecycle_error(env):
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = []
    manager.shutdown()  # clean sweep, successful disarm
    assert not manager.armed
    with pytest.raises(RuntimeError, match="after shutdown"):
        manager.tick()


def test_refresh_after_shutdown_raises_even_when_disarm_failed(env):
    # Previously a failed disarm left the manager willing to keep refreshing;
    # the retirement boundary now holds regardless of the disarm outcome.
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = []
    client.clear_error = ExchangeRequestError("disarm down")
    manager.shutdown()
    assert manager.armed  # disarm failed — exchange-side switch still set
    with pytest.raises(RuntimeError, match="after shutdown"):
        manager.refresh()


def test_arm_after_shutdown_is_a_lifecycle_error(env):
    # arm() shares the terminal boundary tick()/refresh() enforce: re-arming a
    # retired manager would re-issue scheduleCancel and silently reopen the
    # §4.1 gate condition shutdown() just closed.
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = []
    manager.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        manager.arm()
    assert not gate.kill_switch_active
    assert client.schedule_calls == [_NOW + timedelta(seconds=120)]  # arm #1 only


def test_malformed_open_orders_entry_is_a_failure_not_a_sweep_abort(env):
    # One non-dict entry in the enumeration = unknown ownership: it lands in
    # failures (keeping the wallet-wide backstop armed), the well-formed order
    # after it still gets its cancel, and the completed event is written.
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    client.open_orders_result = ["not a dict", {"oid": 2, "coin": "BTC", "cloid": _HEX}]
    manager.shutdown()
    assert client.cancel_calls == [("BTC", _HEX)]
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    detail = json.loads(events[-1]["detail"])
    assert detail["canceled"] == ["2"]
    assert len(detail["failures"]) == 1
    assert "malformed open_orders entry" in detail["failures"][0]
    assert client.clear_calls == 0
    assert manager.armed


def test_non_list_open_orders_counts_as_enumeration_failure(env):
    # A top-level shape surprise (dict instead of list) must not crash the
    # shutdown path: it is the same "cannot enumerate" class as a transport
    # failure — completed event with the error, backstop stays armed.
    db, client, gate, clock, manager = env
    manager.arm()
    client.open_orders_result = {"error": "unexpected"}  # type: ignore[assignment]
    manager.shutdown()
    events = repo.iter_kill_switch_events(db.conn, "r")
    assert events[-1]["event_type"] == "shutdown_cancel_orders_completed"
    assert "expected a list" in events[-1]["error_message"]
    assert client.cancel_calls == []
    assert client.clear_calls == 0
    assert manager.armed


def test_unarmed_shutdown_sweeps_but_never_disarms(env):
    # A shutdown before arm() still runs the sweep and writes its audit pair,
    # but there is no scheduleCancel to clear and no disarmed event to fake.
    db, client, gate, clock, manager = env
    client.open_orders_result = []
    manager.shutdown()
    assert _event_types(db) == [
        "shutdown_cancel_orders_started",
        "shutdown_cancel_orders_completed",
    ]
    assert client.clear_calls == 0
    assert not manager.armed


def test_registry_hit_without_coin_is_a_failure_not_a_skip(env):
    # Bot-owned (registry hit) but the open_orders payload has no 'coin' to
    # address the cancel with: "ours but uncancelable" must land in failures
    # (blocking the disarm), never in skipped_non_bot.
    db, client, gate, clock, manager = env
    manager.arm()
    _register_cloid(db, logical="log-1", hex_id=_HEX)
    client.open_orders_result = [{"oid": 1, "cloid": _HEX}]  # no "coin" key
    manager.shutdown()
    assert client.cancel_calls == []  # no round-trip was possible
    detail = json.loads(repo.iter_kill_switch_events(db.conn, "r")[-1]["detail"])
    assert detail["canceled"] == []
    assert detail["skipped_non_bot"] == []
    assert len(detail["failures"]) == 1
    assert "missing 'coin'" in detail["failures"][0]
    # Not a clean sweep: the wallet-wide backstop stays armed for this order.
    assert client.clear_calls == 0
    assert manager.armed


def test_cross_run_cancel_evidence_spans_runs_without_index_collision(env):
    # Regression (D1): run 'r-old' already holds (cancel_by_cloid, H, 0) on
    # the evidence trail. A later run's sweep canceling the same surviving
    # order must derive attempt_index 1 — not re-derive 0 and die on the
    # UNIQUE (cloid_hex, action, attempt_index) before the round-trip.
    db, client, gate, clock, _ = env
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical="log-old",
            cloid_hex=_HEX,
            run_id="r-old",
            symbol="BTC",
            order_role="entry",
        )
        repo.insert_live_order_attempt(
            conn,
            attempt_id=live_order_attempt_id("r-old", "cancel_by_cloid", _HEX, 0),
            run_id="r-old",
            action="cancel_by_cloid",
            symbol="BTC",
            attempt_index=0,
            cloid_logical="log-old",
            cloid_hex=_HEX,
            requested_at=_NOW - timedelta(days=1),
        )
    manager = KillSwitchManager(
        client=client,
        gate=gate,
        db=db,
        run_id="r-new",
        config=KillSwitchConfig(),
        max_tick_gap_seconds=_MAX_TICK_GAP_S,
        clock=clock,
    )
    manager.arm()
    client.open_orders_result = [{"oid": 7, "coin": "BTC", "cloid": _HEX}]
    manager.shutdown()
    assert client.cancel_calls == [("BTC", _HEX)]
    attempts = repo.iter_live_order_attempts(db.conn, "r-new", cloid_hex=_HEX)
    assert [(a["attempt_index"], a["status"]) for a in attempts] == [(1, "acknowledged")]
    # No IntegrityError anywhere → the sweep was clean and the disarm landed.
    assert client.clear_calls == 1
    assert not manager.armed
