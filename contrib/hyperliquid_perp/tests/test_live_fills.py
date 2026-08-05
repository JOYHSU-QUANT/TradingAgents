"""Tests for live fill ingestion, dedupe, accounting, and backfill (phase3-spec §14/§15).

Covers the PR 3 fill pipeline: parsing a raw Hyperliquid fill, the exchange-basis
accounting (closedPnl / fee, not the paper model), the §14.3 exactly-once guard
across three sources, the §15.1 pending-fee correction via an adjustment event,
and live replay from the recorded exchange events.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import MalformedResponseError
from contrib.hyperliquid_perp.live.fills import (
    BackfillOutcome,
    BackfillResult,
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


def test_parse_fee_without_fee_token_is_pending():
    # feeToken is documented as always present, so its absence is payload drift:
    # an amount we cannot prove is USDC must ride the pending lane, never be
    # booked as if it were USDC.
    raw = _fill(fee="0.01")
    del raw["feeToken"]
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


def test_hand_built_fill_with_naive_time_is_rejected():
    # fill_time is half the §14.3 ordering key: a naive datetime would surface as
    # an opaque "can't compare offset-naive and offset-aware" TypeError deep in
    # apply_live_fill's newest-key comparison, far from whoever built the instance.
    # parse always supplies aware UTC, so only a hand-built instance can trip this.
    ef = ExchangeFill.parse(_fill())
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(ef, fill_time=ef.fill_time.replace(tzinfo=None))


def test_hand_built_fill_with_mismatched_dedupe_key_is_rejected():
    # exchange_fill_key is derived state (§14.2: tid|<tid>). A hand-built instance
    # whose key disagrees with its exchange_fill_id would dedupe under one identity
    # while auditing another — parse derives both from the same tid, so only a
    # hand-built instance can trip this.
    ef = ExchangeFill.parse(_fill())
    with pytest.raises(ValueError, match="exchange_fill_key"):
        replace(ef, exchange_fill_key="tid|999999")


def test_ingest_rejects_fill_paired_with_wrong_payload(db, clock, tmp_path):
    # The documented fill=/raw_fill= pairing is enforced: a mismatched pair would
    # apply one fill's money while recording the OTHER fill's payload as evidence,
    # and both the dedupe key and the audit trail would look internally consistent.
    # A plain ValueError (caller contract), NOT MalformedResponseError — the §11.3
    # skip-one-and-continue handlers must not swallow it as a payload defect.
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    with pytest.raises(ValueError, match="parse of raw_fill"):
        proc.ingest(_fill(tid=1), fill=ExchangeFill.parse(_fill(tid=2)))
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


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


def test_applied_fill_evidence_file_survives_the_dedupe_key_pipe(db, clock, tmp_path):
    """The §16.2 evidence write must survive the ``|`` in every fill's dedupe key.

    ``exchange_fill_key`` is ``"tid|<tid>"`` and ``|`` is an illegal filename
    character on Windows. ``write_raw_payload`` is deliberately fail-soft, so a
    broken ``_safe_key`` would not raise — it would silently record NO evidence
    path for every applied fill (and a Linux-only CI would never notice, since
    the pipe is legal there). Assert the path is recorded and the file exists.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    result = proc.ingest(_fill(sz="1", tid=42))
    assert result.outcome is IngestOutcome.APPLIED
    fid = live_fill_id("r", result.fill.exchange_fill_key)
    path = repo.get_fill(db.conn, fid)["raw_exchange_payload_path"]
    assert path is not None
    assert Path(path).exists()


def test_insert_live_fill_requires_the_exchange_fill_time(db):
    """The write boundary refuses a NULL exchange_fill_time — it is the §14.3
    ordering key; full rationale at the ``insert_live_fill`` guard."""
    _live_run(db)
    _live_order(db)
    with pytest.raises(ValueError, match="exchange_fill_time"), db.transaction() as conn:
        repo.insert_live_fill(
            conn,
            fill_id="f-null-time",
            run_id="r",
            order_id="o1",
            symbol="BTC",
            side="buy",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fill_notional=Decimal("100"),
            exchange_fill_key="tid|9999",
            exchange_fill_time=None,  # type: ignore[arg-type]
            exchange_closed_pnl=Decimal("0"),
            liquidity_role="taker",
            exchange_fill_id="9999",
            exchange_order_id="1",
        )
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["exchange_fill_id", "exchange_order_id"])
def test_insert_live_fill_requires_the_exchange_ids(db, missing):
    """The write boundary refuses an empty tid/oid: every live fill has both by
    construction (parse requires them; an unmapped-oid fill is never inserted),
    and a NULL would silently break the evidence keys, §15.1 rule-8 redelivery
    verification and the §12.3 drift recorders that assume them on every row."""
    _live_run(db)
    _live_order(db)
    ids = {"exchange_fill_id": "9999", "exchange_order_id": "1", missing: ""}
    with pytest.raises(ValueError, match="exchange_fill_id"), db.transaction() as conn:
        repo.insert_live_fill(
            conn,
            fill_id="f-no-ids",
            run_id="r",
            order_id="o1",
            symbol="BTC",
            side="buy",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fill_notional=Decimal("100"),
            exchange_fill_key="tid|9999",
            exchange_fill_time=_NOW,
            exchange_closed_pnl=Decimal("0"),
            liquidity_role="taker",
            **ids,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_insert_adjustment_requires_its_target(db):
    """A correction with no target would be folded by replay (which iterates every
    event) yet be invisible to the per-fill fee chain (target_id filtered) — the
    two reads would silently disagree; the write boundary refuses it instead."""
    _live_run(db)
    with pytest.raises(ValueError, match="target_table"), db.transaction() as conn:
        repo.insert_accounting_adjustment_event(
            conn,
            adjustment_id="adj-no-target",
            run_id="r",
            adjustment_type="fee",
            target_table="",
            target_id="",
            field="",
            old_value=Decimal("0"),
            new_value=Decimal("0.1"),
            timestamp=_NOW,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0


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
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC", source="recon"
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
    backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC")
    again = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC"
    )
    assert again.outcome is BackfillOutcome.ALREADY_POSTED
    # The wallet moved once, not twice.
    assert _ledger(db).total_fees == Decimal("0.05")
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 1


def test_fee_without_fee_token_ingests_with_the_fee_pending(db, clock, tmp_path):
    """A fill whose fee has no feeToken still books — with the fee PENDING, not as USDC."""
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(sz="1", px="100", closed="0", fee="0.05")
    del raw["feeToken"]  # fee present, token missing — cannot prove it is USDC

    result = proc.ingest(raw)

    assert result.outcome is IngestOutcome.APPLIED  # the fill itself books normally
    fid = live_fill_id("r", result.fill.exchange_fill_key)
    assert repo.get_fill(db.conn, fid)["exchange_fee"] is None  # pending, not 0.05
    assert [r["fill_id"] for r in repo.iter_live_fills(db.conn, "r", pending_fee_only=True)] == [
        fid
    ]
    ledger = _ledger(db)
    assert ledger.total_fees == Decimal("0")  # the unproven amount never hit the ledger
    assert ledger.wallet_balance == Decimal("1000")


def test_first_resolution_of_a_pending_fee_to_zero_still_posts(db):
    """A pending fee genuinely resolved to 0 must leave the backlog via an adjustment.

    0 equals the placeholder the ingest posted, but the fee being genuinely 0 is new
    information, and only an adjustment row records it — skipping the write would
    leave the fill in the pending_fee_only backlog forever (§15.1 rule 3). The ledger
    simply moves by 0.
    """
    _live_run(db)
    _live_order(db)
    raw = _fill(sz="1", fee="0.01")
    raw["feeToken"] = "ETH"  # fee pending: not proven USDC
    fill = ExchangeFill.parse(raw)
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    before = _ledger(db)

    res = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0"), fee_token="USDC"
    )

    assert res.outcome is BackfillOutcome.POSTED  # the FIRST resolution always writes
    assert res.adjustment_id is not None
    after = _ledger(db)
    assert (after.wallet_balance, after.realized_pnl, after.total_fees) == (
        before.wallet_balance,
        before.realized_pnl,
        before.total_fees,
    )  # the ledger moved by exactly 0
    assert repo.iter_live_fills(db.conn, "r", pending_fee_only=True) == []  # out of the backlog

    # Re-learning the same 0 afterwards IS a no-op: resolved, and nothing new to say.
    again = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0"), fee_token="USDC"
    )
    assert again.outcome is BackfillOutcome.ALREADY_POSTED
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 1


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity")], ids=["nan", "infinity"])
def test_fee_backfill_rejects_a_non_finite_fee(db, bad):
    """A NaN would poison the ledger irreversibly — rejected before anything is written."""
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    before = _ledger(db)

    with pytest.raises(ValueError, match="finite"):
        backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=bad, fee_token="USDC")

    # Nothing was written and the ledger did not move.
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    after = _ledger(db)
    assert (after.wallet_balance, after.realized_pnl, after.total_fees) == (
        before.wallet_balance,
        before.realized_pnl,
        before.total_fees,
    )


@pytest.mark.parametrize("token", ["ETH", "USDT", ""], ids=["eth", "usdt", "empty"])
def test_fee_backfill_rejects_an_unproven_denomination(db, token):
    """Resolution demands the same USDC proof ingest did (§15.1 rule 3)."""
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    before = _ledger(db)

    with pytest.raises(ValueError, match="USDC"):
        backfill_fill_fee(
            db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token=token
        )

    # Nothing was written, the ledger did not move, and the fill is STILL pending —
    # the backlog keeps resurfacing it until a proven USDC amount arrives.
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    assert _ledger(db).wallet_balance == before.wallet_balance
    assert [r["fill_id"] for r in repo.iter_live_fills(db.conn, "r", pending_fee_only=True)] == [
        fid
    ]


def test_backfill_result_posted_must_name_its_adjustment():
    """POSTED means a correction row exists, so the result must name it (enforced)."""
    with pytest.raises(ValueError, match="POSTED"):
        BackfillResult(BackfillOutcome.POSTED, None, Decimal("0.05"))
    # ALREADY_POSTED legitimately carries either shape: the newest correction's id,
    # or None when the ingested fee was simply confirmed.
    BackfillResult(BackfillOutcome.ALREADY_POSTED, None, Decimal("0.05"))
    BackfillResult(BackfillOutcome.ALREADY_POSTED, "adj-1", Decimal("0.05"))


def test_fee_backfill_of_an_unchanged_fee_is_a_no_op(db):
    """A fill already carrying this fee needs no correction — and the result says so."""
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee="0.05"))  # fee present at ingest
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)

    result = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC"
    )
    assert result.outcome is BackfillOutcome.ALREADY_POSTED
    assert result.fee == Decimal("0.05")
    assert result.adjustment_id is None  # nothing was recorded, so nothing is named
    assert _ledger(db).total_fees == Decimal("0.05")  # not doubled
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0


def test_fee_can_be_corrected_more_than_once(db):
    """A second, genuinely different correction posts its DELTA — it must not be refused.

    Refusing it (as a one-correction-per-target key did) would not merely lose the
    amount: the reconciliation job re-hits the same fill on every pass, so it would
    wedge there forever.
    """
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee=None))  # fee pending at ingest → posts 0
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)

    first = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC"
    )
    assert first.outcome is BackfillOutcome.POSTED
    assert _ledger(db).total_fees == Decimal("0.05")

    # The exchange later corrects the fee downward (a referral discount / late rebate).
    second = backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.03"), fee_token="USDC"
    )
    assert second.outcome is BackfillOutcome.POSTED
    assert second.adjustment_id != first.adjustment_id
    # The books carry the CORRECTED fee: the ledger moved by the delta (-0.02), not +0.03.
    assert _ledger(db).total_fees == Decimal("0.03")
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 2

    # ...and re-learning the corrected amount is still a no-op.
    assert (
        backfill_fill_fee(
            db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.03"), fee_token="USDC"
        ).outcome
        is BackfillOutcome.ALREADY_POSTED
    )
    assert _ledger(db).total_fees == Decimal("0.03")


def test_fee_backfill_refuses_another_runs_fill(db):
    """The ledger this moves is run_id's — another run's fill must not debit it."""
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)

    _live_run(db, run_id="other")
    with pytest.raises(ValueError, match="belongs to run"):
        backfill_fill_fee(
            db, run_id="other", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC"
        )


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


def test_live_replay_refuses_fill_without_exchange_basis(db):
    # require_live_fill_basis: a fill in a LIVE run whose exchange_closed_pnl is
    # NULL was written through a non-live path; folding it against the paper fee
    # model would silently misstate the books, so replay must refuse — never guess.
    _live_run(db)
    _live_order(db)
    with db.transaction() as conn:
        repo.insert_fill(
            conn,
            fill_id="rogue",
            mode="live",
            run_id="r",
            order_id="o1",
            symbol="BTC",
            side="buy",
            fill_qty=Decimal("1"),
            fill_price=Decimal("100"),
            fill_notional=Decimal("100"),
            fee=Decimal("0"),
            fee_rate=Decimal("0"),
            realized_pnl_delta=Decimal("0"),
            timestamp=_NOW,
        )
    with pytest.raises(ValueError, match="no exchange_closed_pnl"):
        accounting.replay(db, run_id="r")


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
        fee_token="USDC",
    )
    # Replay folds the adjustment; the rebuilt ledger matches materialized state.
    result = accounting.replay(db, run_id="r")
    assert result.is_consistent, result.mismatch_detail
    assert result.ledger.total_fees == Decimal("0.05")
    assert result.ledger.wallet_balance == Decimal("999.95")


def test_live_replay_consistent_after_a_second_fee_correction(db):
    """Replay folds EVERY adjustment row; the posting side reads only the newest.

    Two different traversals of ``accounting_adjustment_events`` that must
    telescope to the same answer — a double-count in ``_fold_adjustments`` or a
    wrong target/type filter is observable only with 2+ corrections on the same
    fill, so pin that exact shape against replay.
    """
    _live_run(db)
    _live_order(db)
    fill = ExchangeFill.parse(_fill(sz="1", px="100", closed="0", fee=None))
    post_live_fill(db, run_id="r", fill=fill, order_id="o1")
    fid = live_fill_id("r", fill.exchange_fill_key)
    backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.05"), fee_token="USDC")
    backfill_fill_fee(db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.03"), fee_token="USDC")

    result = accounting.replay(db, run_id="r")
    assert result.is_consistent, result.mismatch_detail
    assert result.ledger.total_fees == Decimal("0.03")
    assert result.ledger.wallet_balance == Decimal("999.97")


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


def test_a_registry_type_with_no_fold_arm_fails_loud(monkeypatch):
    # check_enum validates MEMBERSHIP, so a type ADDED to the registry sails
    # past it. It used to land in the trailing realized_pnl arm — wrong wallet
    # direction, wrong realized, wrong fees. And because this one definition
    # feeds both the live wallet posting and replay's fold, the books would
    # move wrong and replay would agree with itself, so
    # account_replay_mismatch_count reads 0 and nothing ever surfaces it.
    monkeypatch.setattr(
        repo, "ACCOUNTING_ADJUSTMENT_TYPES", repo.ACCOUNTING_ADJUSTMENT_TYPES | {"rebate"}
    )
    with pytest.raises(AssertionError, match="no fold arm"):
        accounting.adjustment_ledger_delta("rebate", Decimal("0"), Decimal("1"))
    # Control: the three real types still fold, so this pins the fallthrough
    # rather than the enum check in front of it.
    assert accounting.adjustment_ledger_delta("fee", Decimal("0"), Decimal("1")) == (
        Decimal("-1"),
        Decimal("0"),
        Decimal("1"),
        Decimal("0"),
    )


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


# ---------------------------------------------------------------------------
# §11.3 fault isolation: one bad fill must never take the batch down with it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({"sz": "0"}, "zero size"),
        ({"sz": "-1"}, "negative size"),
        ({"px": "0"}, "zero price"),
        ({"tid": ""}, "empty tid"),
    ],
)
def test_a_value_defect_is_malformed_not_a_crash(bad, why):
    """A payload defect must raise in the MALFORMED vocabulary, never a bare ValueError.

    ``ingest_message`` skips ``MalformedResponseError`` and nothing else — by design,
    since everything else reaching it is an impossible internal state. A ValueError
    from a constructor invariant would escape that handler and abort the whole batch.
    """
    with pytest.raises(MalformedResponseError):
        ExchangeFill.parse(_fill(**bad))


def test_one_poison_fill_does_not_abort_its_batch(db, tmp_path, clock):
    """The regression that wedged backfill forever: sibling fills must still apply.

    On the WS path the drained siblings would simply be lost. On the REST path it is
    worse: the same poison fill sits in every trailing window, so every later pass
    raises on it again and the gap never closes.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    results = proc.ingest_message(
        {
            "channel": "userFills",
            "data": {
                "fills": [
                    _fill(tid=1, sz="0"),  # poison: zero size
                    _fill(tid=2, sz="0.5"),  # a perfectly good fill behind it
                ]
            },
        }
    )

    assert [r.outcome for r in results] == [IngestOutcome.APPLIED]
    assert results[0].fill.exchange_fill_id == "2"
    # The poison fill left evidence rather than vanishing.
    assert list(tmp_path.glob("fill_parse_error-*.json"))


# ---------------------------------------------------------------------------
# Order mapping: a fill is only booked against THIS run's order for THIS coin
# ---------------------------------------------------------------------------


def test_unmapped_fill_records_evidence(db, tmp_path, clock):
    """A fill the exchange really executed and we did not book is EVIDENCE (§12.3)."""
    _live_run(db)  # no order with this oid
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    result = proc.ingest(_fill(oid=999))

    assert result.outcome is IngestOutcome.UNMAPPED
    assert result.effect is None
    assert list(tmp_path.glob("fill_unmapped-*.json"))  # outlives the log and the REST window
    assert _ledger(db).wallet_balance == Decimal("1000")  # nothing booked


def test_fill_for_another_runs_order_is_not_booked(db, tmp_path, clock):
    """The oid lookup is wallet-scoped; a restart RESUMES its run, so this is an anomaly."""
    _live_run(db)
    _live_run(db, run_id="older")
    _live_order(db, run_id="older", order_id="o-old", oid="777")
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    result = proc.ingest(_fill(oid=777))

    assert result.outcome is IngestOutcome.UNMAPPED
    assert _ledger(db).wallet_balance == Decimal("1000")
    assert list(tmp_path.glob("fill_unmapped-*.json"))


def test_fill_whose_symbol_contradicts_its_order_is_not_booked(db, tmp_path, clock):
    """A wrong mapping would otherwise open a position in a coin we never traded."""
    _live_run(db)
    _live_order(db, coin="BTC", oid="777")
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    result = proc.ingest(_fill(oid=777, coin="ETH"))

    assert result.outcome is IngestOutcome.UNMAPPED
    assert repo.get_current_position(db.conn, "r", "ETH") is None


def test_a_non_dedupe_integrity_error_is_not_reported_as_duplicate(
    db, tmp_path, clock, monkeypatch
):
    """IntegrityError also covers NOT NULL / CHECK / FK — those must fail loud.

    Read as DUPLICATE, such a failure would drop a real fill forever while the counters
    claimed the exactly-once guard had held, and every retry would repeat it.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    def _boom(*args, **kwargs):
        raise sqlite3.IntegrityError("NOT NULL constraint failed: fills.symbol")

    monkeypatch.setattr(repo, "insert_live_fill", _boom)

    with pytest.raises(sqlite3.IntegrityError):
        proc.ingest(_fill())


def test_dedupe_precheck_race_falls_back_to_verified_redelivery(db, tmp_path, clock, monkeypatch):
    """The UNIQUE key is the real dedupe guard; the pre-check is only a fast path.

    When the pre-check misses a fill that IS booked (the concurrent-insert race the
    except-branch exists for), the real INSERT hits the genuine UNIQUE constraint,
    and ingest must re-query and route the REAL booked row into _verify_redelivery —
    not re-raise, and not double-post. The redelivered payload here carries a
    corrected fee, proving the recovery path still runs full content verification:
    the correction posts exactly once through the normal lane.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc.ingest(_fill(fee="0.05", tid=33)).outcome is IngestOutcome.APPLIED
    wallet_before = _ledger(db).wallet_balance

    real_lookup = repo.get_fill_by_exchange_key
    calls = {"n": 0}

    def racy(conn, key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the pre-check loses the race
        return real_lookup(conn, key)

    monkeypatch.setattr(repo, "get_fill_by_exchange_key", racy)

    result = proc.ingest(_fill(fee="0.07", tid=33))

    assert result.outcome is IngestOutcome.DUPLICATE
    assert calls["n"] >= 2  # the insert was really attempted and the handler re-queried
    assert _ledger(db).total_fees == Decimal("0.07")  # verified, corrected — once
    assert _ledger(db).wallet_balance == wallet_before - Decimal("0.02")
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# §14 ordering: entry price is order-dependent, so exchange time is authoritative
# ---------------------------------------------------------------------------


def test_fills_apply_in_exchange_time_order_not_arrival_order(db, tmp_path, clock):
    """A reconnect backfill posts fills OLDER than ones the socket already delivered.

    Realized money is order-independent (it comes per-fill from closedPnl), but the
    weighted-average entry price is not — and replay folds in the same order, so it
    would faithfully reproduce a wrong entry price and no check could see it.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Delivered newest-first: buy 1 @200 (t+1s) arrives BEFORE buy 1 @100 (t).
    proc.ingest_message(
        {
            "channel": "userFills",
            "data": {
                "fills": [
                    _fill(tid=2, sz="1", px="200", fee=None, time_ms=_TIME_MS + 1000),
                    _fill(tid=1, sz="1", px="100", fee=None, time_ms=_TIME_MS),
                ]
            },
        }
    )

    position = repo.get_current_position(db.conn, "r", "BTC")
    assert position.size == Decimal("2")
    assert position.entry_price == Decimal("150")  # (100 + 200) / 2, in trade order

    # And replay — which folds live fills chronologically too — agrees.
    replayed = accounting.replay(db, run_id="r")
    assert replayed.position_mismatches == ()
    assert replayed.account_matches
    assert replayed.positions["BTC"].entry_price == Decimal("150")


# ---------------------------------------------------------------------------
# Out-of-order arrival: materialized books must equal replayed books
# ---------------------------------------------------------------------------


def test_a_fill_arriving_out_of_order_re_folds_the_position(db, tmp_path, clock):
    """A fill older than the newest booked one cannot be folded incrementally.

    Position is the one piece of state that does not commute. This is a designed path,
    not an edge case: an UNMAPPED fill re-ingested after §8.3 recovery records its oid
    lands AFTER newer fills, and a heartbeat backfill can do the same. Folded on top of
    the newer ones, its weighted-average entry price is wrong and nothing ever revisits
    it — and because replay folds chronologically, the two books would disagree while
    each stayed internally consistent, so no check could say which was right.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Booked first (it arrived first): the LATER trade, buy 1 @ 200 at t+10s.
    proc.ingest(_fill(tid=2, sz="1", px="200", fee=None, time_ms=_TIME_MS + 10_000))
    # Then the backfill delivers the EARLIER trade it had missed: buy 1 @ 100 at t.
    proc.ingest(_fill(tid=1, sz="1", px="100", fee=None, time_ms=_TIME_MS))

    position = _position(db)
    assert position.size == Decimal("2")
    assert position.entry_price == Decimal("150")  # (100 + 200) / 2 — trade order

    # The materialized books and the replayed books agree BY CONSTRUCTION.
    replayed = accounting.replay(db, run_id="r")
    assert replayed.position_mismatches == ()
    assert replayed.account_matches
    assert replayed.positions["BTC"].entry_price == Decimal("150")


def test_out_of_order_refold_survives_a_reduce(db, tmp_path, clock):
    """The case where order actually changes the answer: a close folded before its open."""
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Arrival order: the t+20s buy is seen first, then the t and t+10s fills backfill in.
    proc.ingest(_fill(tid=3, sz="1", px="300", fee=None, time_ms=_TIME_MS + 20_000))
    proc.ingest(_fill(tid=1, sz="1", px="100", fee=None, time_ms=_TIME_MS))
    proc.ingest(
        _fill(tid=2, side="A", sz="1", px="150", closed="50", fee=None, time_ms=_TIME_MS + 10_000)
    )

    # True chronology: buy 1 @100 → sell 1 @150 (flat, +50 realized) → buy 1 @300.
    position = _position(db)
    assert position.size == Decimal("1")
    assert position.entry_price == Decimal("300")
    assert position.realized_pnl == Decimal("50")

    replayed = accounting.replay(db, run_id="r")
    assert replayed.position_mismatches == ()
    assert replayed.account_matches


def test_in_order_fills_do_not_trigger_a_refold(db, tmp_path, clock, caplog):
    """The rebuild is the exception, not the rule — the common path stays incremental."""
    import logging

    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    with caplog.at_level(logging.INFO):
        proc.ingest(_fill(tid=1, sz="1", px="100", fee=None, time_ms=_TIME_MS))
        proc.ingest(_fill(tid=2, sz="1", px="200", fee=None, time_ms=_TIME_MS + 10_000))

    assert "re-folding" not in caplog.text
    assert _position(db).entry_price == Decimal("150")


def test_an_absurd_exponent_is_malformed_not_an_arithmetic_crash():
    """decimal.Overflow is an ArithmeticError, not a ValueError — it escaped the backstop.

    "1e1000000" is finite, so require_decimal's NaN/Inf guard passes it; the notional
    multiply then overflows. Escaping §11.3, it would abort the whole batch.
    """
    with pytest.raises(MalformedResponseError):
        ExchangeFill.parse(_fill(px="1e1000000", sz="1e1000000"))


def test_unmapped_evidence_is_written_once_not_once_per_sighting(db, tmp_path, clock):
    """An unapplied fill is re-fetched by every backfill pass — evidence must not pile up.

    It is never inserted, so no cursor advances past it; it sits inside every subsequent
    REST window forever. One file per sighting would grow without bound until the disk
    filled.
    """
    _live_run(db)  # no order → every pass leaves this fill UNMAPPED
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    for _ in range(5):  # five backfill passes re-fetch the same fill
        assert proc.ingest(_fill(oid=999)).outcome is IngestOutcome.UNMAPPED

    assert len(list(tmp_path.glob("fill_unmapped-*.json"))) == 1


def test_malformed_evidence_is_written_once_not_once_per_sighting(db, tmp_path, clock):
    """Same shape as the unmapped path: a malformed fill is never inserted either."""
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    for _ in range(5):
        proc.ingest_message({"channel": "userFills", "data": {"fills": [_fill(tid=7, sz="0")]}})

    assert len(list(tmp_path.glob("fill_parse_error-*.json"))) == 1


def test_the_mirrored_liquidation_price_is_not_part_of_the_replay_identity(db, tmp_path, clock):
    """A stored liq mirror must not make books that agree look mismatched.

    ``replay_within`` compares replayed against materialized ``PositionState``
    by dataclass equality, and no replay of the fill history can reconstruct an
    exchange-reported estimate. Counting it would report a phantom
    ``account_replay_mismatch`` on every live run holding a position — halting
    new decision cycles (the post-cycle verify) and failing the §20.3 gate on a
    run whose books are in fact correct.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    proc.ingest(_fill(tid=1, sz="1", px="100", fee=None, time_ms=_TIME_MS))
    assert accounting.replay(db, run_id="r").position_mismatches == ()

    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", Decimal("43210.5"))

    replayed = accounting.replay(db, run_id="r")
    assert _position(db).liquidation_price == Decimal("43210.5")  # stored...
    assert replayed.position_mismatches == ()  # ...but not an accounting fact
    assert replayed.account_matches


def test_same_millisecond_fill_that_sorts_earlier_is_out_of_order(db, tmp_path, clock):
    """Two fills in one millisecond: the fold breaks the tie on the key, so must we.

    tid|10 sorts BEFORE tid|5 (lexicographic, the same comparison the SQL ORDER BY
    makes), so a later-arriving tid|10 belongs EARLIER in the fold even though its
    timestamp is not older. Judged on time alone it looks in-order, gets stacked on
    top, and the materialized position silently disagrees with the replayed one.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Booked first, but sorts LAST: buy 1 @ 200, tid 5.
    proc.ingest(_fill(tid=5, sz="1", px="200", fee=None, time_ms=_TIME_MS))
    # Arrives second, but sorts FIRST (tid|10 < tid|5): buy 1 @ 100, same millisecond.
    proc.ingest(_fill(tid=10, sz="1", px="100", fee=None, time_ms=_TIME_MS))

    replayed = accounting.replay(db, run_id="r")
    assert replayed.position_mismatches == ()  # materialized == replayed
    assert replayed.account_matches
    assert _position(db).entry_price == replayed.positions["BTC"].entry_price


def test_two_distinct_unparseable_payloads_keep_distinct_evidence(db, tmp_path, clock):
    """`once` dedupes by key, so an ambiguous key would silently discard real evidence.

    A malformed fill often has no usable id — that can be *why* it is malformed. Keyed on
    a fallback, two different bad payloads collapse onto one file and the second is lost.
    """
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Two DIFFERENT tid-less payloads (same oid), plus a non-dict one.
    proc.ingest_message(
        {
            "channel": "userFills",
            "data": {"fills": [_fill(tid=None, px="100"), _fill(tid=None, px="200"), "garbage"]},
        }
    )
    assert len(list(tmp_path.glob("fill_parse_error-*.json"))) == 3

    # ...but re-sighting the SAME payloads (every backfill window re-delivers them) still
    # collapses onto the files already written.
    for _ in range(3):
        proc.ingest_message(
            {
                "channel": "userFills",
                "data": {
                    "fills": [_fill(tid=None, px="100"), _fill(tid=None, px="200"), "garbage"]
                },
            }
        )
    assert len(list(tmp_path.glob("fill_parse_error-*.json"))) == 3


def test_a_payload_that_will_not_serialise_still_cannot_crash_the_drain(db, tmp_path, clock):
    """Deriving the evidence KEY is part of recording evidence — it must be fail-soft too.

    `json.dumps(..., sort_keys=True)` raises TypeError on mixed-type dict keys. Real
    WS/REST payloads are JSON-decoded (string keys only), but a malformed payload gets no
    benefit of the doubt: if the digest input will not serialise, the key falls back to
    repr() rather than letting the exception escape `_record_malformed` — which would
    abort the batch's well-formed siblings and wedge every backfill retry on one payload.
    """
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    undumpable = {1: "mixed", "keys": "boom"}  # tid-less AND unserialisable under sort_keys
    # A well-formed sibling in the same batch: the one payload that cannot even be
    # digested must not take its batch down with it.
    sibling = _fill(tid=None, px="100")
    results = proc.ingest_message(
        {"channel": "userFills", "data": {"fills": [undumpable, sibling]}}
    )

    assert results == []  # both skipped as malformed, nothing raised
    # The undumpable payload's evidence WRITE also degrades (write_raw_payload's own
    # fail-soft: no file, a warning) — but the sibling's evidence is still kept.
    assert len(list(tmp_path.glob("fill_parse_error-*.json"))) == 1


# ---------------------------------------------------------------------------
# parse type guards — payload drift must stay in the malformed vocabulary
# ---------------------------------------------------------------------------


def test_unhashable_side_is_malformed_not_a_type_error():
    """``in`` on a dict hashes its operand: ["B"] must be malformed, not TypeError.

    A TypeError escapes both the §11.3 handlers and parse's (ValueError,
    ArithmeticError) backstop — one drifted fill would wedge the REST backfill
    forever (re-fetched every window, crashing every pass).
    """
    with pytest.raises(MalformedResponseError, match="side"):
        ExchangeFill.parse(_fill(side=["B"]))


@pytest.mark.parametrize("coin", [["BTC"], {"c": 1}, 7, ""])
def test_non_string_coin_is_malformed(coin):
    # An untyped coin would flow to the _unmapped_reason symbol comparison, always
    # mismatch, and mislabel payload drift as a §12.3 unmapped case.
    with pytest.raises(MalformedResponseError, match="coin"):
        ExchangeFill.parse(_fill(coin=coin))


def test_hand_built_fill_with_non_string_coin_is_rejected():
    fill = ExchangeFill.parse(_fill())
    with pytest.raises(ValueError, match="coin"):
        replace(fill, coin=123)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["oid", "tid"])
@pytest.mark.parametrize("value", [[123], {"id": 1}, True])
def test_non_scalar_id_is_malformed(key, value):
    # str() coerces anything without error, so a non-scalar oid would mislabel a
    # readable fill as unmapped and a non-scalar tid would mint a nonsense-but-
    # permanent dedupe key — both must be the malformed verdict instead.
    with pytest.raises(MalformedResponseError, match=key):
        ExchangeFill.parse({**_fill(), key: value})


def test_a_drifted_side_type_does_not_wedge_the_batch(db, clock, tmp_path):
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    good = _fill(tid=2)
    bad = _fill(side=["B"], tid=3)
    results = proc.ingest_message({"channel": "userFills", "data": {"fills": [bad, good]}})
    assert [r.outcome for r in results] == [IngestOutcome.APPLIED]
    assert list(tmp_path.glob("fill_parse_error-*.json"))


# ---------------------------------------------------------------------------
# §15.1 rule 8 — redelivery verification (fee corrections + identity drift)
# ---------------------------------------------------------------------------


def test_redelivered_fee_correction_posts_the_difference(db, clock, tmp_path):
    """The heartbeat's redelivery is rule 5's automatic feeder: a booked fill
    re-arriving with a different USDC fee posts the correction, exactly once."""
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc.ingest(_fill(fee="0.05", tid=7)).outcome is IngestOutcome.APPLIED
    assert _ledger(db).total_fees == Decimal("0.05")

    corrected = _fill(fee="0.03", tid=7)
    assert proc.ingest(corrected).outcome is IngestOutcome.DUPLICATE
    assert _ledger(db).total_fees == Decimal("0.03")
    assert _ledger(db).wallet_balance == Decimal("999.97")

    # Every later pass redelivers the same corrected payload: no further writes.
    assert proc.ingest(corrected).outcome is IngestOutcome.DUPLICATE
    fid = live_fill_id("r", "tid|7")
    fee_adj = [
        a
        for a in repo.iter_accounting_adjustment_events(db.conn, "r", target_id=fid)
        if a["adjustment_type"] == "fee"
    ]
    assert len(fee_adj) == 1
    # The fill row stayed immutable — the correction lives only in the adjustment.
    assert repo.get_fill(db.conn, fid)["exchange_fee"] == "0.05"


def test_redelivery_matching_the_books_writes_nothing(db, clock, tmp_path):
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(fee="0.05", tid=8)
    assert proc.ingest(raw).outcome is IngestOutcome.APPLIED
    assert proc.ingest(raw).outcome is IngestOutcome.DUPLICATE
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM exchange_reconciliation_events").fetchone()[0] == 0


def test_stale_redelivery_does_not_flip_flop_a_manual_valuation(db, clock, tmp_path):
    """The comparison baseline is the AS-INGESTED fee, not the effective fee.

    A manual valuation (rule 3's non-USDC lane) moves the effective fee; the same
    stale payload then re-arrives every heartbeat still carrying the original
    amount. Compared against the effective fee it would "correct" the valuation
    right back, every pass, forever.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(fee="0.05", tid=9)
    assert proc.ingest(raw).outcome is IngestOutcome.APPLIED
    fid = live_fill_id("r", "tid|9")
    backfill_fill_fee(
        db, run_id="r", fill_id=fid, exchange_fee=Decimal("0.07"), fee_token="USDC", source="manual"
    )
    assert _ledger(db).total_fees == Decimal("0.07")

    assert proc.ingest(raw).outcome is IngestOutcome.DUPLICATE  # same stale payload
    assert _ledger(db).total_fees == Decimal("0.07")  # the valuation stands
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 1


def test_pending_fee_resolves_from_a_redelivery(db, clock, tmp_path):
    """A pending fill's redelivery WITH a USDC fee is its first resolution."""
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc.ingest(_fill(fee=None, tid=10)).outcome is IngestOutcome.APPLIED
    fid = live_fill_id("r", "tid|10")
    assert repo.iter_live_fills(db.conn, "r", pending_fee_only=True) != []

    assert proc.ingest(_fill(fee="0.04", tid=10)).outcome is IngestOutcome.DUPLICATE
    assert _ledger(db).total_fees == Decimal("0.04")
    assert _ledger(db).wallet_balance == Decimal("999.96")
    assert repo.iter_live_fills(db.conn, "r", pending_fee_only=True) == []
    assert repo.get_fill(db.conn, fid)["exchange_fee"] is None  # row stays immutable


def test_redelivered_identity_drift_is_recorded_not_applied(db, clock, tmp_path):
    """Same tid, different money: no lane can re-book it — evidence + case row only.

    The fee is deliberately NOT posted on top of an identity mismatch: a payload
    that disagrees about which fill this is cannot be trusted about its cost.
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc.ingest(_fill(px="100", fee="0.05", tid=11)).outcome is IngestOutcome.APPLIED
    wallet_before = _ledger(db).wallet_balance

    drifted = _fill(px="101", fee="0.99", tid=11)
    assert proc.ingest(drifted).outcome is IngestOutcome.DUPLICATE
    assert _ledger(db).wallet_balance == wallet_before
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    cases = repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_money_drift")
    assert len(cases) == 1
    assert cases[0]["local_value"] == live_fill_id("r", "tid|11")
    assert list(tmp_path.glob("fill_money_drift-*.json"))

    # The same drifted payload re-arrives every pass: one record, not one per pass.
    assert proc.ingest(drifted).outcome is IngestOutcome.DUPLICATE
    assert (
        len(repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_money_drift"))
        == 1
    )
    assert len(list(tmp_path.glob("fill_money_drift-*.json"))) == 1

    # A SECOND, different drift on the same fill is new evidence — its own record
    # (the case key folds in a digest of the drift, not just the fill key).
    assert proc.ingest(_fill(px="102", tid=11)).outcome is IngestOutcome.DUPLICATE
    assert (
        len(repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_money_drift"))
        == 2
    )


def test_a_failed_case_row_write_cannot_crash_the_drain(db, clock, tmp_path, monkeypatch):
    """_record_malformed's contract: recording evidence never crashes the drain.

    The case-row INSERT is part of recording evidence, so a store that cannot take
    it (locked, corrupt) must degrade to a logged error — the §11.3 skip stands and
    the batch's well-formed siblings still apply. (The unmapped lane is deliberately
    fail-LOUD by contrast; this pins only the malformed recorder's lane.)
    """
    _live_run(db)
    _live_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "insert_exchange_reconciliation_event", boom)
    bad = _fill(tid=15)
    del bad["px"]
    good = _fill(tid=16)
    results = proc.ingest_message({"channel": "userFills", "data": {"fills": [bad, good]}})
    assert [r.outcome for r in results] == [IngestOutcome.APPLIED]  # sibling still books
    assert list(tmp_path.glob("fill_parse_error-*.json"))  # file evidence still kept


def test_cross_run_duplicate_never_touches_this_runs_ledger(db, clock, tmp_path):
    """A fill booked under another run is not this run's ledger to correct."""
    _live_run(db, run_id="r")
    _live_order(db, run_id="r")
    proc_r = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc_r.ingest(_fill(fee="0.05", tid=12)).outcome is IngestOutcome.APPLIED

    _live_run(db, run_id="s")
    proc_s = LiveFillProcessor(db=db, run_id="s", payload_dir=tmp_path, clock=clock)
    assert proc_s.ingest(_fill(fee="0.03", tid=12)).outcome is IngestOutcome.DUPLICATE
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    assert _ledger(db, run_id="r").total_fees == Decimal("0.05")
    assert _ledger(db, run_id="s").total_fees == Decimal("0")


def test_cross_run_fee_only_difference_lands_a_fee_drift_case(db, clock, tmp_path):
    """An exchange fee correction on a finished run's fill: breadcrumb, never a post.

    The fee lane cannot move another run's ledger and fee is excluded from the
    identity-drift set, so without its own §12.3 case type this difference would
    leave zero trace anywhere (§15.1 rule 8) — the predecessor run's frozen books
    would be wrong with nothing saying so.
    """
    _live_run(db, run_id="r")
    _live_order(db, run_id="r")
    proc_r = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc_r.ingest(_fill(fee="0.05", tid=31)).outcome is IngestOutcome.APPLIED

    _live_run(db, run_id="s")
    proc_s = LiveFillProcessor(db=db, run_id="s", payload_dir=tmp_path, clock=clock)

    # An agreeing redelivery is the ordinary case, and a redelivery with no
    # USDC-proven fee has nothing to teach: neither leaves a breadcrumb.
    assert proc_s.ingest(_fill(fee="0.05", tid=31)).outcome is IngestOutcome.DUPLICATE
    assert proc_s.ingest(_fill(fee=None, tid=31)).outcome is IngestOutcome.DUPLICATE
    assert repo.iter_exchange_reconciliation_events(db.conn, "s", case_type="fill_fee_drift") == []

    # A differing fee records once (per distinct amount) and posts nothing.
    differing = _fill(fee="0.03", tid=31)
    assert proc_s.ingest(differing).outcome is IngestOutcome.DUPLICATE
    assert proc_s.ingest(differing).outcome is IngestOutcome.DUPLICATE  # next pass: deduped
    cases = repo.iter_exchange_reconciliation_events(db.conn, "s", case_type="fill_fee_drift")
    assert len(cases) == 1
    assert cases[0]["local_value"] == live_fill_id("r", "tid|31")
    assert cases[0]["exchange_value"].startswith("tid|31|")
    detail = json.loads(cases[0]["detail"])
    assert detail["booked_run_id"] == "r"
    assert detail["redelivered"] == "0.03"
    assert list(tmp_path.glob("fill_fee_drift-*.json"))
    assert db.conn.execute("SELECT COUNT(*) FROM accounting_adjustment_events").fetchone()[0] == 0
    assert _ledger(db, run_id="r").total_fees == Decimal("0.05")
    assert _ledger(db, run_id="s").total_fees == Decimal("0")


def test_cross_run_fee_drift_compares_against_the_other_runs_effective_fee(db, clock, tmp_path):
    """An amount the dead run's correction chain already carries is not news.

    Mirrors the same-run lane's NET semantics (backfill_fill_fee's ALREADY_POSTED
    gate): recording a breadcrumb for a fee the other run's books already carry
    would plant evidence known to be false.
    """
    _live_run(db, run_id="r")
    _live_order(db, run_id="r")
    proc_r = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc_r.ingest(_fill(fee="0.05", tid=32)).outcome is IngestOutcome.APPLIED
    backfill_fill_fee(
        db,
        run_id="r",
        fill_id=live_fill_id("r", "tid|32"),
        exchange_fee=Decimal("0.03"),
        fee_token="USDC",
        source="manual",
    )

    _live_run(db, run_id="s")
    proc_s = LiveFillProcessor(db=db, run_id="s", payload_dir=tmp_path, clock=clock)

    # Differs from as-ingested (0.05) but matches the effective fee: no case.
    assert proc_s.ingest(_fill(fee="0.03", tid=32)).outcome is IngestOutcome.DUPLICATE
    assert repo.iter_exchange_reconciliation_events(db.conn, "s", case_type="fill_fee_drift") == []

    # Differs from both: a real breadcrumb.
    assert proc_s.ingest(_fill(fee="0.07", tid=32)).outcome is IngestOutcome.DUPLICATE
    assert (
        len(repo.iter_exchange_reconciliation_events(db.conn, "s", case_type="fill_fee_drift")) == 1
    )


def test_cross_run_pending_fill_is_not_silenced_by_the_placeholder(db, clock, tmp_path):
    """A pending fill's fee resolving to 0 is still news — same as the rule-5 exception.

    The effective-fee gate must not read the pending placeholder 0 as "already
    carried": the other run never resolved this fee at all, so ANY USDC-proven
    amount on the redelivery (including 0) is information its books do not have.
    """
    _live_run(db, run_id="r")
    _live_order(db, run_id="r")
    proc_r = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    assert proc_r.ingest(_fill(fee=None, tid=34)).outcome is IngestOutcome.APPLIED

    _live_run(db, run_id="s")
    proc_s = LiveFillProcessor(db=db, run_id="s", payload_dir=tmp_path, clock=clock)
    assert proc_s.ingest(_fill(fee="0", tid=34)).outcome is IngestOutcome.DUPLICATE
    assert (
        len(repo.iter_exchange_reconciliation_events(db.conn, "s", case_type="fill_fee_drift")) == 1
    )


# ---------------------------------------------------------------------------
# §12.3 sighting rows — the queryable backlog behind the evidence files
# ---------------------------------------------------------------------------


def test_unmapped_sighting_lands_one_case_row(db, clock, tmp_path):
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    raw = _fill(oid=555, tid=13)
    assert proc.ingest(raw).outcome is IngestOutcome.UNMAPPED
    assert proc.ingest(raw).outcome is IngestOutcome.UNMAPPED  # re-sighted next pass
    cases = repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_unmapped")
    assert len(cases) == 1
    assert cases[0]["exchange_value"] == "tid|13"
    assert cases[0]["symbol"] == "BTC"
    assert '"exchange_fill_time"' in cases[0]["detail"]  # PR 4's backfill-window locator

    # Resolution: the case rows are a log — booking the fill later leaves them in
    # place; "resolved" is defined by the anti-join against fills, not a mutation.
    _live_order(db, oid="555")
    assert proc.ingest(raw).outcome is IngestOutcome.APPLIED
    assert (
        len(repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_unmapped")) == 1
    )
    assert repo.get_fill_by_exchange_key(db.conn, "tid|13") is not None  # anti-join now misses


def test_malformed_sighting_lands_one_case_row(db, clock, tmp_path):
    _live_run(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    bad = _fill(tid=14)
    del bad["px"]
    msg = {"channel": "userFills", "data": {"fills": [bad]}}
    assert proc.ingest_message(msg) == []
    assert proc.ingest_message(msg) == []  # re-sighted next backfill pass
    cases = repo.iter_exchange_reconciliation_events(db.conn, "r", case_type="fill_malformed")
    assert len(cases) == 1
    assert cases[0]["exchange_value"] == "14"  # the bare-tid malformed evidence key
