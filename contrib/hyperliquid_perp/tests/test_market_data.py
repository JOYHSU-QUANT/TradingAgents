"""Tests for market_data helpers that need no network (interval math, meta)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import (
    HyperliquidMarketData,
    interval_to_ms,
)
from contrib.hyperliquid_perp.ports import ExchangeMarketData


def test_interval_to_ms_known_intervals():
    assert interval_to_ms("4h") == 4 * 60 * 60_000
    assert interval_to_ms("1d") == 24 * 60 * 60_000
    assert interval_to_ms("1m") == 60_000


def test_interval_to_ms_unknown_raises_valueerror():
    # A typo like "4H" (wrong case) must raise a clear ValueError rather than
    # silently selecting a wrong interval.
    with pytest.raises(ValueError):
        interval_to_ms("4H")


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
