"""The two outward seams this package reaches through: history in, hypotheses in.

Neither faces the other way — this package signs nothing and places no orders.
:class:`HistoryMarketData` reads public market history into the store;
:class:`Hypothesist` asks a model for one hypothesis. What comes back through
the second is TEXT, and it is treated as text: it is parsed by the same closed
parser an operator's hand-written spec goes through, so a model cannot widen
the language by writing confidently.

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

__all__ = ["HistoryMarketData", "Hypothesist", "HypothesistError"]


class HypothesistError(RuntimeError):
    """The model seam failed: no answer came back, so there is no proposal.

    The counterpart of ``ExchangeError`` on the other port, and it exists for
    the same reason — a transport failure must not be able to impersonate a
    result. A refused SPEC is a result: the model answered, the answer was not
    a rule, and the round spends its budget and files the refusal. An outage,
    a bad key or a truncated stream is not an answer at all, so it spends
    nothing and stops the run; charging budget for it would let a broken key
    quietly exhaust a run's trials and report a search that never happened.
    """


@runtime_checkable
class Hypothesist(Protocol):
    """Something that answers a prompt with one strategy spec as JSON text.

    Two messages rather than one string, because that is the shape the chat
    models behind it take: the language and the vocabulary are the same every
    round and belong in ``system``, while what changed — the rules already
    tried and why they failed — is ``user``.

    The return is the model's raw text, NOT a parsed spec. Parsing belongs to
    :mod:`~contrib.autoresearch.dsl` on this side of the seam, so an
    implementation cannot decide what counts as a valid hypothesis, and a fake
    in a test cannot be more permissive than the real parser.
    """

    def propose(self, system: str, user: str) -> str:
        """One answer, as text. Raises :class:`HypothesistError` if the seam failed."""
        ...


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
