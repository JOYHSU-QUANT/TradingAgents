"""Tests for live fill ingestion, dedupe, accounting, and backfill (phase3-spec §14/§15).

Covers the PR 3 fill pipeline: parsing a raw Hyperliquid fill, the exchange-basis
accounting (closedPnl / fee, not the paper model), the §14.3 exactly-once guard
across three sources, the §15.1 pending-fee correction via an adjustment event,
and live replay from the recorded exchange events.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import MalformedResponseError
from contrib.hyperliquid_perp.live.fills import (
    BackfillOutcome,
    ExchangeFill,
    IngestOutcome,
    LiveFillProcessor,
    apply_live_fill,
    backfill_fill_fee,
    post_live_fill,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.accounting import compute_live_fill_effect
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import live_fill_id
from contrib.hyperliquid_perp.persistence.models import PositionState, Side
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
_HEX = "0x" + "ab" * 16
_TIME_MS = int(_NOW.timestamp() * 1000)


@pytest.fixture
def db():
    with Database(":memory:") as database:
        yield database


@pytest.fixture
def clock():
    return ManualClock(_NOW)


def _live_run(db, *, run_id="r", balance="1000"):
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=Decimal(balance),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW,
    )


def _live_order(
    db,
    *,
    order_id="o1",
    oid="777",
    cloid_hex=_HEX,
    cloid_logical="log-1",
    coin="BTC",
    side="buy",
    run_id="r",
):
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id=order_id,
            mode="live",
            run_id=run_id,
            symbol=coin,
            order_role="entry",
            side=side,
            order_type="ioc_limit",
            qty=Decimal("1"),
            status="filled",
            price=Decimal("100"),
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            exchange_order_id=oid,
            is_bot_owned=True,
            timestamp=_NOW,
        )


def _fill(
    *,
    oid=777,
    tid=1,
    side="B",
    px="100",
    sz="0.5",
    closed="0",
    fee="0.025",
    crossed=True,
    coin="BTC",
    time_ms=_TIME_MS,
):
    d = {
        "coin": coin,
        "side": side,
        "px": px,
        "sz": sz,
        "closedPnl": closed,
        "crossed": crossed,
        "oid": oid,
        "time": time_ms,
    }
    if tid is not None:
        d["tid"] = tid
    if fee is not None:
        d["fee"] = fee
        d["feeToken"] = "USDC"
    return d


def _ledger(db, run_id="r"):
    return repo.get_current_account_state(db.conn, run_id)


def _position(db, coin="BTC", run_id="r"):
    return repo.get_current_position(db.conn, run_id, coin)


# ---------------------------------------------------------------------------
# ExchangeFill.parse
# ---------------------------------------------------------------------------


def test_parse_maps_side_role_fee_and_key():
    ef = ExchangeFill.parse(_fill(side="B", crossed=True, tid=42))
    assert ef.side is Side.BUY
    assert ef.liquidity_role == "taker"
    assert ef.fee == Decimal("0.025")
    assert ef.exchange_fill_key == "tid|42"
    assert ef.fill_notional == Decimal("50.0")
    ask = ExchangeFill.parse(_fill(side="A", crossed=False))
    assert ask.side is Side.SELL
    assert ask.liquidity_role == "maker"


def test_parse_without_tid_is_malformed():
    # §14.2 decision: the dedupe key IS the exchange's tid. HL sends it on every
    # fill (both WS and REST), so a tid-less fill is an anomaly — NOT a case for a
    # composite key, which can collide two genuinely distinct fills (same order,
    # same ms, same side/price/size) and silently drop one. It must fail loud so
    # §11.3 records the payload and reconciliation sees it.
    with pytest.raises(MalformedResponseError, match="tid"):
        ExchangeFill.parse(_fill(tid=None))


def test_tidless_fill_is_recorded_and_skipped_not_applied(db, clock, tmp_path):
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    msg = {"channel": "userFills", "data": {"fills": [_fill(tid=None, sz="1")]}}
    assert proc.ingest_message(msg) == []  # nothing applied
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert list(tmp_path.glob("fill_parse_error-*.json"))  # evidence kept (§11.3)


def test_parse_absent_fee_is_pending():
    ef = ExchangeFill.parse(_fill(fee=None))
    assert ef.fee is None


def test_parse_non_usdc_fee_is_pending():
    raw = _fill(fee="0.01")
    raw["feeToken"] = "ETH"
    assert ExchangeFill.parse(raw).fee is None


@pytest.mark.parametrize(
    "missing", ["coin", "side", "px", "sz", "closedPnl", "oid", "time", "crossed", "tid"]
)
def test_parse_missing_required_field_fails_loud(missing):
    raw = _fill()
    del raw[missing]
    with pytest.raises(MalformedResponseError):
        ExchangeFill.parse(raw)


def test_parse_unknown_side_fails_loud():
    with pytest.raises(MalformedResponseError, match="side"):
        ExchangeFill.parse(_fill(side="X"))


# ---------------------------------------------------------------------------
# compute_live_fill_effect — exchange-authoritative money
# ---------------------------------------------------------------------------


def test_live_effect_uses_exchange_closed_pnl_not_computed():
    # Long 1 @ 100; close by selling 1 @ 110. The naive computed realized would
    # be +10, but the exchange reports 7 (funding/fees folded); the effect must
    # trust the exchange, not the model.
    pos = PositionState(coin="BTC", size=Decimal("1"), entry_price=Decimal("100"))
    eff = compute_live_fill_effect(
        pos,
        side="sell",
        qty=Decimal("1"),
        price=Decimal("110"),
        exchange_fee=Decimal("0.05"),
        exchange_closed_pnl=Decimal("7"),
    )
    assert eff.realized_pnl_delta == Decimal("7")
    assert eff.wallet_delta == Decimal("6.95")  # 7 - 0.05
    assert eff.position.is_flat
    assert eff.position.realized_pnl == Decimal("7")


def test_live_effect_allows_negative_fee_rebate():
    # A maker rebate is a legitimately negative fee — LiveFillEffect must not
    # reject it (unlike the paper FillEffect).
    pos = PositionState.flat("BTC")
    eff = compute_live_fill_effect(
        pos,
        side="buy",
        qty=Decimal("1"),
        price=Decimal("100"),
        exchange_fee=Decimal("-0.01"),
        exchange_closed_pnl=Decimal("0"),
    )
    assert eff.fee == Decimal("-0.01")
    assert eff.wallet_delta == Decimal("0.01")


# ---------------------------------------------------------------------------
# apply / post — atomic exchange-basis posting (§14.3)
# ---------------------------------------------------------------------------


def test_post_live_fill_moves_wallet_and_position(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(side="B", sz="1", px="100", closed="0", fee="0.05"))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1", cloid_logical="log-1", cloid_hex=_HEX)
    pos = _position(db)
    assert pos.size == Decimal("1")
    assert pos.entry_price == Decimal("100")
    ledger = _ledger(db)
    assert ledger.wallet_balance == Decimal("999.95")  # 1000 - 0.05 fee
    assert ledger.total_fees == Decimal("0.05")
    assert ledger.realized_pnl == Decimal("0")
    # The fill row carries the exchange basis columns.
    row = repo.get_fill(db.conn, live_fill_id("r", fill.exchange_fill_key))
    assert row["exchange_fee"] == "0.05"
    assert row["exchange_closed_pnl"] == "0"
    assert row["liquidity_role"] == "taker"
    assert row["mode"] == "live"


def test_apply_live_fill_requires_open_transaction(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1"))
    with pytest.raises(ValueError, match="open transaction"):
        apply_live_fill(db.conn, run_id="r", fill=fill, order_id="o1")


def test_partial_reduce_keeps_entry_and_books_exchange_pnl(db):
    # The case the full-open/full-close tests miss: a fill that closes PART of a
    # position must leave the remainder open at the ORIGINAL entry, while the
    # realized PnL comes from the exchange's closedPnl (not a recomputed one).
    _live_run(db)
    _live_order(db)
    post_live_fill(  # open 2 @ 100
        db,
        run_id="r",
        fill=ExchangeFill.parse(_fill(tid=1, side="B", sz="2", px="100", closed="0", fee="0.1")),
        order_id="o1",
    )
    post_live_fill(  # sell 1 @ 110, exchange says closedPnl = 5 (not the naive 10)
        db,
        run_id="r",
        fill=ExchangeFill.parse(_fill(tid=2, side="A", sz="1", px="110", closed="5", fee="0.055")),
        order_id="o1",
    )
    pos = _position(db)
    assert pos.size == Decimal("1")
    assert pos.entry_price == Decimal("100")  # remainder keeps the original entry
    assert pos.realized_pnl == Decimal("5")  # the exchange's number, not 10
    ledger = _ledger(db)
    assert ledger.realized_pnl == Decimal("5")
    assert ledger.total_fees == Decimal("0.155")
    assert ledger.wallet_balance == Decimal("1000") - Decimal("0.155") + Decimal("5")
    assert accounting.replay(db, run_id="r").is_consistent


def test_flip_through_zero_reopens_at_fill_price(db):
    # Long 1 @100, then sell 2 @110 in one fill: closes the long and opens a
    # short 1 at 110, booking the exchange's closedPnl for the closed leg.
    _live_run(db)
    _live_order(db)
    post_live_fill(
        db,
        run_id="r",
        fill=ExchangeFill.parse(_fill(tid=1, side="B", sz="1", px="100", closed="0", fee="0.05")),
        order_id="o1",
    )
    post_live_fill(
        db,
        run_id="r",
        fill=ExchangeFill.parse(_fill(tid=2, side="A", sz="2", px="110", closed="9.8", fee="0.11")),
        order_id="o1",
    )
    pos = _position(db)
    assert pos.size == Decimal("-1")  # flipped short
    assert pos.entry_price == Decimal("110")  # remainder opens at the fill price
    assert pos.realized_pnl == Decimal("9.8")
    assert accounting.replay(db, run_id="r").is_consistent


def test_negative_fee_rebate_credits_the_wallet_end_to_end(db):
    # A maker rebate is a negative exchange fee: it must INCREASE the wallet and
    # decrease total_fees, all the way through the persisted ledger.
    _live_run(db)
    _live_order(db)
    raw = _fill(sz="1", px="100", closed="0", fee="0.05", crossed=False)
    raw["fee"] = "-0.01"
    post_live_fill(db, run_id="r", fill=ExchangeFill.parse(raw), order_id="o1")
    ledger = _ledger(db)
    assert ledger.total_fees == Decimal("-0.01")
    assert ledger.wallet_balance == Decimal("1000.01")
    assert accounting.replay(db, run_id="r").is_consistent


def test_unmapped_fill_applies_once_after_the_order_is_recorded(db, clock, tmp_path):
    # The documented recovery path: a fill for an oid we don't know yet is left
    # alone; once §8.3 recovery records the order, a re-ingest applies it — and
    # the dedupe key still keeps it exactly-once.
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(oid=777, sz="1", fee="0.05")
    assert proc.ingest(raw).outcome is IngestOutcome.UNMAPPED
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0

    _live_order(db, oid="777")  # the order surfaces (recovery / ack back-fill)
    assert proc.ingest(raw).outcome is IngestOutcome.APPLIED
    assert proc.ingest(raw).outcome is IngestOutcome.DUPLICATE
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert _position(db).size == Decimal("1")


# ---------------------------------------------------------------------------
# §14.3 exactly-once across three sources + crash idempotency
# ---------------------------------------------------------------------------


def test_same_fill_from_three_sources_applied_once(db, clock, tmp_path):
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(side="B", sz="1", px="100", closed="0", fee="0.05", tid=99)
    # WS, then REST, then orderStatus — all carry the same tid, so the same key.
    outcomes = [proc.ingest(raw).outcome for _ in range(3)]
    assert outcomes == [IngestOutcome.APPLIED, IngestOutcome.DUPLICATE, IngestOutcome.DUPLICATE]
    # Applied exactly once: one fill row, one position move, one fee.
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert _position(db).size == Decimal("1")
    assert _ledger(db).total_fees == Decimal("0.05")


def test_duplicate_insert_rolls_back_whole_unit(db):
    # "Crash 前後冪等": a re-post of an applied fill aborts on the UNIQUE key and
    # rolls back atomically — no partial state, the books unchanged.
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee="0.05"))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    before_wallet = _ledger(db).wallet_balance
    with pytest.raises(sqlite3.IntegrityError):
        post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert _ledger(db).wallet_balance == before_wallet


def test_fill_for_unknown_order_is_unmapped(db, clock, tmp_path):
    _live_run(db)  # no order inserted
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    result = proc.ingest(_fill(oid=12345))
    assert result.outcome is IngestOutcome.UNMAPPED
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# ingest_message — WS envelope handling + §11.3 malformed recording
# ---------------------------------------------------------------------------


def test_ingest_message_applies_good_skips_malformed(db, clock, tmp_path):
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    good = _fill(sz="1", tid=1)
    bad = _fill(sz="1", tid=2)
    del bad["px"]  # malformed
    msg = {"channel": "userFills", "data": {"fills": [good, bad]}}
    results = proc.ingest_message(msg)
    assert [r.outcome for r in results] == [IngestOutcome.APPLIED]  # bad one skipped
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    # §11.3: the malformed payload was recorded, not silently dropped.
    assert list(tmp_path.glob("fill_parse_error-*.json"))


def test_ingest_message_ignores_non_userfills_channel(db, clock, tmp_path):
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc.ingest_message({"channel": "orderUpdates", "data": []}) == []


# ---------------------------------------------------------------------------
# §15.1 pending fee + backfill via adjustment event
# ---------------------------------------------------------------------------


def test_pending_fee_posts_zero_then_backfill_adjusts(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", px="100", closed="0", fee=None))  # fee pending
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    # Ingest: no fee posted yet.
    assert _ledger(db).total_fees == Decimal("0")
    assert _ledger(db).wallet_balance == Decimal("1000")
    assert repo.get_fill(db.conn, fid)["exchange_fee"] is None
    assert [r["fill_id"] for r in repo.iter_live_fills(db.conn, "r", pending_fee_only=True)] == [
        fid
    ]

    # Backfill the learned fee → adjustment event + ledger move.
    res = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), source="recon"
    )
    assert res.outcome is BackfillOutcome.POSTED
    assert _ledger(db).total_fees == Decimal("0.05")
    assert _ledger(db).wallet_balance == Decimal("999.95")
    adj = repo.iter_accounting_adjustment_events(db.conn, "r")
    assert len(adj) == 1
    assert adj[0]["adjustment_type"] == "fee"
    assert (adj[0]["old_value"], adj[0]["new_value"]) == ("0", "0.05")
    # No longer pending (correction recorded).
    assert repo.iter_live_fills(db.conn, "r", pending_fee_only=True) == []


def test_fee_backfill_is_exactly_once(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"))
    again = backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"))
    assert again.outcome is BackfillOutcome.ALREADY_POSTED
    # The wallet moved once, not twice.
    assert _ledger(db).total_fees == Decimal("0.05")
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 1


def test_fee_backfill_refuses_non_pending_fill(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee="0.05"))  # fee present at ingest
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    with pytest.raises(ValueError, match="not pending"):
        backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"))


# ---------------------------------------------------------------------------
# live replay consistency (§15 / spec §5)
# ---------------------------------------------------------------------------


def _sequence(db):
    """Open long 1 @100, add 1 @102, close 2 @110 with an exchange closedPnl."""
    _live_run(db)
    _live_order(db, order_id="o1", oid="777", cloid_hex="0x" + "11" * 16, cloid_logical="l1")
    _live_order(db, order_id="o2", oid="888", cloid_hex="0x" + "22" * 16, cloid_logical="l2")
    post_live_fill(
        db,
        run_id="r",
        fill=ExchangeFill.parse(
            _fill(oid=777, tid=1, side="B", sz="1", px="100", closed="0", fee="0.05")
        ),
        order_id="o1",
    )
    post_live_fill(
        db,
        run_id="r",
        fill=ExchangeFill.parse(
            _fill(oid=777, tid=2, side="B", sz="1", px="102", closed="0", fee="0.051")
        ),
        order_id="o1",
    )
    post_live_fill(
        db,
        run_id="r",
        fill=ExchangeFill.parse(
            _fill(oid=888, tid=3, side="A", sz="2", px="110", closed="15.7", fee="0.11")
        ),
        order_id="o2",
    )


def test_live_replay_is_consistent(db):
    _sequence(db)
    result = accounting.replay(db, run_id="r")
    assert result.is_consistent, result.mismatch_detail
    assert result.positions["BTC"].is_flat
    assert result.ledger.realized_pnl == Decimal("15.7")


def test_live_replay_consistent_after_fee_backfill(db):
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", px="100", closed="0", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    backfill_fill_fee(
        db,
        run_id="r",
        fill_id=live_fill_id("r", fill.exchange_fill_key),
        exchange_fee=Decimal("0.05"),
    )
    # Replay folds the adjustment; the rebuilt ledger matches materialized state.
    result = accounting.replay(db, run_id="r")
    assert result.is_consistent, result.mismatch_detail
    assert result.ledger.total_fees == Decimal("0.05")
    assert result.ledger.wallet_balance == Decimal("999.95")


def test_live_replay_detects_corrupted_materialized_state(db):
    _sequence(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE current_account_state SET wallet_balance = ? WHERE run_id = 'r'", ("123.45",)
        )
    result = accounting.replay(db, run_id="r")
    assert not result.is_consistent
    assert not result.account_matches


def test_adjustment_ledger_delta_signs_per_type():
    # The fold contract every §15 correction rests on. Only 'fee' is exercised by
    # PR3's backfill; 'funding' and 'realized_pnl' ship for PR4, so pin their
    # signs now — a swapped slot would silently misstate the books the first time
    # a real funding correction posted.
    old, new = Decimal("0"), Decimal("0.05")
    # fee: costs money — wallet down, total_fees up.
    assert accounting.adjustment_ledger_delta("fee", old, new) == (
        Decimal("-0.05"),
        Decimal("0"),
        Decimal("0.05"),
        Decimal("0"),
    )
    # funding: signed pnl — moves wallet and net_funding together.
    assert accounting.adjustment_ledger_delta("funding", old, new) == (
        Decimal("0.05"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0.05"),
    )
    # realized_pnl: moves wallet and realized together.
    assert accounting.adjustment_ledger_delta("realized_pnl", old, new) == (
        Decimal("0.05"),
        Decimal("0.05"),
        Decimal("0"),
        Decimal("0"),
    )
    # A negative correction reverses every sign (new < old).
    assert accounting.adjustment_ledger_delta("funding", Decimal("0.05"), Decimal("0")) == (
        Decimal("-0.05"),
        Decimal("0"),
        Decimal("0"),
        Decimal("-0.05"),
    )


def test_adjustment_ledger_delta_rejects_unknown_type():
    with pytest.raises(ValueError, match="adjustment_type"):
        accounting.adjustment_ledger_delta("yolo", Decimal("0"), Decimal("1"))


@pytest.mark.parametrize(
    ("adj_type", "wallet", "realized", "fees", "funding"),
    [
        ("funding", Decimal("1000.2"), Decimal("0"), Decimal("0"), Decimal("0.2")),
        ("realized_pnl", Decimal("1000.2"), Decimal("0.2"), Decimal("0"), Decimal("0")),
    ],
)
def test_replay_folds_funding_and_realized_adjustments(
    db, adj_type, wallet, realized, fees, funding
):
    # An adjustment of a non-fee type must be folded by replay with the same signs
    # the poster used — otherwise replay would report a spurious mismatch (or mask
    # a real one) the moment PR4 posts its first funding correction.
    _live_run(db)
    with db.transaction() as conn:
        repo.insert_accounting_adjustment_event(
            conn,
            adjustment_id="adj-1",
            run_id="r",
            adjustment_type=adj_type,
            target_table="funding_events",
            target_id="fe-1",
            field="funding_pnl",
            old_value=Decimal("0"),
            new_value=Decimal("0.2"),
            timestamp=_NOW,
        )
        # The materialized ledger the poster would have left behind.
        repo.upsert_current_account_state(
            conn,
            "r",
            accounting.AccountLedger(
                wallet_balance=wallet,
                realized_pnl=realized,
                total_fees=fees,
                net_funding_pnl=funding,
            ),
            updated_at=_NOW,
        )
    result = accounting.replay(db, run_id="r")
    assert result.is_consistent, result.mismatch_detail
    assert result.ledger.wallet_balance == wallet
    assert result.ledger.net_funding_pnl == funding
    assert result.ledger.realized_pnl == realized


def test_live_replay_detects_position_mismatch(db):
    _sequence(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE current_positions SET size = ?, entry_price = ? "
            "WHERE run_id = 'r' AND symbol = 'BTC'",
            ("5", "100"),
        )
    result = accounting.replay(db, run_id="r")
    assert "BTC" in result.position_mismatches
