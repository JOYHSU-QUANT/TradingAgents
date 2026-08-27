"""Tests for the books read behind the prompt's position section."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.position_facts import BookPosition, read_book_position
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState

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
    assert read_book_position(db, "r", "BTC") is None
    db.close()


def test_a_seeded_run_reads_flat_with_the_wallet_and_no_fill(tmp_path):
    db = _seeded(tmp_path)
    assert read_book_position(db, "r", "BTC") == BookPosition(
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
    book = read_book_position(db, "r", "BTC")
    assert book.size == D("-0.01")
    assert book.entry_price == D(60000)
    assert book.wallet_balance == D(1000)
    # The NEWEST fill, decoded to an aware datetime (the store's ISO form).
    assert book.last_fill_at == _T
    assert book.last_fill_at.tzinfo is not None
    db.close()
