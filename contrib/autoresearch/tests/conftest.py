"""Scripted history for the AutoResearch tests. Nothing here touches the network.

:class:`ScriptedMarket` implements
:class:`~contrib.autoresearch.ports.HistoryMarketData` over a list of bars and
a list of funding points. It is written to the PORT's contract, not to the
Hyperliquid reader's internals — "the bars closed as of ``end``, oldest
first", and nothing about SDK pagination — because that contract is what the
backfill is allowed to rely on, and a fake reproducing the reader's internals
would let the walk depend on them without anyone noticing.

The one venue behaviour it does model on purpose is the funding endpoint's
RESPONSE CAP, and it models it in the shape that matters: the endpoint is
anchored on its start, so a capped response is missing its NEWEST records.
That is the whole reason the funding walk runs forwards, so a fake truncating
the other end would have made that decision untestable.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import (
    Candle,
    FundingPoint,
    epoch_ms,
    from_epoch_ms,
    interval_to_ms,
)

MS_PER_HOUR = 60 * 60_000

# An arbitrary but fixed anchor, snapped onto the 4h grid and comfortably
# inside the decodable epoch-ms range. Fixed so every expectation in the suite
# can be written as an offset from it rather than as a magic number.
ANCHOR_MS = 1_700_000_000_000 - (1_700_000_000_000 % interval_to_ms("4h"))


def bars(
    count: int,
    *,
    start_ms: int = ANCHOR_MS,
    interval: str = "4h",
    skip: Collection[int] = (),
) -> list[Candle]:
    """``count`` consecutive bars from ``start_ms``, with the indices in ``skip`` left out.

    ``skip`` is how a test writes a series with a hole in it WITHOUT moving
    the remaining bars: every other bar keeps the stamp it would have had, so
    the gap scan is being asked about a hole rather than about a
    differently-shaped series.
    """
    step = interval_to_ms(interval)
    made = []
    for i in range(count):
        if i in skip:
            continue
        open_time = start_ms + i * step
        price = Decimal(100 + i)
        made.append(
            Candle(
                open_time=open_time,
                close_time=open_time + step,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=Decimal("1.5"),
            )
        )
    return made


def funding_points(
    count: int, *, start_ms: int = ANCHOR_MS, step_ms: int = MS_PER_HOUR
) -> list[FundingPoint]:
    """``count`` consecutive hourly funding settlements from ``start_ms``."""
    return [
        FundingPoint(
            time=start_ms + i * step_ms,
            rate=Decimal("0.00001") * (i + 1),
            premium=None if i % 2 else Decimal("0.000002"),
        )
        for i in range(count)
    ]


def candles(
    closes: Sequence[float],
    *,
    start_ms: int = ANCHOR_MS,
    interval: str = "4h",
    step_ms: int | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    opens: Sequence[float] | None = None,
) -> list[Candle]:
    """Bars whose closes — and optionally opens, highs and lows — are exactly as given.

    ``opens`` default to the closes. The evaluator tests set them apart,
    because "filled at the next OPEN" is only distinguishable from "filled at
    this CLOSE" on a bar whose two prices differ.

    The sibling of :func:`bars`, which owns its own price ramp because the
    store and gap tests care about stamps and not about prices. This one is
    for the feature tests, where the prices ARE the subject: a Donchian test
    has to set highs apart from closes to tell which series the channel was
    built from, and an arithmetic test has to know the answer by hand.

    Here rather than in either test module because both need it — and because
    ``Candle`` is a borrowed DTO this package pins against upstream drift, so
    a field renamed there should have one factory to chase, not three.
    """
    step = interval_to_ms(interval) if step_ms is None else step_ms
    made = []
    for index, close in enumerate(closes):
        price = Decimal(str(close))
        opened = Decimal(str(opens[index])) if opens else price
        made.append(
            Candle(
                open_time=start_ms + index * step,
                close_time=start_ms + (index + 1) * step,
                open=opened,
                # The default band brackets BOTH prices, so an open ten away
                # from its close is still a legal bar.
                high=Decimal(str(highs[index])) if highs else max(opened, price) + 10,
                low=Decimal(str(lows[index])) if lows else min(opened, price) - 10,
                close=price,
                volume=Decimal("1.5"),
            )
        )
    return made


@dataclass
class ScriptedMarket:
    """A venue that serves exactly the history it was handed.

    ``funding_cap`` caps how many records one funding response carries, oldest
    first — the real endpoint's shape. ``candle_calls`` / ``funding_calls``
    record every request, so a test can assert HOW the walk paged and not only
    what ended up stored: a walk that quietly re-requests the same window
    forever still lands the right rows.
    """

    clock: datetime
    candles: dict[tuple[str, str], list[Candle]] = field(default_factory=dict)
    funding: dict[str, list[FundingPoint]] = field(default_factory=dict)
    funding_cap: int | None = None
    candle_calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    funding_calls: list[tuple[str, int, int]] = field(default_factory=list)

    def get_exchange_time(self, coin: str) -> datetime:
        return self.clock

    def get_candles(
        self, coin: str, interval: str, lookback: int, *, end: datetime
    ) -> list[Candle]:
        end_ms = epoch_ms(end, what="scripted candle window end")
        self.candle_calls.append((coin, interval, lookback, end_ms))
        series = self.candles.get((coin, interval), [])
        closed = [c for c in series if c.close_time <= end_ms]
        return closed[-lookback:] if lookback else closed

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        end_ms = epoch_ms(end, what="scripted funding window end")
        start_ms = end_ms - window_days * 24 * MS_PER_HOUR
        self.funding_calls.append((coin, start_ms, end_ms))
        inside = [p for p in self.funding.get(coin, []) if start_ms <= p.time <= end_ms]
        # Capped from the START, so what a short response loses is its tail.
        return inside if self.funding_cap is None else inside[: self.funding_cap]


def market_at(last_stamp_ms: int, **kwargs) -> ScriptedMarket:
    """A :class:`ScriptedMarket` whose clock sits one second past ``last_stamp_ms``.

    The clock is what every window is cut at, so it is derived from the
    scripted history rather than from the host: a test written against
    ``datetime.now()`` would serve a different number of bars depending on
    when it ran.
    """
    return ScriptedMarket(clock=from_epoch_ms(last_stamp_ms) + timedelta(seconds=1), **kwargs)


@pytest.fixture
def store():
    """A fresh in-memory store, migrated."""
    with ResearchStore() as opened:
        yield opened
