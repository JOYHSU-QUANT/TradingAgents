"""Tests for the one books read behind the prompt's position section and the audit row."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.position_facts import BookFacts, BookPosition, read_books
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import AccountLedger, PositionState

D = Decimal
_T = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _seeded(tmp_path):
    db = Database(tmp_path / "p.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return db


def test_no_books_yet_reads_as_none_not_as_flat(tmp_path):
    # A fresh run's provider is built before initialize_run: the read must
    # say "no books" (section omitted), never fabricate a flat account.
    db = Database(tmp_path / "p.db")
    assert read_books(db, "r", "BTC") is None
    db.close()


def test_a_seeded_run_reads_flat_with_the_wallet_and_no_fill(tmp_path):
    db = _seeded(tmp_path)
    books = read_books(db, "r", "BTC")
    assert books == BookFacts(
        ledger=AccountLedger(wallet_balance=D(1000)),
        position=PositionState.flat("BTC"),
        last_fill_time=None,
    )
    assert books.position_facts == BookPosition(
        size=D(0), entry_price=None, wallet_balance=D(1000), last_fill_at=None
    )
    db.close()


def test_an_open_position_reads_its_size_entry_and_newest_fill(tmp_path):
    db = _seeded(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn, "r", PositionState(coin="BTC", size=D("-0.01"), entry_price=D(60000))
        )
        for i, ts in enumerate((_T.replace(hour=8), _T)):
            repo.insert_fill(
                conn,
                fill_id=f"f{i}",
                mode="paper",
                run_id="r",
                order_id=f"o{i}",
                symbol="BTC",
                side="sell",
                fill_qty=D("0.005"),
                fill_price=D(60000),
                fill_notional=D(300),
                fee=D("0.135"),
                fee_rate=D("0.00045"),
                realized_pnl_delta=D(0),
                timestamp=ts,
            )
    books = read_books(db, "r", "BTC")
    assert books.position.size == D("-0.01")
    assert books.position.entry_price == D(60000)
    assert books.ledger.wallet_balance == D(1000)
    # The NEWEST fill, in the store's own form — the bytes ai_inputs.last_fill_time
    # carries — and decoded to an aware datetime on the prompt-side view.
    assert books.last_fill_time == repo.last_fill_time(db.conn, "r")
    facts = books.position_facts
    assert (facts.size, facts.entry_price, facts.wallet_balance) == (D("-0.01"), D(60000), D(1000))
    assert facts.last_fill_at == _T
    assert facts.last_fill_at.tzinfo is not None
    db.close()
