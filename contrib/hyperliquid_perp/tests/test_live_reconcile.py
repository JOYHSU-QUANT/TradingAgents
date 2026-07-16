"""Tests for the §12 live reconciliation sweep (fake exchange seams).

One test per §12.3 case row, plus the safe-mode application semantics of
``reconcile_and_apply`` and the snapshot/event recording contracts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.reconcile import LiveReconciler
from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import exchange_fill_key
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
_HEX = "0x" + "ab" * 16
_HEX_SL = "0x" + "cd" * 16


def _clearinghouse(
    *,
    account_value: str = "100",
    withdrawable: str = "100",
    margin_used: str = "0",
    maintenance: str = "0",
    positions: list[dict] | None = None,
) -> dict:
    return {
        "marginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": margin_used,
            "totalNtlPos": "0",
        },
        "withdrawable": withdrawable,
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


class _Seams:
    """The injectable exchange reads, each settable per test."""

    def __init__(self):
        self.open_orders: list | Exception = []
        self.clearinghouse: dict | Exception = _clearinghouse()
        self.order_status: dict[str, object] = {}
        self.fills: list | Exception = []

    def fetch_open_orders(self):
        if isinstance(self.open_orders, Exception):
            raise self.open_orders
        return self.open_orders

    def fetch_clearinghouse(self):
        if isinstance(self.clearinghouse, Exception):
            raise self.clearinghouse
        return self.clearinghouse

    def query_order_by_cloid(self, cloid_hex):
        result = self.order_status.get(cloid_hex, {"status": "unknownOid"})
        if isinstance(result, Exception):
            raise result
        return result

    def fetch_fills(self, start_ms, end_ms):
        if isinstance(self.fills, Exception):
            raise self.fills
        return self.fills


@pytest.fixture
def env(tmp_path):
    db = Database(":memory:")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW - timedelta(days=1),
    )
    seams = _Seams()
    reconciler = LiveReconciler(
        db=db,
        run_id="r",
        coin="BTC",
        fetch_open_orders=seams.fetch_open_orders,
        fetch_clearinghouse=seams.fetch_clearinghouse,
        query_order_by_cloid=seams.query_order_by_cloid,
        fetch_fills=seams.fetch_fills,
        payload_dir=tmp_path / "payloads",
        clock=ManualClock(_NOW),
    )
    yield db, seams, reconciler
    db.close()


def _gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
    )


def _insert_local_order(
    db,
    *,
    order_id="o1",
    hex_id=_HEX,
    logical="log-entry",
    status="open",
    role="entry",
    exchange_order_id=None,
):
    with db.transaction() as conn:
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
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("0.001"),
            status=status,
            cloid_logical=logical,
            cloid_hex=hex_id,
            exchange_order_id=exchange_order_id,
            is_bot_owned=True,
            timestamp=_NOW - timedelta(hours=1),
        )


def _register_cloid(db, *, hex_id, logical, role):
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical=logical,
            cloid_hex=hex_id,
            run_id="r",
            symbol="BTC",
            order_role=role,
        )


def _cases(db, case_type=None):
    return repo.iter_exchange_reconciliation_events(db.conn, "r", case_type=case_type)


# -- the clean pass -----------------------------------------------------------


def test_a_flat_matching_state_reconciles_clean_and_writes_an_ok_snapshot(env):
    db, seams, reconciler = env
    report = reconciler.run("heartbeat")
    assert report.clean
    assert report.cases == ()
    row = db.conn.execute(
        "SELECT * FROM account_snapshots WHERE run_id = 'r' ORDER BY snapshot_id DESC"
    ).fetchone()
    assert row is not None
    assert row["reconciliation_status"] == "ok"
    assert row["mode"] == "live"
    assert row["exchange_account_value"] == "100"


def test_an_unknown_trigger_word_is_rejected(env):
    _, _, reconciler = env
    with pytest.raises(ValueError, match="trigger"):
        reconciler.run("whenever")


# -- §12.3: SQLite has an order the exchange does not -----------------------


def test_a_stuck_order_with_no_evidence_is_settled_rejected(env):
    db, seams, reconciler = env
    _insert_local_order(db, status="submitted")
    report = reconciler.run("startup")
    assert report.orders_reconciled
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "rejected"
    assert order["status_reason"] == "send_never_reached_exchange"
    (case,) = _cases(db, "order_missing_on_exchange")
    assert case["action_taken"] == "settled_never_sent"


def test_a_missing_order_with_rule10_evidence_is_a_mismatch_never_a_resend(env):
    db, seams, reconciler = env
    # exchange_order_id non-NULL is the durable §8.3 rule-10 proof of receipt.
    _insert_local_order(db, exchange_order_id="55")
    report = reconciler.run("heartbeat")
    assert not report.orders_reconciled
    assert not report.clean
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "open"  # never guessed settled
    (case,) = _cases(db, "order_missing_on_exchange")
    assert case["action_taken"] is None


def test_a_missing_order_that_order_status_resolves_is_settled_from_the_answer(env):
    db, seams, reconciler = env
    _insert_local_order(db)
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 77}, "status": "filled"},
    }
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "filled"
    assert order["exchange_order_id"] == "77"


def test_an_order_still_live_per_order_status_is_not_a_case(env):
    db, seams, reconciler = env
    _insert_local_order(db)
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 77}, "status": "open"},
    }
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled
    assert _cases(db, "order_missing_on_exchange") == []


# -- §12.3: exchange has a bot-owned order SQLite does not -------------------


def test_an_orphan_bot_owned_order_gets_its_local_row_backfilled(env):
    db, seams, reconciler = env
    _register_cloid(db, hex_id=_HEX, logical="log-entry", role="entry")
    seams.open_orders = [
        {
            "oid": 42,
            "coin": "BTC",
            "cloid": _HEX,
            "side": "B",
            "sz": "0.001",
            "origSz": "0.002",
            "limitPx": "50000",
            "reduceOnly": False,
        }
    ]
    report = reconciler.run("startup")
    assert report.orders_reconciled
    row = repo.get_order_by_cloid_hex(db.conn, _HEX)
    assert row is not None
    assert row["status"] == "open"
    assert row["exchange_order_id"] == "42"
    assert row["qty"] == "0.002"
    assert row["filled_qty"] == "0.001"
    (case,) = _cases(db, "orphan_exchange_order")
    assert case["action_taken"] == "local_row_backfilled"


# -- §12.3: non-bot-owned order → manual ---------------------------------------


def test_a_non_bot_owned_order_is_a_manual_case(env):
    db, seams, reconciler = env
    seams.open_orders = [{"oid": 9, "coin": "BTC", "cloid": "0x" + "99" * 16, "sz": "1"}]
    report = reconciler.run("heartbeat")
    assert not report.clean
    assert [c.case_type for c in report.manual_cases] == ["non_bot_owned_order"]


def test_reconcile_and_apply_routes_a_non_bot_order_to_manual_safe_mode(env):
    db, seams, reconciler = env
    seams.open_orders = [{"oid": 9, "coin": "BTC", "cloid": None, "sz": "1"}]
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    reconciler.reconcile_and_apply("heartbeat", safe_mode=safe_mode)
    state = safe_mode.current()
    assert state is not None and state.is_manual
    assert gate.manual_safe_mode is True


# -- §12.3: position rows -------------------------------------------------------


def test_an_exchange_position_the_ledger_lacks_is_a_mismatch(env):
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position()], maintenance="1"
    )
    report = reconciler.run("heartbeat")
    assert not report.position_reconciled
    assert not report.position_protected  # no SL anywhere either
    types = {c.case_type for c in report.cases}
    assert "exchange_position_mismatch" in types
    assert "position_sl_missing" in types


def test_a_local_position_the_exchange_denies_is_a_phantom(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    report = reconciler.run("heartbeat")
    assert not report.position_reconciled
    assert [c.case_type for c in report.cases] == ["local_position_phantom"]
    # Never zeroed by the sweep: the books are corrected by booking fills, not edits.
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.size == Decimal("0.001")


def test_a_position_in_an_unexpected_coin_is_manual(env):
    db, seams, reconciler = env
    eth = dict(_btc_position(), coin="ETH")
    seams.clearinghouse = _clearinghouse(account_value="101", positions=[eth], maintenance="1")
    report = reconciler.run("heartbeat")
    assert any(c.manual for c in report.cases)


def test_a_matching_position_with_a_valid_sl_is_clean(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    # A local orders row exists for the SL, so it is no orphan.
    _insert_local_order(db, order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position()], maintenance="1"
    )
    seams.open_orders = [
        {
            "oid": 5,
            "coin": "BTC",
            "cloid": _HEX_SL,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    report = reconciler.run("heartbeat")
    assert report.position_reconciled
    assert report.position_protected
    assert report.clean
    row = db.conn.execute(
        "SELECT * FROM position_snapshots WHERE run_id = 'r' ORDER BY position_snapshot_id DESC"
    ).fetchone()
    assert row is not None
    assert row["reconciliation_status"] == "ok"
    assert row["exchange_position_size"] == "0.001"


def test_an_sl_covering_only_part_of_the_position_does_not_protect(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.002"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    _insert_local_order(db, order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position(szi="0.002", value="102")], maintenance="1"
    )
    seams.open_orders = [
        {
            "oid": 5,
            "coin": "BTC",
            "cloid": _HEX_SL,
            "side": "A",
            "sz": "0.001",
            "reduceOnly": True,
        }
    ]
    report = reconciler.run("heartbeat")
    assert not report.position_protected


# -- §12.3: equity tolerance -----------------------------------------------------


def test_equity_beyond_tolerance_is_a_mismatch(env):
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(account_value="95")  # ledger says 100
    report = reconciler.run("heartbeat")
    assert not report.account_reconciled
    (case,) = [c for c in report.cases if c.case_type == "equity_mismatch"]
    assert case.exchange_value == "95"


def test_equity_within_tolerance_is_clean(env):
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(account_value="99.50")  # inside max(1, 1%)
    report = reconciler.run("heartbeat")
    assert report.account_reconciled


# -- §12.3: fills ------------------------------------------------------------------


def _insert_booked_fill(db, *, tid="4521", fill_time):
    key = exchange_fill_key(tid=tid)
    _insert_local_order(
        db,
        order_id=f"o-fill-{tid}",
        hex_id="0x" + f"{int(tid):032x}",
        logical=f"log-{tid}",
        exchange_order_id="88",
        status="filled",
    )
    with db.transaction() as conn:
        repo.insert_live_fill(
            conn,
            fill_id=f"r|livefill|{key}",
            run_id="r",
            order_id=f"o-fill-{tid}",
            symbol="BTC",
            side="buy",
            fill_qty=Decimal("0.001"),
            fill_price=Decimal(50000),
            fill_notional=Decimal(50),
            exchange_fill_key=key,
            exchange_fill_time=fill_time,
            exchange_closed_pnl=Decimal(0),
            liquidity_role="taker",
            exchange_fee=Decimal("0.01"),
            exchange_fill_id=tid,
            exchange_order_id="88",
        )
    return key


def test_a_local_fill_the_exchange_denies_is_invalid_and_manual(env):
    db, seams, reconciler = env
    _insert_booked_fill(db, fill_time=_NOW - timedelta(hours=1))
    seams.fills = []  # the exchange's window has no such fill
    report = reconciler.run("heartbeat")
    assert not report.fills_reconciled
    (case,) = [c for c in report.cases if c.case_type == "invalid_local_fill"]
    assert case.manual


def test_a_local_fill_the_exchange_confirms_is_clean(env):
    db, seams, reconciler = env
    _insert_booked_fill(db, tid="4521", fill_time=_NOW - timedelta(hours=1))
    seams.fills = [{"tid": "4521"}]
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled


def test_a_fill_near_the_window_edge_is_not_judged(env):
    db, seams, reconciler = env
    _insert_booked_fill(db, fill_time=_NOW - timedelta(seconds=30))
    seams.fills = []
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled  # inside the edge margin: no verdict


def test_an_unbooked_unmapped_sighting_blocks_the_fills_leg(env):
    db, seams, reconciler = env
    key = exchange_fill_key(tid="777")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_unmapped",
            exchange_value=key,
        )
    report = reconciler.run("heartbeat")
    assert not report.fills_reconciled
    # Booking the fill resolves it with no row edits: the anti-join stops hitting.
    _insert_booked_fill(db, tid="777", fill_time=_NOW - timedelta(hours=1))
    seams.fills = [{"tid": "777"}]
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled


def test_a_malformed_sighting_whose_tid_was_booked_is_swept_resolved(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_malformed",
            exchange_value="4521",  # the bare-tid malformed key
        )
    _insert_booked_fill(db, tid="4521", fill_time=_NOW - timedelta(hours=1))
    seams.fills = [{"tid": "4521"}]
    report = reconciler.run("heartbeat")
    assert report.clean
    (row,) = _cases(db, "fill_malformed")
    assert row["action_taken"] == "resolved_fill_booked"


def test_a_digest_keyed_malformed_sighting_stays_open_but_does_not_block(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_malformed",
            exchange_value="unparsed-deadbeef00000000",
        )
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled
    (row,) = _cases(db, "fill_malformed")
    assert row["action_taken"] is None


# -- degraded reads ---------------------------------------------------------------


def test_a_failed_exchange_read_is_an_unclean_verdict_not_a_crash(env):
    db, seams, reconciler = env
    seams.open_orders = RuntimeError("api down")
    seams.clearinghouse = RuntimeError("api down")
    report = reconciler.run("heartbeat")
    assert not report.clean
    assert not report.orders_reconciled
    assert not report.account_reconciled
    assert not report.position_reconciled
    assert report.errors


# -- reconcile_and_apply safe-mode wiring -------------------------------------------


def test_a_mismatch_enters_recoverable_safe_mode_and_a_clean_pass_recovers_it(env):
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))

    seams.clearinghouse = _clearinghouse(account_value="90")  # equity mismatch
    report = reconciler.reconcile_and_apply("heartbeat", safe_mode=safe_mode)
    assert not report.clean
    state = safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"
    assert gate.state_reconciled is False

    seams.clearinghouse = _clearinghouse(account_value="100")  # healed
    report = reconciler.reconcile_and_apply("heartbeat", safe_mode=safe_mode)
    assert report.clean
    assert safe_mode.current() is None
    assert gate.state_reconciled is True


def test_a_clean_pass_with_no_safe_mode_reproves_the_gate(env):
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    reconciler.reconcile_and_apply("heartbeat", safe_mode=safe_mode)
    assert gate.state_reconciled is True
    assert safe_mode.current() is None


def test_case_rows_are_deduped_once_per_fact_across_passes(env):
    db, seams, reconciler = env
    seams.open_orders = [{"oid": 9, "coin": "BTC", "cloid": None, "sz": "1"}]
    reconciler.run("heartbeat")
    reconciler.run("heartbeat")
    assert len(_cases(db, "non_bot_owned_order")) == 1


def test_a_retry_that_resolves_a_deduped_case_stamps_the_existing_row(env):
    db, seams, reconciler = env
    _insert_local_order(db)
    # Pass 1: orderStatus read fails → case recorded, unresolved (action NULL).
    seams.order_status[_HEX] = RuntimeError("api down")
    reconciler.run("heartbeat")
    (row,) = _cases(db, "order_missing_on_exchange")
    assert row["action_taken"] is None
    # Pass 2 settles the same order; the dedupe swallows the fresh insert, so
    # the disposition must land on the EXISTING row instead of being lost.
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 77}, "status": "canceled"},
    }
    reconciler.run("heartbeat")
    (row,) = _cases(db, "order_missing_on_exchange")
    assert row["action_taken"] == "settled_canceled"


def test_an_exchange_open_order_with_a_terminal_local_row_is_reopened(env):
    db, seams, reconciler = env
    _insert_local_order(db, status="rejected")
    seams.open_orders = [
        {"oid": 42, "coin": "BTC", "cloid": _HEX, "side": "B", "sz": "0.001", "origSz": "0.001"}
    ]
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled  # resolved in-pass, per §12.1 the exchange wins
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "open"
    assert order["status_reason"] == "reopened_from_exchange_reconciliation"
    assert order["exchange_order_id"] == "42"
    (case,) = _cases(db, "orphan_exchange_order")
    assert case["action_taken"] == "local_row_reopened"


def test_a_wrong_side_sl_does_not_count_as_protection(env):
    db, seams, reconciler = env
    # Long position; the resting SL is a BUY (it closed a long-gone short).
    _insert_local_order(db, order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position()], maintenance="1"
    )
    seams.open_orders = [
        {"oid": 5, "coin": "BTC", "cloid": _HEX_SL, "side": "B", "sz": "0.001", "reduceOnly": True}
    ]
    report = reconciler.run("heartbeat")
    assert not report.position_protected


def test_a_capped_fill_window_withholds_invalid_fill_verdicts(env):
    from contrib.hyperliquid_perp.live.fill_backfill import RESPONSE_FILL_CAP

    db, seams, reconciler = env
    _insert_booked_fill(db, fill_time=_NOW - timedelta(hours=1))
    # A cap-length response whose newest time never advances: the window can
    # never be proven covered — no manual verdict may be issued from it.
    seams.fills = [
        {"tid": str(1000 + i), "time": int((_NOW - timedelta(hours=5)).timestamp() * 1000)}
        for i in range(RESPONSE_FILL_CAP)
    ]
    report = reconciler.run("heartbeat")
    assert not report.fills_reconciled  # inconclusive → fail-safe unclean
    assert not any(c.case_type == "invalid_local_fill" for c in report.cases)
    assert any("not covered" in e for e in report.errors)


class _StubBackfiller:
    """Records the ``since`` each pass was asked to cover; books nothing."""

    def __init__(self):
        self.calls = []

    def backfill(self, now=None, *, since=None):
        from contrib.hyperliquid_perp.live.fill_backfill import BackfillSummary

        self.calls.append(since)
        return BackfillSummary(fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0)


def test_startup_backfill_floor_reaches_back_to_the_newest_booked_fill(env):
    db, seams, reconciler = env
    stub = _StubBackfiller()
    reconciler._backfiller = stub  # no stream: the startup-command wiring
    fill_time = _NOW - timedelta(days=3)  # far outside the 6h trailing lookback
    _insert_booked_fill(db, fill_time=fill_time)
    seams.fills = [{"tid": "4521"}]
    reconciler.run("startup")
    assert stub.calls == [fill_time]


def test_startup_backfill_floor_falls_back_to_run_genesis_with_no_fills(env):
    db, seams, reconciler = env
    stub = _StubBackfiller()
    reconciler._backfiller = stub
    reconciler.run("startup")
    (since,) = stub.calls
    assert since == _NOW - timedelta(days=1)  # the run's genesis timestamp


def test_an_sl_only_mismatch_enters_safe_mode_under_its_own_reason(env):
    from contrib.hyperliquid_perp.live.safe_mode import REASON_SL_MISSING

    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position()], maintenance="1"
    )
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    report = reconciler.reconcile_and_apply("heartbeat", safe_mode=safe_mode)
    assert not report.position_protected
    state = safe_mode.current()
    assert state is not None and state.reason == REASON_SL_MISSING


def test_a_case_cannot_be_both_manual_and_resolved(env):
    from contrib.hyperliquid_perp.live.reconcile import ReconciliationCase

    with pytest.raises(ValueError, match="manual and resolved"):
        ReconciliationCase(
            case_type="invalid_local_fill",
            symbol="BTC",
            local_value=None,
            exchange_value="x",
            manual=True,
            resolved=True,
        )
