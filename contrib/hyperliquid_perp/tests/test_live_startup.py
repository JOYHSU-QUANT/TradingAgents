"""Tests for the §19 startup recovery (fake signed client, manual clock)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import CancelAck
from contrib.hyperliquid_perp.live.config import ExecutionMode, KillSwitchConfig
from contrib.hyperliquid_perp.live.kill_switch import KillSwitchManager
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.reconcile import LiveReconciler
from contrib.hyperliquid_perp.live.safe_mode import REASON_NON_BOT_OWNED_ORDER, SafeModeManager
from contrib.hyperliquid_perp.live.startup import run_startup_recovery
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from .conftest import echo_order_status_cloid

_NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
_HEX_ENTRY = "0x" + "ab" * 16
_HEX_CLOSE = "0x" + "cd" * 16
_HEX_SL = "0x" + "ef" * 16


def _clearinghouse(
    *, account_value: str = "100", maintenance: str = "0", positions: list[dict] | None = None
) -> dict:
    return {
        "marginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
        },
        "withdrawable": account_value,
        "crossMaintenanceMarginUsed": maintenance,
        "assetPositions": [{"position": p} for p in (positions or [])],
    }


def _btc_position(szi: str = "0.001", upnl: str = "1", value: str = "51") -> dict:
    return {
        "coin": "BTC",
        "szi": szi,
        "entryPx": "50000",
        "unrealizedPnl": upnl,
        "positionValue": value,
        "marginUsed": value,
    }


class _FakeSigned:
    """The signed-client surface startup touches; cancels mutate open_orders."""

    def __init__(self, gate, clock):
        self._gate = gate
        self._clock = clock
        self.open_orders_result: list = []
        self.cancel_results: dict[str, CancelAck | Exception] = {}
        self.cancel_calls: list[tuple[str, str]] = []
        self.schedule_error: Exception | None = None
        self.schedule_calls: list[datetime] = []
        self.clear_calls = 0
        self.order_status: dict[str, object] = {}

    def schedule_cancel(self, *, cancel_at):
        self._gate.require_exchange_action()
        if self.schedule_error is not None:
            raise self.schedule_error
        self.schedule_calls.append(cancel_at)

    def clear_scheduled_cancel(self):
        self._gate.require_exchange_action()
        self.clear_calls += 1

    def exchange_time(self):
        return self._clock.now()

    def open_orders(self):
        return list(self.open_orders_result)

    def query_order_by_cloid(self, cloid_hex):
        result = self.order_status.get(cloid_hex, {"status": "unknownOid"})
        if isinstance(result, Exception):
            raise result
        return echo_order_status_cloid(result, cloid_hex)

    def cancel_by_cloid(self, *, coin, cloid_hex):
        self._gate.require_exchange_action()
        self.cancel_calls.append((coin, cloid_hex))
        result = self.cancel_results.get(cloid_hex, CancelAck(success=True))
        if isinstance(result, Exception):
            raise result
        if result.success:
            self.open_orders_result = [
                o
                for o in self.open_orders_result
                # A non-dict entry (the exchange returning junk) is never the
                # order being cancelled, and must survive the filter — the fake
                # must not fail where the real client would not.
                if not isinstance(o, dict) or o.get("cloid") != cloid_hex
            ]
        return result


class _Env:
    def __init__(self, tmp_path):
        self.db = Database(":memory:")
        accounting.initialize_run(
            self.db,
            run_id="r",
            mode="live",
            initial_balance_usdc=Decimal(100),
            schema_version=SCHEMA_VERSION,
            created_at=_NOW - timedelta(days=1),
        )
        self.gate = RealOrderGate(
            allow_real_orders=True,
            mode=ExecutionMode.TESTNET_LIVE,
            allowed_symbols=("BTC",),
            agent_authorized=True,
        )
        self.clock = ManualClock(_NOW)
        self.client = _FakeSigned(self.gate, self.clock)
        self.clearinghouse = _clearinghouse()
        self.payload_dir = tmp_path / "payloads"
        self.kill_switch = KillSwitchManager(
            client=self.client,
            gate=self.gate,
            db=self.db,
            run_id="r",
            config=KillSwitchConfig(),
            max_tick_gap_seconds=30.0,
            network_timeout_s=None,
            payload_dir=self.payload_dir,
            clock=self.clock,
        )
        self.safe_mode = SafeModeManager(db=self.db, run_id="r", gate=self.gate, clock=self.clock)
        self.reconciler = LiveReconciler(
            db=self.db,
            run_id="r",
            coin="BTC",
            fetch_open_orders=self.client.open_orders,
            fetch_clearinghouse=self.fetch_clearinghouse,
            query_order_by_cloid=self.client.query_order_by_cloid,
            fetch_fills=lambda start, end: [],
            payload_dir=self.payload_dir,
            clock=self.clock,
        )

    def fetch_clearinghouse(self):
        return self.clearinghouse

    def recover(self):
        return run_startup_recovery(
            db=self.db,
            run_id="r",
            client=self.client,
            fetch_clearinghouse=self.fetch_clearinghouse,
            gate=self.gate,
            kill_switch=self.kill_switch,
            reconciler=self.reconciler,
            safe_mode=self.safe_mode,
            payload_dir=self.payload_dir,
            clock=self.clock,
        )

    def register_order(self, *, order_id, hex_id, logical, role, status="open"):
        with self.db.transaction() as conn:
            repo.insert_cloid_mapping(
                conn,
                cloid_logical=logical,
                cloid_hex=hex_id,
                run_id="r",
                symbol="BTC",
                order_role=role,
            )
            repo.insert_order(
                conn,
                order_id=order_id,
                mode="live",
                run_id="r",
                symbol="BTC",
                order_role=role,
                side="buy" if role in ("entry", "rebalance") else "sell",
                order_type="ioc_limit",
                qty=Decimal("0.001"),
                status=status,
                cloid_logical=logical,
                cloid_hex=hex_id,
                is_bot_owned=True,
                timestamp=_NOW - timedelta(hours=2),
            )

    def close(self):
        self.db.close()


@pytest.fixture
def env(tmp_path):
    e = _Env(tmp_path)
    yield e
    e.close()


# -- the clean paths -----------------------------------------------------------


def test_a_clean_startup_passes_and_opens_the_startup_gate(env):
    result = env.recover()
    assert result.passed
    assert env.gate.startup_reconciliation_passed is True
    assert env.gate.state_reconciled is True
    assert env.kill_switch.armed  # step 5 ran; shutdown is the CLI's job, not recovery's
    assert result.canceled_stale == ()


def test_an_existing_position_with_a_valid_sl_passes_after_reconciliation(env):
    # §19.2 row 1: position has valid SL → allow normal operation.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position()]
    )
    env.client.open_orders_result = [
        {"oid": 5, "coin": "BTC", "cloid": _HEX_SL, "side": "A", "sz": "0.001", "reduceOnly": True}
    ]
    result = env.recover()
    assert result.passed
    assert result.kept_orders == ("5",)  # the SL is validated, never cancelled


# -- §19.3 stale-order handling ---------------------------------------------------


def test_a_stale_entry_order_is_cancelled_with_the_evidence_trail(env):
    env.register_order(order_id="o-e", hex_id=_HEX_ENTRY, logical="log-e", role="entry")
    env.client.open_orders_result = [
        {"oid": 7, "coin": "BTC", "cloid": _HEX_ENTRY, "side": "B", "sz": "0.001"}
    ]
    result = env.recover()
    assert result.canceled_stale == ("7",)
    assert ("BTC", _HEX_ENTRY) in env.client.cancel_calls
    order = repo.get_order(env.db.conn, "o-e")
    assert order["status"] == "canceled"
    assert order["cancel_reason"] == "stale_startup_cancel"
    attempts = repo.iter_live_order_attempts(env.db.conn, "r")
    assert any(a["action"] == "cancel_by_cloid" and a["cloid_hex"] == _HEX_ENTRY for a in attempts)
    assert result.passed  # the cancel resolved the staleness; verdict is clean


def test_a_close_order_that_still_closes_the_position_is_kept(env):
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.register_order(order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position()]
    )
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        },
        {"oid": 5, "coin": "BTC", "cloid": _HEX_SL, "side": "A", "sz": "0.001", "reduceOnly": True},
    ]
    result = env.recover()
    assert "8" in result.kept_orders
    assert ("BTC", _HEX_CLOSE) not in env.client.cancel_calls


def test_a_close_order_with_no_position_to_close_is_stale_and_cancelled(env):
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    result = env.recover()
    assert result.canceled_stale == ("8",)


def test_an_invalid_sl_is_kept_but_the_run_stays_unprotected(env):
    # §19.3: protection is validated, never cancelled here — and an SL that
    # does not cover the position leaves it unprotected (§17.1), which keeps
    # the verdict unclean and the run in safe mode until PR 5 can repair.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.002"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position(szi="0.002", value="102")]
    )
    env.client.open_orders_result = [
        {"oid": 5, "coin": "BTC", "cloid": _HEX_SL, "side": "A", "sz": "0.001", "reduceOnly": True}
    ]
    result = env.recover()
    assert ("BTC", _HEX_SL) not in env.client.cancel_calls  # never cancelled
    assert not result.passed
    assert not result.report.position_protected
    assert env.safe_mode.active


def test_a_failed_positions_read_keeps_close_orders_instead_of_cancelling(env):
    # "Unknown" is not "flat": when the sweep cannot read positions, a resting
    # reduce-only close order must be KEPT (cancelling it on a read failure
    # would strip a real position's close intent), and the recorded failure
    # holds the run in safe mode.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    # The reconciler's own passes (env.fetch_clearinghouse) keep seeing the
    # position; ONLY the sweep's positions read — the flaky fetch handed to
    # run_startup_recovery — fails. A real position exists, so cancelling the
    # close order off the failed read would strip live close intent.
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position()]
    )

    def flaky_fetch():
        raise RuntimeError("transient network blip")

    result = run_startup_recovery(
        db=env.db,
        run_id="r",
        client=env.client,
        fetch_clearinghouse=flaky_fetch,
        gate=env.gate,
        kill_switch=env.kill_switch,
        reconciler=env.reconciler,
        safe_mode=env.safe_mode,
        payload_dir=env.payload_dir,
        clock=env.clock,
    )
    assert ("BTC", _HEX_CLOSE) not in env.client.cancel_calls  # kept, not cancelled
    assert "8" in result.kept_orders
    assert result.sweep_failures
    assert not result.passed
    assert env.safe_mode.active


def test_a_non_bot_order_is_left_alone_and_forces_manual_safe_mode(env):
    env.client.open_orders_result = [{"oid": 9, "coin": "BTC", "cloid": None, "sz": "1"}]
    result = env.recover()
    assert env.client.cancel_calls == []
    assert not result.passed
    state = env.safe_mode.current()
    assert state is not None and state.is_manual
    assert state.reason == REASON_NON_BOT_OWNED_ORDER
    assert env.gate.startup_reconciliation_passed is False


def test_a_failed_cancel_keeps_the_sweep_going_and_the_run_in_safe_mode(env):
    env.register_order(order_id="o-e", hex_id=_HEX_ENTRY, logical="log-e", role="entry")
    env.register_order(order_id="o-e2", hex_id=_HEX_CLOSE, logical="log-e2", role="entry")
    env.client.open_orders_result = [
        {"oid": 7, "coin": "BTC", "cloid": _HEX_ENTRY, "side": "B", "sz": "0.001"},
        {"oid": 8, "coin": "BTC", "cloid": _HEX_CLOSE, "side": "B", "sz": "0.001"},
    ]
    env.client.cancel_results[_HEX_ENTRY] = ExchangeRequestError("cancel refused")
    result = env.recover()
    assert result.canceled_stale == ("8",)  # the sweep continued past the failure
    assert result.sweep_failures
    assert not result.passed
    state = env.safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"


# -- persisted safe mode / hard failures --------------------------------------------


def test_a_persisted_manual_safe_mode_blocks_the_startup_verdict(env):
    env.safe_mode.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    result = env.recover()
    assert not result.passed
    assert env.gate.manual_safe_mode is True
    assert env.gate.startup_reconciliation_passed is False


def test_an_arming_failure_propagates_hard(env):
    env.client.schedule_error = ExchangeRequestError("scheduleCancel down")
    with pytest.raises(ExchangeRequestError):
        env.recover()
    assert env.gate.startup_reconciliation_passed is False


def test_the_kill_switch_is_ticked_between_recovery_legs(env, monkeypatch):
    # The recovery's legs are network round-trips that can stretch past the
    # scheduled-cancel deadline; the switch must be ticked between them (and
    # per swept order), or it fires mid-recovery.
    ticks = {"n": 0}
    real_tick = env.kill_switch.tick

    def counting_tick():
        ticks["n"] += 1
        real_tick()

    monkeypatch.setattr(env.kill_switch, "tick", counting_tick)
    env.register_order(order_id="o-e", hex_id=_HEX_ENTRY, logical="log-e", role="entry")
    env.client.open_orders_result = [
        {"oid": 7, "coin": "BTC", "cloid": _HEX_ENTRY, "side": "B", "sz": "0.001"}
    ]
    env.recover()
    # After pass 1, once per swept order, and after the sweep: >= 3 here.
    assert ticks["n"] >= 3


# -- round-3 hardening (2026-07-17 review loop) ---------------------------------

_HEX_SL2 = "0x" + "12" * 16
_HEX_TP = "0x" + "34" * 16


def test_split_sl_legs_and_a_partial_tp_are_not_falsely_flagged(env, caplog):
    # Size coverage is the reconciler leg's AGGREGATE job: two SLs jointly
    # covering the position, and a deliberately partial take_profit
    # (a scale-out), are legitimate shapes — the sweep's per-order structural
    # check must not warn "does not validate" over them.
    import logging

    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.002"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-sl1", hex_id=_HEX_SL, logical="log-sl1", role="stop_loss")
    env.register_order(order_id="o-sl2", hex_id=_HEX_SL2, logical="log-sl2", role="stop_loss")
    env.register_order(order_id="o-tp", hex_id=_HEX_TP, logical="log-tp", role="take_profit")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position(szi="0.002", value="102")]
    )
    env.client.open_orders_result = [
        {
            "oid": 5,
            "coin": "BTC",
            "cloid": _HEX_SL,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
            "isTrigger": True,
            "triggerPx": "48000",
        },
        {
            "oid": 6,
            "coin": "BTC",
            "cloid": _HEX_SL2,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
            "isTrigger": True,
            "triggerPx": "48000",
        },
        # A partial TP resting as a plain reduce-only limit: legitimate.
        {"oid": 9, "coin": "BTC", "cloid": _HEX_TP, "side": "A", "sz": "0.001", "reduceOnly": True},
    ]
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.live.startup"):
        result = env.recover()
    assert result.passed  # the two SL legs jointly cover 0.002 (§17.1)
    assert sorted(result.kept_orders) == ["5", "6", "9"]
    assert not [r for r in caplog.records if "does not validate" in r.getMessage()]


def test_a_plain_limit_stop_loss_still_warns_structurally(env, caplog):
    # The trigger requirement stays: a stop_loss-role order resting as a plain
    # limit protects nothing at the trigger level — kept, but named in the log.
    import logging

    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position()]
    )
    env.client.open_orders_result = [
        {"oid": 5, "coin": "BTC", "cloid": _HEX_SL, "side": "A", "sz": "0.001", "reduceOnly": True}
    ]
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.live.startup"):
        result = env.recover()
    assert "5" in result.kept_orders  # protection is never cancelled here
    assert [r for r in caplog.records if "does not validate" in r.getMessage()]


# -- round-4 hardening: oversized close orders, short-position closing side ---


def test_an_oversized_close_order_is_kept_not_cancelled(env):
    # The position shrank during downtime (ADL, a manual partial close): the
    # resting reduce-only close order's remaining size now exceeds the
    # position. Cancelling the ONLY closing order over that — PR 4 has no
    # re-placement path — would leave the position open, so size is
    # deliberately not judged (decided 2026-07-17); reduce-only means the
    # exchange clamps the excess.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position()]
    )
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "A",
            "sz": "0.002",  # exceeds the 0.001 position
            "reduceOnly": True,
        }
    ]
    result = env.recover()
    assert "8" in result.kept_orders
    assert ("BTC", _HEX_CLOSE) not in env.client.cancel_calls


def test_a_short_position_keeps_its_bid_side_close_order(env):
    # The closing side of a SHORT is the bid ("B"). Every prior sweep test
    # used long positions — a sign flip in hl_closing_side's short branch
    # would silently invert keep/cancel for shorts.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("-0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position(szi="-0.001")]
    )
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "B",  # a buy closes a short
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    result = env.recover()
    assert "8" in result.kept_orders
    assert ("BTC", _HEX_CLOSE) not in env.client.cancel_calls


def test_a_short_position_cancels_an_ask_side_close_order(env):
    # A sell-side "close" order on a short rests in the short-ADDING
    # direction: it can never close the position and is stale.
    with env.db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("-0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    env.register_order(order_id="o-c", hex_id=_HEX_CLOSE, logical="log-c", role="close")
    env.clearinghouse = _clearinghouse(
        account_value="101", maintenance="1", positions=[_btc_position(szi="-0.001")]
    )
    env.client.open_orders_result = [
        {
            "oid": 8,
            "coin": "BTC",
            "cloid": _HEX_CLOSE,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    result = env.recover()
    assert result.canceled_stale == ("8",)


# -- manual evidence is sticky across the two passes --------------------------


class _StubBackfiller:
    """A fully-wired fill leg, so legs_skipped cannot withhold the release."""

    def backfill(self, now, *, since=None):
        from contrib.hyperliquid_perp.live.fill_backfill import BackfillSummary

        return BackfillSummary(
            fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0, complete=True
        )


def test_a_manual_fact_seen_only_by_the_first_pass_still_latches_safe_mode(env):
    # Decided 2026-07-17: a non-bot order proves someone else operates this
    # wallet. If its owner pulls it between the two recovery passes, the verdict
    # pass reads clean — but the evidence still happened, and §13.5 requires a
    # human to acknowledge it. Without the first-pass latch the run would start
    # trading with nothing but a DB row nobody is forced to read.
    env.client.open_orders_result = [
        {"oid": 99, "coin": "BTC", "cloid": None, "side": "B", "sz": "1"}
    ]
    real_run = env.reconciler.run

    def run_then_vanish(trigger, **kwargs):
        report = real_run(trigger, **kwargs)
        env.client.open_orders_result = []  # the owner pulls it mid-recovery
        return report

    env.reconciler.run = run_then_vanish
    result = env.recover()

    assert result.report.clean  # the verdict pass saw a clean book
    state = env.safe_mode.current()
    assert state is not None and state.is_manual
    assert state.reason == REASON_NON_BOT_OWNED_ORDER
    assert env.gate.manual_safe_mode is True
    assert not result.passed  # step 16 refuses: manual safe mode is latched


def test_startup_never_attests_ws_restored_from_a_wiring_with_no_ws(env):
    # Decided 2026-07-17: "no WS to restore" is not "WS restored". A recoverable
    # safe mode a PRIOR process entered for ws_disconnect must NOT be auto-
    # released by this one-shot recovery, which has no WS stream at all — the
    # same "absent machinery ≠ healthy" stance the §13.4 seam gate takes.
    env.safe_mode.enter("recoverable", "ws_disconnect")
    env.reconciler._backfiller = _StubBackfiller()

    result = env.recover()

    assert result.report.clean  # the books themselves are fine
    state = env.safe_mode.current()
    assert state is not None  # the ws_disconnect latch still holds
    assert state.reason == "ws_disconnect"
    assert not result.passed


# -- the sweep's defensive guards over malformed exchange data ----------------


def test_a_malformed_open_orders_entry_is_recorded_and_the_sweep_continues(env):
    # The sweep must keep going over a junk entry (recording the failure), not
    # crash startup on `order.get` — an unparseable neighbour must not strand
    # the real orders behind it.
    env.register_order(order_id="o-e", hex_id=_HEX_ENTRY, logical="log-e", role="entry")
    env.client.open_orders_result = [
        "not-a-dict",
        {"oid": 5, "coin": "BTC", "cloid": _HEX_ENTRY, "side": "B", "sz": "0.001"},
    ]
    result = env.recover()
    assert result.canceled_stale == ("5",)  # the valid order was still swept
    assert any("malformed open_orders entry" in f for f in result.sweep_failures)
    assert not result.passed


def test_an_open_orders_read_that_is_not_a_list_is_a_sweep_failure_not_a_crash(env):
    env.client.open_orders = lambda: {"unexpected": "shape"}
    result = env.recover()
    assert any("could not enumerate open orders" in f for f in result.sweep_failures)
    assert not result.passed


# -- the shared cancel helper's double-failure path ---------------------------


def test_a_cancel_failure_that_cannot_be_recorded_still_raises_the_original(
    env, monkeypatch, caplog
):
    # Both halves fail: the exchange rejects the cancel AND the write recording
    # that rejection dies on a busy store. The ORIGINAL exchange error must
    # propagate — it is the diagnosis the caller puts verbatim into its audit
    # event and the sweep's failure list, and "database is locked" must never
    # become the permanent record of why a cancel failed. The attempt row is
    # left 'submitted' (outcome unknown), which the log must name.
    from contrib.hyperliquid_perp.live import cancel as cancel_mod

    env.register_order(order_id="o-e", hex_id=_HEX_ENTRY, logical="log-e", role="entry")
    env.client.cancel_results[_HEX_ENTRY] = ExchangeRequestError("exchange said no")

    def boom(conn, attempt_id, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cancel_mod.repo, "update_live_order_attempt", boom)

    with pytest.raises(ExchangeRequestError, match="exchange said no"):
        cancel_mod.cancel_bot_order_with_evidence(
            db=env.db,
            client=env.client,
            run_id="r",
            payload_dir=env.payload_dir,
            clock=env.clock,
            coin="BTC",
            cloid_hex=_HEX_ENTRY,
            cloid_logical="log-e",
            cancel_reason="stale_startup_cancel",
        )
    assert "stays 'submitted'" in caplog.text
