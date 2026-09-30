"""The repository readers behind the live acceptance validator (refactor plan v2, T3-a).

``live/validation_metrics.py`` reads its counts through these instead of its
own SQL. The four predicates here survived a mutation probe against the validator
suite alone (2026-09-30): the verdict tests stage one run, one fill per
order and one attempt per cloid, so a reader that forgot its run filter, its
DISTINCT or its ``action = 'place'`` still passed them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from ..conftest import insert_decision_attempts

_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    with Database(":memory:") as database:
        yield database


def _attempt(conn, *, attempt_id: str, run_id: str, at: datetime, status: str) -> None:
    repo.insert_decision_attempt(
        conn,
        decision_attempt_id=attempt_id,
        timestamp=at,
        mode="live",
        run_id=run_id,
        scheduled_at=at,
        attempt_count=1,
        status=status,
        error_type=None,
    )


def _fill(conn, *, fill_id: str, run_id: str) -> None:
    repo.insert_fill(
        conn,
        fill_id=fill_id,
        mode="paper",
        run_id=run_id,
        order_id="o1",
        symbol="BTC",
        side="buy",
        fill_qty=Decimal("0.01"),
        fill_price=Decimal("60000"),
        fill_notional=Decimal("600"),
        fee=Decimal("0.27"),
        fee_rate=Decimal("0.00045"),
        realized_pnl_delta=Decimal("0"),
        timestamp=_T0,
    )


def _place(
    conn,
    *,
    attempt_id: str,
    cloid: int,
    index: int,
    action: str,
    status: str,
    exchange_order_id: str | None = None,
) -> None:
    repo.insert_live_order_attempt(
        conn,
        attempt_id=attempt_id,
        run_id="r",
        action=action,
        symbol="BTC",
        attempt_index=index,
        status=status,
        exchange_order_id=exchange_order_id,
        cloid_logical=f"smoke_r_BTC_o_p_na_000_entry_{cloid}",
        cloid_hex=f"0x{cloid:032x}",
        side="buy",
        qty=Decimal("0.001"),
        price=Decimal(60000),
        reduce_only=False,
        order_role="entry",
        requested_at=_T0,
    )


def test_in_progress_attempts_come_oldest_first_by_state_change_not_by_insertion(db):
    # The validator names the OLDEST stranded attempt and ages the run by it;
    # ``timestamp`` is when the row last changed state, and insertion order
    # (rowid) only breaks ties.
    with db.transaction() as conn:
        _attempt(conn, attempt_id="newer", run_id="r", at=_T0 + timedelta(hours=8), status="in_progress")
        _attempt(conn, attempt_id="older", run_id="r", at=_T0, status="in_progress")
        _attempt(conn, attempt_id="done", run_id="r", at=_T0 + timedelta(hours=4), status="completed")
        _attempt(conn, attempt_id="elsewhere", run_id="other", at=_T0, status="in_progress")
    rows = repo.iter_in_progress_attempts(db.conn, "r")
    assert [row["decision_attempt_id"] for row in rows] == ["older", "newer"]


def test_count_decision_attempts_is_run_scoped_and_refuses_an_unknown_status(db):
    insert_decision_attempts(db, ["completed", "api_failed"], run_id="r", start=_T0, mode="live")
    insert_decision_attempts(db, ["completed"], run_id="other", start=_T0, mode="live")
    assert repo.count_decision_attempts(db.conn, "r", statuses=("completed",)) == 1
    assert repo.count_decision_attempts(db.conn, "r", statuses=("completed", "api_failed")) == 2
    with pytest.raises(ValueError, match="status"):
        repo.count_decision_attempts(db.conn, "r", statuses=("finished",))


def test_count_fills_is_run_scoped(db):
    with db.transaction() as conn:
        _fill(conn, fill_id="f1", run_id="r")
        _fill(conn, fill_id="f2", run_id="r")
        _fill(conn, fill_id="f3", run_id="other")
    assert repo.count_fills(db.conn, "r") == 2


def test_count_exchange_known_place_cloids_counts_orders_not_round_trips(db):
    # One order resent under §8.3 is two acknowledged place attempts on one
    # cloid; a cancel round-trip and a place the exchange never took are not
    # orders the exchange holds.
    with db.transaction() as conn:
        _place(conn, attempt_id="p0", cloid=1, index=0, action="place", status="acknowledged")
        _place(conn, attempt_id="p1", cloid=1, index=1, action="place", status="duplicate")
        _place(conn, attempt_id="p2", cloid=2, index=0, action="place", status="acknowledged")
        _place(
            conn,
            attempt_id="c0",
            cloid=3,
            index=0,
            action="cancel",
            status="acknowledged",
            exchange_order_id="7",
        )
        _place(conn, attempt_id="p3", cloid=4, index=0, action="place", status="submitted")
    assert repo.count_exchange_known_place_cloids(db.conn, "r") == 2
