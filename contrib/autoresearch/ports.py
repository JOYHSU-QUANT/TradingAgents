"""The outward seam this package fetches history through.

One protocol, because there is one direction of traffic: AutoResearch reads
public market history and writes it to its own store. Nothing here faces the
other way — this package signs nothing and places no orders.

:class:`HistoryMarketData` is a structural SUPERSET of the perp package's
``ExchangeMarketData``: the two windowed reads with the same signatures, plus
``get_exchange_time``. The clock is on the port rather than looked up beside
it because of what the backfill does with it — every page's ``end`` is cut at
the venue's clock, never the host's (issue #124's discipline, restated in
plan §3.4), so a double that scripts the history has to script the clock that
bounds it as well. A fake that answers candles from the host's clock would
pass a narrower port and quietly test a window this package never asks for.

FAILURE IS A TYPE here for the same reason it is upstream: an implementation
says the VENUE failed by raising ``ExchangeError`` (or a subclass), and the
backfill catches that family and nothing wider. A bug in a scripted feed must
not be able to impersonate an outage — it would be written to the store as a
short history and then read back as a market that had not listed yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .upstream import Candle, FundingPoint


@runtime_checkable
class HistoryMarketData(Protocol):
    """Read-only public market history, windowed by the venue's own clock."""

    def get_exchange_time(self, coin: str) -> datetime:
        """The VENUE's clock, as an aware UTC datetime."""
        ...

    def get_candles(
        self, coin: str, interval: str, lookback: int, *, end: datetime
    ) -> list[Candle]:
        """Up to ``lookback`` ``interval`` candles CLOSED as of ``end``, oldest first."""
        ...

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        """Funding observations over the ``window_days`` trailing ``end``, oldest first."""
        ...
