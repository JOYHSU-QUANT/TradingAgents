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


def _btc_position(
    szi: str = "0.001",
    upnl: str = "1",
    value: str = "51",
    liquidation_px: str | None = None,
) -> dict:
    return {
        "coin": "BTC",
        "szi": szi,
        "entryPx": "50000",
        "unrealizedPnl": upnl,
        "positionValue": value,
        "marginUsed": value,
        # Absent by default (the mapper treats a None liquidationPx as absent),
        # so the mirror tests are the only ones that carry an estimate.
        "liquidationPx": liquidation_px,
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


def test_the_sweep_refreshes_the_kill_switch_across_its_exchange_reads(env, tmp_path):
    """§18.2: a sweep is the longest wall of REST traffic on the single-threaded
    live tick — two account reads, a paged backfill, and an orderStatus
    round-trip per order in two loops whose length the exchange and the store
    decide, not config. The tick's own refresh is already spent by the time any
    of it runs (2026-07-31 deadline review).

    Three refreshes with an empty book: one between the account reads, one
    after, and one for the fill cross-check's single (uncapped) page.
    """
    db, seams, _ = env
    refreshes: list[int] = []
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
        refresh_kill_switch=lambda: refreshes.append(1),
    )
    reconciler.run("heartbeat")
    assert len(refreshes) == 3


def test_the_sweep_refreshes_once_per_open_order_not_once_per_leg(env, tmp_path):
    """The per-order half of the guarantee above: the orderStatus round-trips in
    the open-orders loop are as many as the exchange says, so the refresh has to
    ride the loop, not the leg.

    Uses malformed entries deliberately — the refresh sits at the top of the
    loop body, so this isolates "one refresh per entry" from any registry or
    order-row setup. Two entries → the three baseline refreshes of the test
    above plus two more.
    """
    db, seams, _ = env
    seams.open_orders = ["junk", "junk"]
    refreshes: list[int] = []
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
        refresh_kill_switch=lambda: refreshes.append(1),
    )
    reconciler.run("heartbeat")
    assert len(refreshes) == 5


def test_the_fill_cross_check_ladder_refreshes_between_pages(env, tmp_path, monkeypatch):
    """§18.2: the reconciler has its OWN page ladder for the fill cross-check,
    separate from the backfiller's and easy to miss because it is inline.

    The first pass of the deadline review wired only the backfiller's and left
    this one untouched, which made the real worst case DEFAULT_MAX_PAGES deep
    while ``_MAX_UNREFRESHED_REST_CALLS`` still claimed 3 — an advisory
    promising headroom it could not deliver. One refresh per page.
    """
    from contrib.hyperliquid_perp.live import reconcile as reconcile_mod

    db, seams, _ = env
    monkeypatch.setattr(reconcile_mod, "RESPONSE_FILL_CAP", 1)  # every page "capped"
    monkeypatch.setattr(reconcile_mod, "DEFAULT_MAX_PAGES", 3)
    refreshes: list[int] = []
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
        refresh_kill_switch=lambda: refreshes.append(1),
    )
    window_start = _NOW - timedelta(hours=1)
    base = int(window_start.timestamp() * 1000)
    stamps = iter([base + 1, base + 2, base + 3])

    def fetch_fills(start_ms, end_ms):
        t = next(stamps)
        return [{"tid": t, "time": t}]

    errors: list[str] = []
    keys = reconciler._fetch_window_fill_keys(fetch_fills, window_start, _NOW, errors)
    assert keys is None  # budget exhausted, window unproven — the paging really ran
    assert len(refreshes) == 3  # one per page, not one per leg


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
    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
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


def test_the_exchange_liquidation_estimate_is_mirrored_onto_the_local_row(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        positions=[_btc_position(liquidation_px="43210.5")],
        maintenance="1",
    )
    reconciler.run("heartbeat")
    # §12.1: the exchange is the truth source for the liq estimate, and this row
    # is how it reaches the engine's SL band (§3.6/§17.2) — nothing else in the
    # live path reads the clearinghouse for it.
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.liquidation_price == Decimal("43210.5")


def test_a_direction_disagreement_withholds_the_liquidation_mirror(env):
    # The flip fill landed between this pass's clearinghouse read and its fill
    # backfill, so the exchange still shows the PRE-flip side. Mirroring that
    # estimate onto the freshly flipped row hands the SL band a liquidation
    # price on the wrong side of entry, which stops.py answers CLOSE_NOW to —
    # a §17.2 emergency close of a healthy position. Withhold instead.
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("-0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        # The exchange still reports the LONG the local books already flipped.
        positions=[_btc_position(szi="0.001", liquidation_px="43210.5")],
        maintenance="1",
    )
    reconciler.run("heartbeat")
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.liquidation_price is None
    # The size disagreement is still reported through its own lane — withholding
    # the estimate must not also swallow the mismatch.
    assert {r["exchange_value"] for r in _cases(db, "exchange_position_mismatch")} == {
        "BTC:-0.001->0.001"
    }


def test_an_agreeing_direction_still_mirrors_after_a_flip(env):
    # Negative control for the test above: once both views agree on the short,
    # the short's own estimate (ABOVE entry) is mirrored normally.
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("-0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        positions=[_btc_position(szi="-0.001", liquidation_px="56789.5")],
        maintenance="1",
    )
    reconciler.run("heartbeat")
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.liquidation_price == Decimal("56789.5")


def test_a_failed_liquidation_mirror_does_not_fail_the_position_leg(env, monkeypatch):
    """The mirror is a cache for the SL band, not this leg's evidence.

    The leg's verdict answers "do the local and exchange positions agree?", and
    the read already answered it. A transient store error on the metadata write
    must not retroactively mark the position unreconciled AND unprotected —
    that drives safe mode (halted cycles, a manual §13.6 release) off a cache
    failure (decision 2026-07-29).
    """
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    _insert_local_order(db, order_id="o-sl", hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        positions=[_btc_position(liquidation_px="43210.5")],
        maintenance="1",
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

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "set_position_liquidation_price", _boom)
    report = reconciler.run("heartbeat")

    assert report.position_reconciled
    assert report.position_protected
    assert report.clean
    # Unmirrored this tick: the engine falls back to the entry-based band, the
    # same band it used before the mirror existed. The next pass rewrites it.
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.liquidation_price is None


def test_a_flat_exchange_clears_a_stale_mirrored_liquidation_estimate(env):
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))
    # The default clearinghouse holds no position at all.
    report = reconciler.run("heartbeat")
    # The clear does NOT wait for a clean pass: the local books still claim a
    # position (a phantom), and that is exactly when a surviving estimate would
    # be most misleading.
    assert not report.position_reconciled
    pos = repo.get_current_position(db.conn, "r", "BTC")
    assert pos is not None and pos.liquidation_price is None


# -- §12.3: equity tolerance -----------------------------------------------------


def test_equity_beyond_tolerance_is_a_mismatch(env):
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(account_value="95")  # ledger says 100
    report = reconciler.run("heartbeat")
    assert not report.account_reconciled
    (case,) = [c for c in report.cases if c.case_type == "equity_mismatch"]
    # The fact key is a CONSTANT (the observed account value drifts with mark
    # prices — a drifting key would defeat the once-per-fact dedupe and write
    # one audit row per pass); the live magnitudes live in the detail.
    assert case.exchange_value == "equity_out_of_tolerance"
    assert "95" in (case.detail or "")


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
    # The flip to unclean must NAME itself in report.errors: that channel is what
    # the persisted reconciliation_diff and the safe-mode detail are built from,
    # so a bare log line would fire safe mode with no cause on any
    # operator-facing surface.
    assert any("unmapped fill sighting" in e for e in report.errors)
    # Booking the fill resolves it with no row edits: the anti-join stops hitting.
    _insert_booked_fill(db, tid="777", fill_time=_NOW - timedelta(hours=1))
    seams.fills = [{"tid": "777"}]
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled
    assert not report.errors


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


def test_a_digest_keyed_malformed_sighting_blocks_until_a_human_stamps_it(env):
    # Decided 2026-07-17: an un-actioned malformed sighting (the exchange
    # reported a fill SQLite never booked) BLOCKS the verdict — unbooked money a
    # human must stamp, not an audit backlog the pass may read past. The reason
    # reaches report.errors (→ safe-mode detail, persisted reconciliation_diff).
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
    assert not report.fills_reconciled
    assert any("malformed fill sighting" in e for e in report.errors)
    (row,) = _cases(db, "fill_malformed")
    assert row["action_taken"] is None
    # A human stamping action_taken clears the block on the next pass.
    with db.transaction() as conn:
        repo.set_reconciliation_action(conn, row["event_id"], "resolved_manual")
    report = reconciler.run("heartbeat")
    assert report.fills_reconciled


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
    # §13.4 auto-release demands a FULLY wired reconciler (no legs_skipped):
    # bind the backfill seam the shared fixture leaves out.
    reconciler._backfiller = _StubBackfiller()

    seams.clearinghouse = _clearinghouse(account_value="90")  # equity mismatch
    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert not report.clean
    state = safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"
    assert gate.state_reconciled is False

    seams.clearinghouse = _clearinghouse(account_value="100")  # healed
    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert report.clean
    assert safe_mode.current() is None
    assert gate.state_reconciled is True


def test_a_clean_pass_with_no_safe_mode_reproves_the_gate(env):
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
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
    # orderStatus is the tiebreaker: it must CONFIRM the order live before the
    # terminal row is reopened.
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 42}, "status": "open"},
    }
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled  # resolved in-pass, per §12.1 the exchange wins
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "open"
    assert order["status_reason"] == "reopened_from_exchange_reconciliation"
    assert order["exchange_order_id"] == "42"
    assert order["exchange_status"] == "open"
    assert order["exchange_raw_status"] == "open"
    (case,) = _cases(db, "orphan_exchange_order")
    assert case["action_taken"] == "local_row_reopened"


def test_a_reopened_row_persists_the_mapped_exchange_status_not_a_literal_open(env):
    # The reopen persists exchange_status = the MAPPED local status, never a
    # hardcoded "open" (the idiom that let the protection manager misrecord a
    # filled recovery as live). Today every live mapping lands on "open", so
    # this pins the wiring through a raw word that is NOT literally "open":
    # "triggered" must map to family "open" while exchange_raw_status keeps
    # the verbatim exchange word.
    db, seams, reconciler = env
    _insert_local_order(db, status="rejected")
    seams.open_orders = [
        {"oid": 43, "coin": "BTC", "cloid": _HEX, "side": "B", "sz": "0.001", "origSz": "0.001"}
    ]
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 43}, "status": "triggered"},
    }
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "open"
    assert order["exchange_status"] == "open"  # the mapped family
    assert order["exchange_raw_status"] == "triggered"  # the verbatim word


def test_a_stale_open_orders_listing_of_a_cancelled_order_is_not_reopened(env):
    # The run just cancelled the order (row terminal); open_orders is merely
    # behind, and orderStatus confirms the cancel — reopening would resurrect
    # a phantom-open row for an order the run itself retired.
    db, seams, reconciler = env
    _insert_local_order(db, status="canceled")
    seams.open_orders = [
        {"oid": 42, "coin": "BTC", "cloid": _HEX, "side": "B", "sz": "0.001", "origSz": "0.001"}
    ]
    seams.order_status[_HEX] = {
        "status": "order",
        "order": {"order": {"oid": 42}, "status": "canceled"},
    }
    report = reconciler.run("heartbeat")
    assert report.orders_reconciled
    assert repo.get_order(db.conn, "o1")["status"] == "canceled"  # untouched
    assert _cases(db, "orphan_exchange_order") == []  # not a conflict, no case


def test_contradictory_exchange_answers_on_a_terminal_row_stay_unproven(env):
    # open_orders lists it, orderStatus denies it: never guessed either way.
    db, seams, reconciler = env
    _insert_local_order(db, status="rejected")
    seams.open_orders = [
        {"oid": 42, "coin": "BTC", "cloid": _HEX, "side": "B", "sz": "0.001", "origSz": "0.001"}
    ]
    report = reconciler.run("heartbeat")  # order_status defaults to unknownOid
    assert not report.orders_reconciled
    assert repo.get_order(db.conn, "o1")["status"] == "rejected"  # untouched
    (case,) = _cases(db, "orphan_exchange_order")
    assert case["action_taken"] is None


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
    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert not report.position_protected
    state = safe_mode.current()
    assert state is not None and state.reason == REASON_SL_MISSING


def test_a_failed_snapshot_write_keeps_the_case_rows_and_the_verdict(env, monkeypatch):
    # The case rows are the durable evidence of WHY safe mode fired; a snapshot
    # write failing in the same unit of work must not roll them back, and the
    # pass must still return its verdict ("records everything, raises nothing"
    # includes the recording leg itself).
    db, seams, reconciler = env
    seams.open_orders = [{"oid": 9, "coin": "BTC", "cloid": None, "sz": "1"}]  # manual case

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(LiveReconciler, "_write_snapshots", boom)
    report = reconciler.run("heartbeat")  # must not raise
    assert not report.clean
    assert report.manual_cases  # the in-memory verdict survives
    (case,) = _cases(db, "non_bot_owned_order")  # the durable evidence survives
    assert case["case_type"] == "non_bot_owned_order"
    rows = db.conn.execute("SELECT COUNT(*) FROM account_snapshots WHERE run_id='r'").fetchone()
    assert rows[0] == 0  # only the snapshot leg was lost


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


# -- round-2 hardening (2026-07-17 review loop) ---------------------------------


def test_both_nonzero_but_different_sizes_are_a_mismatch(env):
    # A missed partial fill's exact shape: both sides hold a position, the
    # sizes disagree. Shares a case_type with the "local flat" branch, so only
    # this test proves the elif is actually reachable.
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position(szi="0.003", value="153")], maintenance="1"
    )
    report = reconciler.run("heartbeat")
    assert not report.position_reconciled
    (case,) = [c for c in report.cases if c.case_type == "exchange_position_mismatch"]
    assert case.local_value == "0.001"
    # The once-per-fact dedupe key encodes the coin and the (local → exchange)
    # transition so distinct facts never collide on a bare size.
    assert case.exchange_value == "BTC:0.001->0.003"
    assert "sizes differ" in (case.detail or "")


def test_snapshot_rows_are_skipped_when_maintenance_margin_is_unreported(env, caplog):
    # "Never fabricate": a clearinghouse payload without
    # crossMaintenanceMarginUsed must skip the snapshot rows (warning), not
    # write a guessed 0 into the audit trail.
    db, seams, reconciler = env
    ch = _clearinghouse()
    del ch["crossMaintenanceMarginUsed"]
    seams.clearinghouse = ch
    with caplog.at_level("WARNING"):
        reconciler.run("heartbeat")
    row = db.conn.execute("SELECT COUNT(*) FROM account_snapshots WHERE run_id='r'").fetchone()
    assert row[0] == 0
    assert any("crossMaintenanceMarginUsed" in r.getMessage() for r in caplog.records)


def test_snapshot_rows_are_skipped_when_a_position_has_no_value(env, caplog):
    # An unpriced position cannot be summed into total notional; writing the
    # partial total would understate exposure — skip, never guess.
    db, seams, reconciler = env
    pos = _btc_position()
    del pos["positionValue"]
    ch = _clearinghouse(account_value="101", positions=[pos], maintenance="1")
    del ch["marginSummary"]["totalNtlPos"]
    seams.clearinghouse = ch
    with caplog.at_level("WARNING"):
        reconciler.run("heartbeat")
    row = db.conn.execute("SELECT COUNT(*) FROM account_snapshots WHERE run_id='r'").fetchone()
    assert row[0] == 0
    assert any("positionValue" in r.getMessage() for r in caplog.records)


def test_an_unfillable_orphan_stays_an_unresolved_mismatch(env):
    # The backfill raising (unrecognised side) must flow through to an
    # UNRESOLVED case and an unclean orders leg — an orphan the pass could not
    # adopt is still a live local/exchange conflict.
    db, seams, reconciler = env
    _register_cloid(db, hex_id=_HEX, logical="log-entry", role="entry")
    seams.open_orders = [
        {"oid": 42, "coin": "BTC", "cloid": _HEX, "side": "X", "sz": "0.001", "origSz": "0.002"}
    ]
    report = reconciler.run("startup")
    assert not report.orders_reconciled
    (case,) = _cases(db, "orphan_exchange_order")
    assert case["action_taken"] is None
    assert repo.get_order_by_cloid_hex(db.conn, _HEX) is None  # nothing half-written


def test_a_reopen_tiebreaker_read_failure_leaves_the_row_untouched(env):
    # Terminal local row, open_orders lists it, and the orderStatus tiebreaker
    # read itself fails: unproven either way — never reopened, leg unclean.
    db, seams, reconciler = env
    _insert_local_order(db, status="rejected")
    seams.open_orders = [
        {"oid": 42, "coin": "BTC", "cloid": _HEX, "side": "B", "sz": "0.001", "origSz": "0.001"}
    ]
    seams.order_status[_HEX] = RuntimeError("api down")
    report = reconciler.run("heartbeat")
    assert not report.orders_reconciled
    assert repo.get_order(db.conn, "o1")["status"] == "rejected"  # untouched


def test_a_crashed_leg_is_an_unclean_verdict_not_a_crash(env, monkeypatch):
    # run()'s "records everything, raises nothing" covers the legs' own DB
    # work too: a raise inside a leg maps to the fail-safe unproven verdict.
    db, seams, reconciler = env

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(LiveReconciler, "_reconcile_orders", boom)
    report = reconciler.run("heartbeat")  # must not raise
    assert not report.clean
    assert not report.orders_reconciled
    assert any("orders leg crashed" in e for e in report.errors)


def test_sl_absence_is_not_asserted_off_a_failed_open_orders_read(env):
    # unknown ≠ unprotected-as-a-fact: the verdict stays fail-safe
    # (protected=False) but no durable row may claim "no valid SL" when the
    # truth is "could not look".
    db, seams, reconciler = env
    seams.open_orders = RuntimeError("api down")
    seams.clearinghouse = _clearinghouse(
        account_value="101", positions=[_btc_position()], maintenance="1"
    )
    report = reconciler.run("heartbeat")
    assert not report.position_protected
    assert _cases(db, "position_sl_missing") == []


def test_a_clean_pass_does_not_reopen_the_gate_while_release_conditions_are_unmet(env):
    # try_auto_recover declining because the §13.4 conditions are unmet must
    # NOT fall through to set_state_reconciled(True): for recoverable safe
    # mode that flag is the gate's only blocking line.
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    # Fully wired (no legs_skipped): this test's subject is the §13.4
    # attestation conditions, not the wiring gate.
    reconciler._backfiller = _StubBackfiller()
    seams.clearinghouse = _clearinghouse(account_value="90")  # mismatch → recoverable
    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert safe_mode.current() is not None

    seams.clearinghouse = _clearinghouse(account_value="100")  # healed...
    report = reconciler.reconcile_and_apply(
        "heartbeat",
        safe_mode=safe_mode,
        ws_restored=True,
        kill_switch_active=False,  # ...but KS unhealthy
    )
    assert report.clean
    state = safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"  # not released
    assert gate.state_reconciled is False  # trading must NOT resume

    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert report.clean
    assert safe_mode.current() is None  # every §13.4 condition proven → released
    assert gate.state_reconciled is True


def test_manual_reason_mapping_reaches_the_safe_mode_record(env):
    # The case_type → REASON_* vocabulary (_MANUAL_CASE_REASONS): a manual case
    # missing there now fails loud at construction, and the safe-mode record
    # must carry the specific reason end to end.
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    eth = {
        "coin": "ETH",
        "szi": "1",
        "entryPx": "3000",
        "unrealizedPnl": "1",
        "positionValue": "3000",
        "marginUsed": "300",
    }
    seams.clearinghouse = _clearinghouse(account_value="101", positions=[eth], maintenance="1")
    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    state = safe_mode.current()
    assert state is not None and state.is_manual
    assert state.reason == "unknown_exchange_position"


def test_a_missing_genesis_floor_degradation_is_logged(env, caplog):
    # The backfill floor silently degrading from "since genesis" to the bare
    # trailing lookback is money-relevant (an outage longer than 6h loses
    # fills) — it must leave a trace.
    db, seams, reconciler = env
    stub = _StubBackfiller()
    reconciler._backfiller = stub
    with db.transaction() as conn:
        conn.execute("UPDATE runs SET created_at = 'not-a-timestamp' WHERE run_id = 'r'")
    with caplog.at_level("WARNING"):
        reconciler.run("heartbeat")
    assert stub.calls == [None]  # the floor really did degrade
    assert any("degrades to the trailing 6h lookback" in r.getMessage() for r in caplog.records)


def test_an_orphan_protection_order_backfills_with_its_role_type(env):
    # The registry role is the durable record of what the bot placed: an
    # orphaned SL must not be backfilled as "ioc_limit" (a permanent audit
    # mislabel once PR 5 places trigger orders).
    db, seams, reconciler = env
    _register_cloid(db, hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    seams.open_orders = [
        {
            "oid": 43,
            "coin": "BTC",
            "cloid": _HEX_SL,
            "side": "A",
            "sz": "0.001",
            "origSz": "0.001",
            "limitPx": "45000",
            "reduceOnly": True,
        }
    ]
    reconciler.run("startup")
    row = repo.get_order_by_cloid_hex(db.conn, _HEX_SL)
    assert row is not None
    assert row["type"] == "stop_market"
    assert row["exchange_raw_status"] == "open"


def test_an_unknown_case_type_fails_loud_at_construction(env):
    from contrib.hyperliquid_perp.live.reconcile import ReconciliationCase

    with pytest.raises(ValueError, match="case_type"):
        ReconciliationCase(
            case_type="tpyo_case", symbol="BTC", local_value=None, exchange_value="x"
        )


def test_an_action_taken_without_resolved_fails_loud_at_construction(env):
    from contrib.hyperliquid_perp.live.reconcile import ReconciliationCase

    with pytest.raises(ValueError, match="must be resolved"):
        ReconciliationCase(
            case_type="order_missing_on_exchange",
            symbol="BTC",
            local_value="o1",
            exchange_value="x",
            action_taken="settled_canceled",
            resolved=False,
        )


# -- round-3 hardening (2026-07-17 review loop) ---------------------------------


def test_a_manual_case_without_a_reason_mapping_fails_loud_at_construction(env):
    # _MANUAL_CASE_REASONS is the ONE manual → safe-mode-reason vocabulary; a
    # manual case_type missing there would silently enter safe mode under the
    # generic mismatch reason.
    from contrib.hyperliquid_perp.live.reconcile import ReconciliationCase

    with pytest.raises(ValueError, match="_MANUAL_CASE_REASONS"):
        ReconciliationCase(
            case_type="order_missing_on_exchange",  # valid type, but never manual
            symbol="BTC",
            local_value="o1",
            exchange_value="x",
            manual=True,
        )


def test_a_tidless_window_entry_withholds_the_invalid_fill_verdict(env):
    # An entry the cross-check cannot key is an entry the membership verdict
    # cannot see — the window must count as NOT covered (withhold), or a
    # genuinely booked local fill gets flagged invalid_local_fill (MANUAL
    # safe mode) on a false premise.
    db, seams, reconciler = env
    _insert_booked_fill(db, fill_time=_NOW - timedelta(hours=1))
    seams.fills = [{"time": 1}]  # the same window read came back tid-less
    report = reconciler.run("heartbeat")
    assert not report.fills_reconciled  # inconclusive, not clean
    assert not [c for c in report.cases if c.case_type == "invalid_local_fill"]
    assert any("no usable tid" in e for e in report.errors)


def test_one_case_rows_write_failure_does_not_lose_its_siblings(env, monkeypatch):
    # Each case row is an independent fact in its own transaction: the manual
    # row a human most needs must survive an unrelated row's write failure.
    db, seams, reconciler = env
    seams.open_orders = [
        {"oid": 9, "coin": "BTC", "cloid": None, "sz": "1"},  # non_bot_owned_order
    ]
    seams.clearinghouse = _clearinghouse(account_value="90")  # + equity_mismatch
    real_insert = repo.insert_exchange_reconciliation_event

    def sometimes_boom(conn, **kwargs):
        if kwargs.get("case_type") == "equity_mismatch":
            raise RuntimeError("disk full")
        return real_insert(conn, **kwargs)

    monkeypatch.setattr(
        "contrib.hyperliquid_perp.live.reconcile.repo.insert_exchange_reconciliation_event",
        sometimes_boom,
    )
    report = reconciler.run("heartbeat")  # must not raise
    assert not report.clean
    (case,) = _cases(db, "non_bot_owned_order")  # the sibling fact landed
    assert case["case_type"] == "non_bot_owned_order"
    assert not _cases(db, "equity_mismatch")  # only the failed row was lost


def test_a_persistent_equity_mismatch_writes_one_audit_row_across_passes(env):
    # The fact key is constant: the drifting account value must not defeat the
    # once-per-fact dedupe and write one row per pass on the heartbeat cadence.
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(account_value="90")
    reconciler.run("heartbeat")
    seams.clearinghouse = _clearinghouse(account_value="89")  # mark drifted
    reconciler.run("heartbeat")
    (row,) = _cases(db, "equity_mismatch")
    assert row["exchange_value"] == "equity_out_of_tolerance"


# -- round-4 hardening: wiring gate, short positions, guarded legs, paging ----


def test_a_clean_pass_with_skipped_legs_never_auto_releases(env):
    # §13.4's "reconciliation clean" means the FULL sweep: the shared fixture
    # has no backfiller, so its clean verdicts carry legs_skipped and must
    # hold — never lift — a latched recoverable safe mode (decided
    # 2026-07-17; the tempting PR 5 shape is a cheap heartbeat wiring
    # without the fill seams).
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    seams.clearinghouse = _clearinghouse(account_value="90")  # mismatch → recoverable
    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert safe_mode.current() is not None

    seams.clearinghouse = _clearinghouse(account_value="100")  # healed...
    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert report.clean
    assert report.legs_skipped == ("fill_backfill",)
    state = safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"  # NOT released
    assert gate.state_reconciled is False  # the internal refusal held the line


def test_a_reconciler_without_fill_seams_names_both_skipped_legs():
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
        clock=ManualClock(_NOW),
    )
    report = reconciler.run("heartbeat")
    assert report.clean  # run()'s verdict still speaks for the legs it ran
    assert report.legs_skipped == ("fill_backfill", "invalid_local_fill_crosscheck")
    db.close()


def test_a_fully_wired_clean_pass_reports_no_skipped_legs(env):
    db, seams, reconciler = env
    reconciler._backfiller = _StubBackfiller()
    report = reconciler.run("heartbeat")
    assert report.clean
    assert report.legs_skipped == ()


def test_short_position_sl_coverage_counts_only_buy_side_stops(env):
    # The closing side of a SHORT is the bid ("B"): a buy-side reduce-only SL
    # protects it, and a sell-side one (the long direction's stop) must not.
    # Every prior test used long positions — a sign flip in the short branch
    # of hl_closing_side would otherwise pass the suite silently.
    db, seams, reconciler = env
    _register_cloid(db, hex_id=_HEX_SL, logical="log-sl", role="stop_loss")
    short = _btc_position(szi="-0.001")
    seams.clearinghouse = _clearinghouse(account_value="101", positions=[short], maintenance="1")
    seams.open_orders = [
        {
            "oid": 7,
            "coin": "BTC",
            "cloid": _HEX_SL,
            "side": "B",
            "sz": "0.001",
            "reduceOnly": True,
            "isTrigger": True,
            "triggerPx": "60000",
        }
    ]
    report = reconciler.run("heartbeat")
    assert report.position_protected

    # The same order on the ask side rests in the SHORT-ADDING direction: it
    # can never close the short, so coverage must read zero.
    seams.open_orders[0]["side"] = "A"
    report = reconciler.run("heartbeat")
    assert not report.position_protected
    assert [c.case_type for c in report.cases if c.case_type == "position_sl_missing"]


@pytest.mark.parametrize(
    "leg",
    ["_run_fill_backfill", "_reconcile_fills", "_reconcile_positions", "_reconcile_account"],
)
def test_every_guarded_leg_maps_a_crash_to_an_unclean_verdict(env, monkeypatch, leg):
    # run()'s "records everything, raises nothing" is enforced per leg by the
    # guarded() wrapper — prove it for each wrapped site, not just orders
    # (a future edit dropping one wrapper must fail a test, §18.2 callers
    # rely on run() never raising).
    db, seams, reconciler = env

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(LiveReconciler, leg, boom)
    report = reconciler.run("heartbeat")  # must not raise
    assert not report.clean
    assert any("leg crashed" in e for e in report.errors)


def test_fill_crosscheck_accumulates_keys_across_pages(env):
    # A capped first page must not evict its keys when page two completes the
    # window: a genuinely booked fill seen only on page one would otherwise be
    # flagged invalid_local_fill — a false MANUAL safe-mode verdict.
    from contrib.hyperliquid_perp.live.fill_backfill import RESPONSE_FILL_CAP

    db, seams, reconciler = env
    booked_tid = "1000"
    fill_time = _NOW - timedelta(hours=1)
    _insert_booked_fill(db, tid=booked_tid, fill_time=fill_time)
    base_ms = int((_NOW - timedelta(hours=2)).timestamp() * 1000)
    page1 = [
        {"tid": booked_tid if i == 0 else f"p1-{i}", "time": base_ms + i}
        for i in range(RESPONSE_FILL_CAP)
    ]
    page2 = [{"tid": "p2-1", "time": base_ms + RESPONSE_FILL_CAP + 1}]
    calls: list[int] = []

    def pager(start_ms, end_ms):
        calls.append(start_ms)
        return page1 if len(calls) == 1 else page2

    reconciler._fetch_fills = pager
    report = reconciler.run("heartbeat")
    assert len(calls) == 2  # page one was capped, page two completed the window
    assert calls[1] == base_ms + RESPONSE_FILL_CAP - 1  # advanced to page 1's newest
    assert report.fills_reconciled  # the page-1 key still counted
    assert not [c for c in report.cases if c.case_type == "invalid_local_fill"]


# -- §6.1/§6.6 snapshot identities (the mode-agnostic validator's contract) ----


def _account_identities_hold(db) -> bool:
    """Re-run `validate`'s OWN identity check over every live snapshot row.

    ``validate`` is mode-agnostic: it recomputes these identities for every
    account_snapshots row regardless of mode, under DECIMAL_CONTEXT (see
    _snapshot_mismatches). Calling the real checker — rather than restating its
    arithmetic here — is what makes this a contract test: a live writer that
    drifts from the paper writer's canonical formulas fails it.
    """
    from decimal import localcontext

    from contrib.hyperliquid_perp.paper.validation import _account_row_identities_ok
    from contrib.hyperliquid_perp.persistence.models import DECIMAL_CONTEXT

    rows = db.conn.execute("SELECT * FROM account_snapshots WHERE run_id = 'r'").fetchall()
    assert rows, "expected at least one snapshot row to check"
    with localcontext(DECIMAL_CONTEXT):
        return all(_account_row_identities_ok(row) for row in rows)


def test_a_flat_snapshot_satisfies_the_validator_identities_with_a_null_ratio(env):
    # A flat live account (maintenance margin 0) must store margin_ratio NULL:
    # the ratio is undefined with no position, and a 0 would both break the
    # identity and read downstream as a real (maximally safe) ratio.
    db, seams, reconciler = env
    report = reconciler.run("heartbeat")
    assert report.clean
    row = db.conn.execute("SELECT * FROM account_snapshots WHERE run_id='r'").fetchone()
    assert row["margin_ratio"] is None
    assert _account_identities_hold(db)


def test_a_live_snapshot_satisfies_the_validator_identities_with_a_position(env):
    # The open-position shape, where the pre-fix writer broke THREE identities:
    # margin_ratio was stored inverted (maint/equity), available_balance was a
    # duplicate of the raw exchange withdrawable rather than equity − used_im,
    # and total_pnl dropped its −fees +funding terms.
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        withdrawable="49",
        margin_used="51",
        maintenance="2",
        positions=[_btc_position(szi="0.001", value="51")],
    )
    reconciler.run("heartbeat")
    row = db.conn.execute("SELECT * FROM account_snapshots WHERE run_id='r'").fetchone()
    equity = Decimal(row["account_equity"])
    maint = Decimal(row["total_maintenance_margin"])
    # equity/maint (50.5), NOT its reciprocal — the §6.6 risk metric.
    assert Decimal(row["margin_ratio"]) == equity / maint
    # The local view is derived; the exchange's withdrawable keeps its own column.
    assert Decimal(row["available_balance"]) == equity - Decimal(row["used_initial_margin"])
    assert row["exchange_withdrawable"] == "49"
    assert Decimal(row["available_balance"]) != Decimal(row["exchange_withdrawable"])
    assert _account_identities_hold(db)


# -- once-per-fact dedupe keys ------------------------------------------------


def test_an_off_coin_and_a_same_coin_position_fact_never_collide(env):
    # The dedupe key is (run_id, case_type, exchange_value) — there is NO symbol
    # column in it. Both facts below are exchange_position_mismatch and shared a
    # bare size of "2.5" before the fix, so the second insert was swallowed and
    # its audit row lost — possibly the MANUAL off-coin one that fires safe mode.
    db, seams, reconciler = env
    seams.clearinghouse = _clearinghouse(
        account_value="101",
        maintenance="1",
        positions=[
            _btc_position(szi="2.5", value="153"),
            {
                "coin": "ETH",
                "szi": "2.5",
                "entryPx": "3000",
                "unrealizedPnl": "0",
                "positionValue": "7500",
                "marginUsed": "7500",
            },
        ],
    )
    reconciler.run("heartbeat")
    rows = _cases(db, "exchange_position_mismatch")
    assert len(rows) == 2  # both facts survived
    assert {r["exchange_value"] for r in rows} == {"ETH:2.5", "BTC:0->2.5"}


def test_two_phantoms_of_different_sizes_each_get_their_own_row(env):
    # A phantom's exchange side is ALWAYS 0, so keying the fact on the exchange
    # size alone collapsed every phantom this run ever saw onto one row. The
    # local size is the distinguishing datum.
    db, seams, reconciler = env
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.001"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    reconciler.run("heartbeat")  # exchange flat, local 0.001 → phantom
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=Decimal("0.004"), entry_price=Decimal(50000)),
            updated_at=_NOW,
        )
    reconciler.run("heartbeat")  # an independent, larger phantom later
    rows = _cases(db, "local_position_phantom")
    assert {r["exchange_value"] for r in rows} == {"BTC:0.001->0", "BTC:0.004->0"}
    # The SAME unhealed phantom re-observed still dedupes to its one row.
    reconciler.run("heartbeat")
    assert len(_cases(db, "local_position_phantom")) == 2


# -- the verdict's reason reaches the operator --------------------------------


def test_an_unmapped_backlog_enters_safe_mode_with_a_named_detail(env):
    # The end-to-end shape of the silent-failure fix: the backlog blocks the
    # pass, and the reason survives all the way into the safe-mode entry detail
    # (and the persisted diff) instead of entering with detail="".
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_unmapped",
            exchange_value=exchange_fill_key(tid="777"),
        )
    report = reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert not report.clean
    state = safe_mode.current()
    assert state is not None and state.safe_mode_type == "recoverable"
    (event,) = [e for e in repo.iter_safe_mode_events(db.conn, "r") if e["detail"]]
    assert "unmapped fill sighting" in event["detail"]
    # The persisted audit diff names it too.
    row = db.conn.execute("SELECT * FROM account_snapshots WHERE run_id='r'").fetchone()
    assert "unmapped fill sighting" in row["reconciliation_diff"]


def test_sweep_failures_make_the_pass_unclean_without_a_release_flap(env):
    # Decided 2026-07-17: a §19.3 cancel that would not land is a verdict INPUT.
    # Folding it in BEFORE the release door means a reconciliation-clean pass
    # can neither auto-release the latch nor re-anchor the episode — and the
    # entry names the sweep, not the generic mismatch.
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    reconciler._backfiller = _StubBackfiller()
    safe_mode.enter("recoverable", "ws_disconnect")  # a latch a clean pass would lift

    report = reconciler.reconcile_and_apply(
        "heartbeat",
        safe_mode=safe_mode,
        ws_restored=True,
        kill_switch_active=True,
        sweep_failures=("123 (entry): ExchangeError: nope",),
    )
    assert not report.clean  # reconciliation was clean; the sweep failure isn't
    assert report.reconciliation_clean  # ...but the books themselves proved out
    assert "123 (entry)" in "; ".join(report.sweep_failures)
    # The PERSISTED pass must agree with the verdict: the row is recorded inside
    # run(), so a sweep failure folded in afterwards would leave a "mismatch"
    # verdict stored as reconciliation_status "ok" with no cause in its diff —
    # exactly what a post-mortem reader (who no longer has the CLI transcript)
    # would be misled by.
    row = db.conn.execute("SELECT * FROM account_snapshots WHERE run_id='r'").fetchone()
    assert row["reconciliation_status"] == "mismatch"
    assert "123 (entry)" in row["reconciliation_diff"]
    state = safe_mode.current()
    assert state is not None  # the latch was NOT released
    assert gate.state_reconciled is False
    types = [e["event_type"] for e in repo.iter_safe_mode_events(db.conn, "r")]
    assert "safe_mode_released" not in types  # no release→re-enter flap
    reasons = [e["reason"] for e in repo.iter_safe_mode_events(db.conn, "r")]
    assert "stale_order_sweep_failed" in reasons  # named specifically


def test_a_compound_failure_names_every_cause_in_the_safe_mode_detail(env):
    # Causes arrive on three separate channels — unresolved case types, the
    # errors-only legs, and the sweep. A compound failure must not let whichever
    # channel is checked first suppress the others from the §13.6 triage
    # surface: an operator reading "equity_mismatch" would never learn that
    # exchange fills are also unbooked and a stale order is still resting.
    db, seams, reconciler = env
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    seams.clearinghouse = _clearinghouse(account_value="90")  # equity mismatch (a case)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(  # an unmapped backlog (an error)
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_unmapped",
            exchange_value=exchange_fill_key(tid="777"),
        )
    reconciler.reconcile_and_apply(
        "heartbeat",
        safe_mode=safe_mode,
        ws_restored=True,
        kill_switch_active=True,
        sweep_failures=("123 (entry): ExchangeError: nope",),
    )
    (event,) = [e for e in repo.iter_safe_mode_events(db.conn, "r") if e["detail"]]
    assert "equity_mismatch" in event["detail"]
    assert "unmapped fill sighting" in event["detail"]
    assert "123 (entry)" in event["detail"]
