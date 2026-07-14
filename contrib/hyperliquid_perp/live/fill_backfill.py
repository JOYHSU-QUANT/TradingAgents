"""REST fill backfill: catch whatever the WebSocket missed (phase3-spec §11.2 / §14.1).

The WebSocket is the low-latency source, not the only one (§11.2 rules 1–2): it
cannot deliver a fill that happened before it was subscribed or while it was
down. :class:`FillBackfiller` polls REST ``userFillsByTime`` over a trailing
window and feeds every fill through the same :class:`~.fills.LiveFillProcessor` the
WS drain uses — so a fill already applied from the socket is a no-op here (the
§14.2 dedupe key), and one the socket missed is applied exactly once.

Runs at the three §11.2 moments: startup (catch fills from before the process
came up), after every reconnect (catch the outage gap — driven by the stream's
``needs_backfill`` flag), and on the periodic heartbeat (a safety net against a
silently-dropped WS event). The trailing window makes it self-healing: the
overlap re-fetches recent fills every time, and the dedupe key absorbs the
repeats, so no high-water cursor has to be persisted or kept exactly right.

Downtime longer than ``lookback_seconds`` is the one gap this cannot close on its
own — but a WS disconnect trips safe mode at 5 minutes (§11.2 rule 7) and PR 4's
reconciliation catches any position drift, so the window only has to cover the
normal reconnect case, not an unbounded outage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..exchanges.hyperliquid.errors import MalformedResponseError
from ..paper.clock import Clock, WallClock
from .fills import IngestOutcome, LiveFillProcessor
from .ws_stream import USER_FILLS_CHANNEL

__all__ = ["DEFAULT_LOOKBACK_SECONDS", "BackfillSummary", "FillBackfiller"]

logger = logging.getLogger(__name__)

# The trailing window width. Generous next to the normal reconnect case (a WS
# drop trips safe mode at 5 minutes — §11.2 rule 7) so an outage is fully
# re-covered, and cheap because the dedupe key absorbs the overlap. Six hours
# comfortably spans one 4-hour decision cycle plus slack.
DEFAULT_LOOKBACK_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class BackfillSummary:
    """What one :meth:`FillBackfiller.backfill` pass did.

    ``fetched`` is how many fills the REST window returned; the rest partition
    them by outcome. ``malformed`` fills had their raw payload recorded and were
    skipped (§11.3), never applied.
    """

    fetched: int
    applied: int
    duplicate: int
    unmapped: int
    malformed: int

    @property
    def new_fills(self) -> int:
        return self.applied


class FillBackfiller:
    """Poll REST ``userFillsByTime`` over a trailing window into a processor.

    ``fetch`` is the injected REST seam — ``(start_ms, end_ms) -> list[fill dict]``
    — bound in production to the wallet's ``user_fills_by_time`` and in tests to a
    fake, so the whole thing runs without a network. ``processor`` is the SAME
    :class:`LiveFillProcessor` the WS drain feeds, which is what makes WS and REST
    converge on one exactly-once ledger.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[int, int], Any],
        processor: LiveFillProcessor,
        clock: Clock | None = None,
        lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
    ) -> None:
        if lookback_seconds <= 0:
            raise ValueError(f"lookback_seconds must be > 0, got {lookback_seconds}")
        self._fetch = fetch
        self._processor = processor
        self._clock = clock or WallClock()
        self._lookback = lookback_seconds

    def backfill(self, now: datetime | None = None) -> BackfillSummary:
        """Fetch the trailing window and apply every new fill (dedupe-safe).

        A transport failure PROPAGATES: the caller (the engine loop) leaves the
        stream's ``needs_backfill`` flag set so the next tick retries — swallowing
        it here would silently declare the gap closed when it was not. Malformed
        fills inside a successful response are recorded and skipped (§11.3), not
        raised, since one bad fill must not abandon the rest of the window.
        """
        stamp = now or self._clock.now()
        start_ms = int((stamp - timedelta(seconds=self._lookback)).timestamp() * 1000)
        end_ms = int(stamp.timestamp() * 1000)
        raw = self._fetch(start_ms, end_ms)
        if not isinstance(raw, list):
            # Anything that is not a list — INCLUDING ``None`` — is a malformed or
            # error payload the SDK let through, not "no fills". Fail loud rather
            # than read it as empty: an empty result declares the reconnect /
            # heartbeat gap CLOSED (the caller clears ``needs_backfill``), so
            # coercing a null response to [] would silently swallow exactly the
            # gap this module exists to close. Same null-vs-empty stance as
            # ``map_candles`` / ``map_funding_history`` ("a null payload is an
            # anomaly, not 'no data'").
            raise MalformedResponseError(
                f"userFillsByTime returned {type(raw).__name__}, expected a list: {raw!r}"
            )
        # Reuse the WS drain's per-fill §11.3 handling by wrapping the REST list in
        # a userFills envelope — one code path for "apply a batch of fills, skipping
        # malformed ones", whether they arrived over the socket or over REST.
        results = self._processor.ingest_message(
            {"channel": USER_FILLS_CHANNEL, "data": {"fills": raw}}
        )
        applied = sum(1 for r in results if r.outcome is IngestOutcome.APPLIED)
        duplicate = sum(1 for r in results if r.outcome is IngestOutcome.DUPLICATE)
        unmapped = sum(1 for r in results if r.outcome is IngestOutcome.UNMAPPED)
        # ingest_message returns one result per PARSED fill and silently records +
        # skips the malformed ones, so the shortfall is exactly the malformed count.
        malformed = len(raw) - len(results)
        summary = BackfillSummary(
            fetched=len(raw),
            applied=applied,
            duplicate=duplicate,
            unmapped=unmapped,
            malformed=malformed,
        )
        if summary.fetched:
            logger.info(
                "fill backfill: %d fetched, %d new, %d duplicate, %d unmapped, %d malformed",
                summary.fetched,
                summary.applied,
                summary.duplicate,
                summary.unmapped,
                summary.malformed,
            )
        return summary
