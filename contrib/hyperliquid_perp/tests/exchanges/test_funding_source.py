"""Tests for the funding source the daemons read settled funding through."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.funding_source import HistoryFundingSource


def test_history_funding_source_escalates_after_consecutive_failures(caplog):
    """A chronic funding-history break logs ERROR, not an eternal WARNING."""
    import logging

    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError

    class _BrokenMarket:
        def get_funding_history(self, coin, days, *, end):
            raise ExchangeRequestError("endpoint gone")

    source = HistoryFundingSource(_BrokenMarket())
    threshold = source._FAILURE_ESCALATION_THRESHOLD
    when = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.exchanges.hyperliquid.funding_source"):
        for _ in range(threshold):
            assert source.rate_at("BTC", when) is None
    failures = [r for r in caplog.records if "funding history fetch failed" in r.getMessage()]
    levels = [r.levelno for r in failures]
    assert len(levels) == threshold
    assert all(lv == logging.WARNING for lv in levels[:-1])
    assert levels[-1] == logging.ERROR
    # The failure counted must be the fake's — a venue error, the one family
    # ``rate_at`` records as "pending". Anything else propagates (next test).
    assert all("endpoint gone" in r.getMessage() for r in failures)


class _DriftedMarket:
    def get_funding_history(self, coin, days):  # no ``end`` keyword
        return []


class _RefusingMarket:
    def get_funding_history(self, coin, days, *, end):
        raise ValueError("funding history window end must be timezone-aware (UTC)")


@pytest.mark.parametrize(
    ("market", "error"), [(_DriftedMarket(), TypeError), (_RefusingMarket(), ValueError)]
)
def test_history_funding_source_lets_a_programmer_error_through(market, error):
    """Only the venue's failures are "pending"; a caller bug propagates (issue #157).

    A ``TypeError`` from a call site that drifted from the reader's signature
    (here: a reader without the ``end`` keyword) and a ``ValueError`` from
    the reader's own naive-clock refusal are not fetch failures. Swallowed,
    each would log three WARNINGs and an ERROR that read as an endpoint
    outage while every settlement stayed pending forever — and the failure
    counter must not have moved, so the bug is not mistaken for an outage
    once it is fixed and the next fetch succeeds.
    """

    source = HistoryFundingSource(market)
    with pytest.raises(error):
        source.rate_at("BTC", datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc))
    assert source._consecutive_failures == {}


def test_history_funding_source_buckets_points_by_the_hour_of_their_stamp():
    """The happy path: a returned point is found under the hour its stamp falls in.

    The venue stamps a settlement as epoch ms; the lookup key is that stamp's
    whole hour (a settlement is published on the hour, but the bucket must
    not depend on it), and an hour with no point is ``None`` (pending), not a
    neighbour's rate. Pinned on stamps built by the same integer arithmetic
    the lookup decodes with — one at the hour, one at its last millisecond.
    """
    from contrib.hyperliquid_perp.common.instants import epoch_ms

    noon = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    one_pm = noon + timedelta(hours=1)
    last_ms_of_one_pm = one_pm + timedelta(minutes=59, seconds=59, milliseconds=999)
    points = [
        SimpleNamespace(time=epoch_ms(noon, what="x"), rate=Decimal("0.0001")),
        SimpleNamespace(time=epoch_ms(last_ms_of_one_pm, what="x"), rate=Decimal("-0.0002")),
    ]

    class _Market:
        def get_funding_history(self, coin, days, *, end):
            return points

    source = HistoryFundingSource(_Market())
    assert source.rate_at("BTC", noon) == Decimal("0.0001")
    assert source.rate_at("BTC", one_pm) == Decimal("-0.0002")
    assert source.rate_at("BTC", noon - timedelta(hours=1)) is None
    assert source._consecutive_failures == {}


def test_history_funding_source_survives_an_out_of_range_stamp_in_the_window():
    """Issue #191 end to end: one absurd stamp costs its hour, not the run.

    The seam where the run used to die. This lookup decodes EVERY point of the
    fetched window, outside its own ``except ExchangeError``, so a venue
    ``"time"`` in nanoseconds raised ``OverflowError`` right here — through
    the engine's ``@_fail_stop`` funding loop (engine halted, daemon exit 2,
    no shutdown export) and through the backfill pass's corrupt lane, which
    catches ``ValueError`` and ``InvalidOperation`` but not that.

    Driven through the REAL mapper, not a hand-built point list: the fix works
    by making the boundary refuse the stamp as a ``MalformedResponseError`` —
    the second arm of the per-point ``except (ValueError,
    MalformedResponseError)`` that already drops and counts bad points — and a
    fake that skipped the mapper would skip the fix too. The poisoned hour reads ``None`` — pending, retried next pass —
    and the good hour in the same response still resolves.
    """
    from contrib.hyperliquid_perp.common.instants import epoch_ms
    from contrib.hyperliquid_perp.exchanges.hyperliquid import mapper

    noon = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    one_pm = noon + timedelta(hours=1)
    raw = [
        {"time": epoch_ms(noon, what="x"), "fundingRate": "0.0001", "coin": "BTC"},
        # Nanoseconds where milliseconds belong — venue drift, or any integer
        # past ``datetime``'s range.
        {"time": "1788163200000000000", "fundingRate": "-0.0002", "coin": "BTC"},
    ]

    class _DriftingMarket:
        def get_funding_history(self, coin, days, *, end):
            return mapper.map_funding_history(raw, expected_coin=coin, max_drop_fraction=1.0)

    source = HistoryFundingSource(_DriftingMarket())
    assert source.rate_at("BTC", noon) == Decimal("0.0001")
    assert source.rate_at("BTC", one_pm) is None  # pending, not a crashed run
    # Not a venue failure either: the counter must not escalate toward the
    # ERROR that reads as an endpoint outage.
    assert source._consecutive_failures == {}


def test_history_funding_source_hands_its_own_clock_to_the_window_end():
    """The ``end=`` wiring of the one deliberately host-clocked windowed read.

    Pinned on the HAPPY path with a recording fake. Issue #124: this read
    looks up a PAST hour, so it passes the host's clock rather than fetching
    the exchange's; that clock must be tz-aware and current, and a miss must
    be a quiet ``None`` (pending), not a counted failure.
    """

    calls = []

    class _RecordingMarket:
        def get_funding_history(self, coin, days, *, end):
            calls.append((coin, days, end))
            return []

    source = HistoryFundingSource(_RecordingMarket())
    before = datetime.now(timezone.utc)
    assert source.rate_at("BTC", datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)) is None
    after = datetime.now(timezone.utc)
    assert len(calls) == 1
    coin, days, end = calls[0]
    assert coin == "BTC" and days >= source._MIN_WINDOW_DAYS
    assert end.tzinfo is not None and before <= end <= after
    assert source._consecutive_failures == {}
