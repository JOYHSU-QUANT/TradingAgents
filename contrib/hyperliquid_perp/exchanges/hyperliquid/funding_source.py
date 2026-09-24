"""Funding rates for past settlement hours, read from the public fundingHistory endpoint.

The production :class:`~...ports.FundingSource`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ...common.instants import from_epoch_ms
from .errors import ExchangeError

logger = logging.getLogger(__name__)


class HistoryFundingSource:
    """Funding rates from the public fundingHistory endpoint (execution §6.5).

    Serves the engine's hourly settlements and the pending-event backfill
    (restart + every cycle boundary). Responses are cached briefly so a
    backfill loop over many pending hours does not re-fetch per event; the
    fetch window widens to cover however old the requested settlement is, so a
    long-pending event can always resolve. A missing hour returns ``None``
    (the caller records/keeps a ``pending`` event — never a fabricated rate).
    """

    _MIN_WINDOW_DAYS = 7
    _CACHE_TTL_SECONDS = 900
    # After this many consecutive fetch failures the log escalates to ERROR: a
    # chronic integration break (auth, endpoint drift) must read differently
    # from the ordinary "rate not published yet" warning it otherwise mimics —
    # events would pile up pending forever behind an easy-to-miss line.
    _FAILURE_ESCALATION_THRESHOLD = 3

    def __init__(self, market) -> None:
        self._market = market
        # coin -> (fetched_at_monotonic, window_days_fetched, {hour: rate})
        self._cache: dict[str, tuple[float, int, dict[datetime, object]]] = {}
        self._consecutive_failures: dict[str, int] = {}

    def rate_at(self, coin: str, funding_timestamp: datetime):
        hour = funding_timestamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        now = datetime.now(timezone.utc)
        age = now - hour
        needed_days = max(self._MIN_WINDOW_DAYS, age.days + 2)
        cached = self._cache.get(coin)
        if (
            cached is None
            or time.monotonic() - cached[0] > self._CACHE_TTL_SECONDS
            or cached[1] < needed_days
        ):
            try:
                # The HOST clock cuts this window, deliberately — the one
                # windowed read that does not take the exchange's (issue
                # #124). This looks up a PAST hour, and the only thing a host
                # clock offset can do to it is a miss: a host behind by S has
                # no points for the last S, so a fresh hour reads ``None``
                # and the event stays pending until a later poll — never a
                # wrong rate. Reading the exchange's clock here would add a
                # REST call per refresh to buy nothing the caller can use.
                points = self._market.get_funding_history(coin, needed_days, end=now)
            except ExchangeError as exc:
                # A VENUE failure means "pending" — the endpoint refused,
                # throttled, or answered malformed; the event waits for a
                # later poll. Only that family is caught: a ``TypeError``
                # from a call site that drifted from the reader's signature,
                # or a ``ValueError`` from a naive ``end``, is a programmer
                # error and propagates out of this lookup — counted as a
                # fetch failure it would log three WARNINGs and an ERROR that
                # read as an outage, while every settlement stayed pending
                # forever (issue #157). What the callers make of it is theirs:
                # the engine tick lets it end the run; the cycle-boundary
                # backfill contains it in a lane of its own, which says the
                # READER failed rather than blaming the stored row (issue
                # #193 — it used to land in the corrupt-row lane and send an
                # operator to SQLite to hunt a fault that is in the code).
                failures = self._consecutive_failures.get(coin, 0) + 1
                self._consecutive_failures[coin] = failures
                log = (
                    logger.error
                    if failures >= self._FAILURE_ESCALATION_THRESHOLD
                    else logger.warning
                )
                log(
                    "funding history fetch failed for %s (%d consecutive): %s",
                    coin,
                    failures,
                    exc,
                )
                return None
            self._consecutive_failures.pop(coin, None)
            by_hour = {}
            for point in points:
                stamp = from_epoch_ms(point.time)
                by_hour[stamp.replace(minute=0, second=0, microsecond=0)] = point.rate
            cached = (time.monotonic(), needed_days, by_hour)
            self._cache[coin] = cached
        return cached[2].get(hour)
