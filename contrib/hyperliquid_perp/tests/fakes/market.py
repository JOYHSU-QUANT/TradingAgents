"""The BTC mark, margin schedule and price snapshot the engine tests run on."""

from __future__ import annotations

from decimal import Decimal

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier

MARK = Decimal(50000)


def margin_schedule(coin: str = "BTC") -> MarginSchedule:
    return MarginSchedule(coin=coin, tiers=(MarginTier(Decimal(0), Decimal(50)),))


def snap(mark=MARK, mid=MARK):
    return (Decimal(mark), Decimal(mid))
