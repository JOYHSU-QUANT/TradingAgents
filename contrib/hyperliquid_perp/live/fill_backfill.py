"""REST fill backfill: catch whatever the WebSocket missed (phase3-spec §11.2 / §14.1).

The WebSocket is the low-latency source, not the only one (§11.2 rules 1–2): it
cannot deliver a fill that happened before it was subscribed or while it was
down. (In the v1 live loop no socket is attached yet — PR 6 connects the
stream — so until then this backfill is the SOLE fills path, not a safety
net.) :class:`FillBackfiller` polls REST ``userFillsByTime`` over a trailing
window and feeds every fill through the same :class:`~.fills.LiveFillProcessor` the
WS drain uses — so a fill already applied from the socket is a no-op here (the
§14.2 dedupe key), and one the socket missed is applied exactly once.

Runs at the three §11.2 moments: startup (catch fills from before the process came
up), after every reconnect (catch the outage gap — driven by the stream's
``needs_backfill`` flag), and on the periodic heartbeat (a safety net against a
silently-dropped WS event). The overlap re-fetches recent fills every time and the
dedupe key absorbs the repeats, so a pass is always safe to repeat.

The window's start is the trailing lookback OR the caller's ``since``, whichever is
EARLIER. A fixed trailing window alone would silently fail the one case that matters:
down for longer than the window — an overnight outage, a bad deploy, a restart loop —
and the fills older than it are ingested by no path at all, with no error and no gap
reported. ``since`` is ``LiveWsStream.backfill_since()`` — the stream's earliest
still-uncovered obligation (startup floor folded with any reconnect gap anchor); the
lookback then just adds a generous overlap on top. Deriving it here from the fills
table instead would look right and be wrong — see ``_window_start``.

The window is also PAGED, since the endpoint caps a response: a full page is a
truncated view, not a complete one, and a pass that cannot finish walking its pages
says so (``BackfillSummary.complete``) instead of letting the caller mark the gap
closed.
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

__all__ = [
    "DEFAULT_LOOKBACK_SECONDS",
    "DEFAULT_MAX_PAGES",
    "RESPONSE_FILL_CAP",
    "BackfillSummary",
    "FillBackfiller",
]

logger = logging.getLogger(__name__)

# The trailing window width — a FLOOR on how far back a pass reads, not a ceiling:
# the cursor pulls the start further back whenever the last booked fill is older
# (see FillBackfiller._window_start). Generous next to the normal reconnect case (a
# WS drop trips safe mode at 5 minutes — §11.2 rule 7), and cheap because the dedupe
# key absorbs the overlap. Six hours comfortably spans one 4-hour decision cycle plus
# slack, so a routine pass re-reads recent fills and finds them all already applied.
DEFAULT_LOOKBACK_SECONDS = 6 * 60 * 60

# Hyperliquid caps a ``userFillsByTime`` response at 2000 fills. A page that comes
# back FULL is therefore a truncated view of its window, not the whole of it — the
# signal to page again rather than to conclude the window is covered.
RESPONSE_FILL_CAP = 2000

# How many capped pages one pass will walk before giving up and reporting the window
# uncovered (``BackfillSummary.complete = False``). Bounded so a pathological cursor
# cannot spin forever inside one tick; 2000 x 20 = 40k fills is far beyond any real
# gap for this bot, so hitting it means something is wrong, and saying so is better
# than looping.
DEFAULT_MAX_PAGES = 20


def _require_aware(value: datetime, *, name: str) -> None:
    """Reject a naive datetime at the boundary (every instant here is aware UTC)."""
    if value.tzinfo is None:
        raise ValueError(f"backfill {name!r} must be timezone-aware (UTC), got {value!r}")


def _newest_fill_ms(fills: list[Any]) -> int | None:
    """The newest ``time`` (epoch ms) in a REST page, or ``None`` if none is readable.

    Paging resumes from here. Malformed entries are ignored rather than fatal — they
    are recorded and skipped by the ingester (§11.3), and a page of nothing BUT
    malformed fills simply cannot advance the cursor, which the caller treats as
    "window not covered" instead of paging on a guess.
    """
    newest: int | None = None
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        try:
            stamp = int(fill["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


@dataclass(frozen=True)
class BackfillSummary:
    """What one :meth:`FillBackfiller.backfill` pass did.

    ``fetched`` is how many fills the REST window returned; the rest partition them
    by outcome. ``malformed`` fills had their raw payload recorded and were skipped
    (§11.3), never applied.

    ``complete`` is the field the CALLER must act on. False means the pass could not
    prove it covered its window — the exchange capped the response and the page budget
    ran out — so the gap is still open and ``needs_backfill`` must NOT be cleared.
    Reporting "fetched N" on a truncated response and letting the caller move on would
    declare closed exactly the gap this module exists to close.
    """

    fetched: int
    applied: int
    duplicate: int
    unmapped: int
    malformed: int
    complete: bool = True


class FillBackfiller:
    """Poll REST ``userFillsByTime`` over a trailing window into a processor.

    ``fetch`` is the injected REST seam — ``(start_ms, end_ms) -> list[fill dict]`` —
    bound in production to the wallet's ``user_fills_by_time`` and in tests to a fake,
    so the whole thing runs without a network. ``processor`` is the SAME
    :class:`LiveFillProcessor` the WS drain feeds, which is what makes WS and REST
    converge on one exactly-once ledger.

    How far BACK a pass reads comes per call through :meth:`backfill`'s ``since`` —
    the caller relays ``LiveWsStream.backfill_since()`` verbatim; see
    :meth:`_window_start` for why the fills table cannot answer that question for
    itself.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[int, int], Any],
        processor: LiveFillProcessor,
        clock: Clock | None = None,
        lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        response_fill_cap: int = RESPONSE_FILL_CAP,
    ) -> None:
        if lookback_seconds <= 0:
            raise ValueError(f"lookback_seconds must be > 0, got {lookback_seconds}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")
        if response_fill_cap < 1:
            raise ValueError(f"response_fill_cap must be >= 1, got {response_fill_cap}")
        self._fetch = fetch
        self._processor = processor
        self._clock = clock or WallClock()
        self._lookback = lookback_seconds
        self._max_pages = max_pages
        self._response_cap = response_fill_cap

    def _window_start(self, stamp: datetime, since: datetime | None) -> datetime:
        """Where this pass must start reading: the trailing window, or the gap's start.

        The trailing window is a FLOOR, not a guarantee — it covers the routine case
        and nothing longer. ``since`` is what makes a long outage recoverable, and the
        caller must relay it, because the gap's start is a fact only the STREAM's
        bookkeeping holds (registered at boot, anchored at drops):

        - pass ``LiveWsStream.backfill_since()`` VERBATIM. It is the earliest
          still-uncovered obligation — the startup floor (registered at boot via
          ``set_startup_floor``: the newest booked fill, or the run's genesis when no
          fill exists yet) folded with any reconnect gap anchor. Folded by the STREAM,
          not dispatched here, so a drop right after a successful first connect cannot
          shadow a still-uncovered floor with its own few-minute gap;
        - on the routine heartbeat nothing is owed, and ``None`` leaves the window.

        The fills table cannot answer this on its own. A ``MAX(exchange_fill_time)``
        cursor looks like it can, but it collapses: let one newer fill be booked — a
        reconnect drains HL's isSnapshot batch before the backfill runs — and the MAX
        jumps to now, the window snaps back to the lookback, and every fill in the
        outage is fetched by no path at all while the pass reports success.
        """
        # Both instants are checked, not just ``since``. A naive ``now`` is the more
        # dangerous of the two and the quieter: it does not raise, it makes
        # ``.timestamp()`` read the wall clock as LOCAL time, so the window silently
        # shifts by the UTC offset and the fetch asks the exchange for the wrong hours.
        # A naive ``since`` merely blows up inside min() with an opaque "can't compare
        # offset-naive and offset-aware", far from the caller that got it wrong. Every
        # instant in this system is aware UTC; neither is accepted.
        _require_aware(stamp, name="now")
        floor = stamp - timedelta(seconds=self._lookback)
        if since is None:
            return floor
        _require_aware(since, name="since")
        return min(floor, since)

    def backfill(
        self, now: datetime | None = None, *, since: datetime | None = None
    ) -> BackfillSummary:
        """Fetch everything back to ``since`` and apply every new fill (dedupe-safe).

        A transport failure PROPAGATES: the caller (the engine loop) leaves the
        stream's ``needs_backfill`` flag set so the next tick retries — swallowing it
        here would silently declare the gap closed when it was not. Malformed fills
        inside a successful response are recorded and skipped (§11.3), not raised,
        since one bad fill must not abandon the rest of the window.

        The window is PAGED, because ``userFillsByTime`` caps a response (2000 fills)
        and a single unpaginated fetch would silently return a prefix of a long gap
        while reporting success. Each page resumes from the newest fill it saw; when
        the page budget runs out with the response still capped, the pass reports
        ``complete=False`` rather than pretending the gap is closed.
        """
        stamp = now or self._clock.now()
        end_ms = int(stamp.timestamp() * 1000)
        start_ms = int(self._window_start(stamp, since).timestamp() * 1000)

        fetched = applied = duplicate = unmapped = malformed = 0
        complete = True
        for page in range(self._max_pages):
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
            if not raw:
                break

            # Reuse the WS drain's per-fill §11.3 handling by wrapping the REST list in
            # a userFills envelope — one code path for "apply a batch of fills, skipping
            # malformed ones", whether they arrived over the socket or over REST. It
            # also orders the batch by exchange time, which a REST page need not be in.
            results = self._processor.ingest_message(
                {"channel": USER_FILLS_CHANNEL, "data": {"fills": raw}}
            )
            fetched += len(raw)
            applied += sum(1 for r in results if r.outcome is IngestOutcome.APPLIED)
            duplicate += sum(1 for r in results if r.outcome is IngestOutcome.DUPLICATE)
            unmapped += sum(1 for r in results if r.outcome is IngestOutcome.UNMAPPED)
            # ingest_message returns one result per PARSED fill and silently records +
            # skips the malformed ones, so the shortfall is exactly the malformed count.
            malformed += len(raw) - len(results)

            if len(raw) < self._response_cap:
                break  # the exchange gave us everything it had: the window is covered

            # The response was capped, so there is more inside this window. Resume from
            # the newest fill this page carried. The overlap on that millisecond is
            # re-fetched deliberately — the dedupe key absorbs it, and advancing past it
            # instead could step over a fill sharing that exact timestamp.
            newest_ms = _newest_fill_ms(raw)
            if newest_ms is None or newest_ms < start_ms:
                # Cannot advance (unparseable/absent times, or the page went backwards):
                # paging again would re-fetch the same page forever. Stop and report the
                # window as NOT covered rather than spin.
                complete = False
                break
            start_ms = newest_ms
            if page == self._max_pages - 1:
                complete = False

        if not complete:
            logger.warning(
                "fill backfill did NOT cover its window (%d fills over %d pages, response "
                "still capped) — the gap is still open; do not clear needs_backfill",
                fetched,
                self._max_pages,
            )
        summary = BackfillSummary(
            fetched=fetched,
            applied=applied,
            duplicate=duplicate,
            unmapped=unmapped,
            malformed=malformed,
            complete=complete,
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
