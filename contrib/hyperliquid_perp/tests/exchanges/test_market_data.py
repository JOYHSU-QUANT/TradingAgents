"""Tests for market_data helpers that need no network (meta, candle window)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import MalformedResponseError
from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import HyperliquidMarketData
from contrib.hyperliquid_perp.ports import ExchangeMarketData

from ..conftest import synthetic_bar as _settled_bar


class _MetaOnlyInfo:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def meta_and_asset_ctxs(self):
        self.calls += 1
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.info = _MetaOnlyInfo(payload)


def test_get_asset_meta_pair_from_one_meta_request(meta_and_asset_ctxs):
    # AssetSpec's two exchange-derived inputs come from the SAME meta response —
    # one request, so the (szDecimals, schedule) pair can never be internally
    # inconsistent.
    client = _FakeClient(meta_and_asset_ctxs)
    market = HyperliquidMarketData(client)
    sz_decimals, schedule = market.get_asset_meta("BTC")
    assert client.info.calls == 1
    assert sz_decimals == 5  # BTC's szDecimals, not ETH's 4
    # No tier table in the fixture -> single tier from BTC's maxLeverage.
    assert schedule == MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))


def test_market_data_satisfies_exchange_market_data_port():
    # Method-presence check only (``runtime_checkable``): a rename/removal on
    # either side of the port fails here rather than in a paper run.
    assert issubclass(HyperliquidMarketData, ExchangeMarketData)


# ---------------------------------------------------------------------------
# identity echo wiring (2026-08-17): the client must ASK for the check
# ---------------------------------------------------------------------------
#
# map_candles / map_funding_history only verify identity when the caller passes
# the expected values (None skips, for identity-agnostic fixtures). These tests
# pin that the production reader actually passes them — the check being optional
# at the mapper makes THIS wiring the load-bearing part.


class _CandleInfo:
    def __init__(self, bars=None, funding=None):
        self._bars = bars or []
        self._funding = funding or []

    def candles_snapshot(self, coin, interval, start, end):
        return self._bars

    def funding_history(self, coin, start, end):
        return self._funding


def test_get_candles_rejects_a_misrouted_response():
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar(s="ETH")])
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_candles("BTC", "1m", 1)


def test_get_candles_accepts_the_matching_identity():
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar()])
    candles = HyperliquidMarketData(client).get_candles("BTC", "1m", 1)
    assert len(candles) == 1


def test_get_funding_history_rejects_a_misrouted_response():
    client = _FakeClient(None)
    client.info = _CandleInfo(
        funding=[{"coin": "ETH", "time": 1000, "fundingRate": "0.0001", "premium": "0"}]
    )
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_funding_history("BTC", 7)


def test_get_funding_history_accepts_the_matching_identity():
    # The positive companion the reject test cannot stand in for: its ``match=``
    # names the ECHOED value, so it keeps raising even when the call site passes
    # a mangled expectation. Mutating expected_coin to ``coin + "-PERP"`` --
    # which would blow up every real funding read -- left 76 tests green without
    # this one (2026-08-17 exit-sweep mutation probe).
    client = _FakeClient(None)
    client.info = _CandleInfo(
        funding=[{"coin": "BTC", "time": 1000, "fundingRate": "0.0001", "premium": "0"}]
    )
    assert len(HyperliquidMarketData(client).get_funding_history("BTC", 7)) == 1


class _BookInfo:
    def __init__(self, book):
        self._book = book
        self.asked = []

    def l2_snapshot(self, coin):
        self.asked.append(coin)
        return self._book


def test_get_exchange_time_reads_the_l2book_stamp_for_the_coin():
    # Issue #51: the exchange clock comes off the public l2Book snapshot —
    # requested for the coin (so the answer carries an identity to check) and
    # mapped to an aware UTC datetime at ms precision.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "BTC", "time": 1787369175468, "levels": [[], []]})
    when = HyperliquidMarketData(client).get_exchange_time("BTC")
    assert client.info.asked == ["BTC"]
    assert when.tzinfo is not None
    assert int(when.timestamp() * 1000) == 1787369175468


def test_get_exchange_time_rejects_a_misrouted_response():
    # Same identity-echo discipline as the other two reads, and the same
    # mutation-probed wiring: the mapper only checks when the reader asks.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "ETH", "time": 1787369175468, "levels": [[], []]})
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_exchange_time("BTC")


def test_get_exchange_time_fails_closed_without_a_stamp():
    # Fail-closed (decided 2026-08-22): the stamp is the guard's only clock, so
    # a book without one is a malformed answer, not a skipped check.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "BTC", "levels": [[], []]})
    with pytest.raises(MalformedResponseError, match="'time' is unusable"):
        HyperliquidMarketData(client).get_exchange_time("BTC")


def test_get_candles_rejects_a_response_for_the_wrong_interval():
    # The interval half of the wiring, pinned separately: the coin test's bar
    # carries the RIGHT interval, so its raise proves nothing about ``i``.
    # Dropping expected_interval from the call site left 76 tests green
    # (2026-08-17 exit-sweep mutation probe), and a 4h series read as 1m
    # rescales every indicator -- this is not the cheap half.
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar(i="4h")])
    with pytest.raises(MalformedResponseError, match="carries interval '4h'"):
        HyperliquidMarketData(client).get_candles("BTC", "1m", 1)
