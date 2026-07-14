"""WebSocket ingestion: a thread-safe queue + connection lifecycle (phase3-spec §11).

The SDK's WebSocket manager delivers events on a background thread; this system
keeps the Phase 2 single-threaded 30-second tick loop (§11.4). The bridge is
:class:`LiveWsStream`: the WS callback does ONE thing — drop the raw event into a
thread-safe queue (no SQLite, no business logic — §11.4 rule 1) — and the tick
loop drains it, so SQLite stays single-writer and event order is deterministic.

The stream also tracks the connection lifecycle the rest of §11 rests on:

- ``mark_connected`` / ``mark_disconnected`` record transitions; every reconnect
  (and the first connect) raises ``needs_backfill`` — §11.2 rule 5 / §11 startup:
  a REST backfill must fill whatever the socket missed while it was down.
- ``disconnected_for`` / ``is_stale`` expose how long the socket has been down;
  past ``stale_after_seconds`` (default 300 — §11.2 rule 7) the run is stale and
  PR 4's safe-mode state machine takes over. This module only reports the fact.

:class:`WsConnectionSupervisor` drives (re)connection from the tick, matching the
single-thread model: :meth:`ensure_connected` opens a connection when there is
none (with a bounded backoff), and the socket layer calls :meth:`note_closed`
when it drops. The ``connect`` seam (which builds the SDK Info and binds the
subscriptions) is injected, so the policy is testable without a real socket.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from ..paper.clock import Clock, WallClock

__all__ = [
    "CLEARINGHOUSE_CHANNEL",
    "ORDER_UPDATES_CHANNEL",
    "USER_FILLS_CHANNEL",
    "LiveWsStream",
    "WsConnection",
    "WsConnectionSupervisor",
    "bind_user_subscriptions",
    "user_stream_subscriptions",
]

logger = logging.getLogger(__name__)

# §11.1 required streams. userFills is the accounting source PR 3 consumes;
# orderUpdates and the clearinghouse (account state) stream are enqueued too but
# their consumer is PR 4's reconciliation — PR 3 drains and ignores them so the
# queue never grows unbounded while they wait for their owner.
USER_FILLS_CHANNEL = "userFills"
ORDER_UPDATES_CHANNEL = "orderUpdates"
# webData2 carries clearinghouseState (the account/position snapshot) over WS;
# the basic openOrders/user_state endpoints have no WS equivalent that includes it.
CLEARINGHOUSE_CHANNEL = "webData2"

# §11.2 rule 7: disconnected longer than this → the run is stale and PR 4 enters
# recoverable safe mode. Named here (not only in the loop) so the WS layer and
# the state machine agree on the threshold.
DEFAULT_STALE_AFTER_SECONDS = 300.0

# The other half of rule 7. A socket can fail WITHOUT ever reporting a close —
# a half-open TCP connection (the usual outcome behind NAT / a load balancer)
# leaves the SDK believing it is connected while nothing is delivered, so a
# liveness test that only measures "how long have we been disconnected" reads a
# dead feed as healthy forever and the bot keeps opening entries on a stale
# position model. ``webData2`` pushes account state continuously, so silence on a
# genuinely-open socket is unambiguous: past this many seconds with no event at
# all, the connection is presumed dead even though it claims otherwise.
DEFAULT_SILENT_AFTER_SECONDS = 120.0


class LiveWsStream:
    """A thread-safe raw-event queue plus the §11 connection-state bookkeeping.

    The WS callback thread only ever calls :meth:`enqueue`; the tick thread calls
    :meth:`drain` and the lifecycle/query methods. One lock guards all mutable
    state so the two threads never see a torn view.
    """

    def __init__(
        self,
        *,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        silent_after_seconds: float = DEFAULT_SILENT_AFTER_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError(f"stale_after_seconds must be > 0, got {stale_after_seconds}")
        if silent_after_seconds <= 0:
            raise ValueError(f"silent_after_seconds must be > 0, got {silent_after_seconds}")
        self._stale_after = stale_after_seconds
        self._silent_after = silent_after_seconds
        self._clock = clock or WallClock()
        self._lock = threading.Lock()
        self._events: deque[Any] = deque()
        self._connected = False
        # None until the first connect/disconnect gives a basis: a stream that
        # has never connected is not "disconnected for N seconds" (there is no
        # prior connection to have dropped), so disconnected_for reads 0 there.
        self._disconnected_since: datetime | None = None
        # Doubles as "has EVER connected" (it is never cleared): mark_disconnected's
        # gap anchoring keys off that — clearing this on disconnect would silently
        # disable the anchor and reintroduce the startup-floor bypass it prevents.
        self._connected_since: datetime | None = None
        self._needs_backfill = False
        self._backfill_epoch = 0
        # The start of the gap a backfill still has to cover. Distinct from
        # ``_disconnected_since``, which is the LIVENESS clock and is cleared the moment
        # the socket comes back: the gap outlives the reconnect that created it, and is
        # retired only when a backfill has actually covered it.
        self._gap_since: datetime | None = None
        # The OTHER backfill obligation: everything that happened while the process was
        # not running (see :meth:`set_startup_floor`). Held here, beside the gap anchor,
        # because the two must be retired by the same epoch-gated clear — held apart
        # (the caller dispatching "floor at startup, anchor after") they can shadow each
        # other: a drop right after a successful first connect anchors a post-boot gap,
        # the caller reads the anchor as authoritative, and the still-uncovered floor is
        # silently forgotten (see :meth:`backfill_since`).
        self._startup_floor: datetime | None = None
        self._last_event_at: datetime | None = None

    def enqueue(self, event: Any) -> None:
        """Append one raw WS event (the callback's ONLY job — §11.4 rule 1).

        Runs on the SDK's background thread. It does not parse, touch SQLite, or
        decide anything — parsing (which may fail — §11.3) happens on the tick
        thread when the event is drained, so a malformed payload can never crash
        the socket callback and drop the subscription.
        """
        with self._lock:
            self._events.append(event)
            self._last_event_at = self._clock.now()

    def drain(self) -> list[Any]:
        """Remove and return every queued event in arrival order (tick thread)."""
        with self._lock:
            drained = list(self._events)
            self._events.clear()
        return drained

    def pending(self) -> int:
        with self._lock:
            return len(self._events)

    def mark_connected(self, now: datetime | None = None) -> None:
        """Record that the socket is (re)connected; always request a backfill.

        Both the first connect (startup) and every reconnect must be followed by
        a REST backfill (§11 startup / §11.2 rule 5): the socket cannot deliver
        what happened before it was subscribed, so a backfill request is raised on
        every transition into connected, and :meth:`mark_backfill_done` clears
        only the request whose backfill actually ran.

        ``now`` anchors the connect clock, which is the silence baseline until the
        first event arrives: a socket that connects and then delivers nothing has
        no ``last_event_at`` to measure against, and without a "connected since"
        the half-open case (see :meth:`is_stale`) would stay invisible for as long
        as the socket sat silently open.
        """
        stamp = now or self._clock.now()
        with self._lock:
            self._connected = True
            self._connected_since = stamp
            self._disconnected_since = None
            self._needs_backfill = True
            # Every transition into connected is a DISTINCT backfill request — it
            # has its own gap to close. The epoch names it, so a backfill that was
            # already in flight when the socket dropped and came back cannot clear
            # the request the newer reconnect raised, whose gap its window never
            # covered (see :meth:`mark_backfill_done`).
            self._backfill_epoch += 1

    def mark_disconnected(self, now: datetime | None = None) -> None:
        """Record that the socket has dropped; start the disconnect clock once.

        Idempotent while already disconnected: the clock is anchored at the FIRST
        drop, so repeated calls (a reconnect attempt that fails again) do not keep
        resetting how long we have been down — otherwise the §11.2-rule-7 stale
        threshold could never be reached under a flapping socket.
        """
        stamp = now or self._clock.now()
        with self._lock:
            self._connected = False
            if self._disconnected_since is None:
                self._disconnected_since = stamp
            if self._gap_since is None and self._connected_since is not None:
                # Anchor the gap the next backfill must cover. Anchored at the FIRST
                # drop and kept across the reconnect (unlike the liveness clock above,
                # which mark_connected resets), because a gap does not stop existing
                # just because the socket came back — it is retired only once a backfill
                # has actually covered it. A second drop before that backfill runs must
                # NOT move the anchor forward, or the first outage's fills fall out of
                # the window and are fetched by nothing.
                #
                # Never-connected is NOT a gap — hence the ``_connected_since`` gate. A
                # FAILED first connect lands here too (the supervisor marks the stream
                # disconnected so the liveness clock runs and a boot that cannot connect
                # still goes stale), but nothing was ever subscribed, so there is no drop
                # instant to anchor: the whole pre-connect era belongs to the STARTUP
                # floor (``repo.last_live_fill_time``), which ``backfill_since()``'s
                # ``None`` routes the caller to. Anchoring here instead would hand the
                # caller a "gap" starting at boot, the startup floor would be skipped,
                # and every fill older than the trailing lookback would be fetched by
                # no path — the exact silent loss the anchor exists to prevent.
                self._gap_since = stamp

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def needs_backfill(self) -> bool:
        with self._lock:
            return self._needs_backfill

    def backfill_epoch(self) -> int:
        """Which backfill request is currently outstanding (see :meth:`mark_backfill_done`)."""
        with self._lock:
            return self._backfill_epoch

    def set_startup_floor(self, floor: datetime | None) -> None:
        """Register the boot-time backfill obligation, ONCE, before the first connect.

        ``floor`` is where the process-was-down gap begins: the newest fill already
        booked (``repo.last_live_fill_time``), or — when the run has booked no fill yet,
        which still says nothing about its resting orders — the run's genesis timestamp.
        ``None`` declares there is no startup obligation at all; prefer the genesis
        fallback over ``None``, it is never wrong and the dedupe key absorbs the overlap.

        Held on the stream, not dispatched by the caller, so that it and the gap anchor
        are ONE obligation stream retired by the same epoch-gated clear. Dispatching
        caller-side ("floor at startup, anchor afterwards") has a silent hole: a drop
        seconds after a successful first connect — a flapping network right after a long
        outage is exactly this window — anchors a post-boot gap, the caller reads the
        non-``None`` anchor as the whole story, and the pass that covers the anchor's
        few minutes clears the request while the floor's day-long gap was never fetched
        by anything.
        """
        if floor is not None and floor.tzinfo is None:
            raise ValueError(f"startup floor must be timezone-aware (UTC), got {floor!r}")
        with self._lock:
            self._startup_floor = floor

    def backfill_since(self) -> datetime | None:
        """Where the pending backfill's window must begin, or ``None``.

        Pass this as ``FillBackfiller.backfill(since=...)``, verbatim. It is the
        EARLIEST still-uncovered obligation: the startup floor (everything since the
        process was last running — :meth:`set_startup_floor`) and/or the gap anchor
        (the socket's drop instant, kept alive across the reconnect that raised the
        request — ``disconnected_for`` cannot serve here, because it is the liveness
        clock and reads 0 the moment the socket is back).

        ``None`` means nothing is owed beyond the trailing lookback: every registered
        obligation has been retired by an epoch-gated :meth:`mark_backfill_done`, and
        no socket that was ever subscribed has dropped since — the routine-heartbeat
        state. Failed connect attempts do not disturb this: they run the liveness
        clock but anchor no gap (see :meth:`mark_disconnected`).

        Without this, an outage longer than the trailing lookback is unrecoverable and
        SILENT: the window falls back to the lookback, the older fills are fetched by no
        path, and the pass still reports success.
        """
        with self._lock:
            if self._startup_floor is None:
                return self._gap_since
            if self._gap_since is None:
                return self._startup_floor
            return min(self._startup_floor, self._gap_since)

    def mark_backfill_done(self, epoch: int) -> bool:
        """Clear the backfill request ``epoch`` once ITS backfill has run (§11.2 rule 5).

        Read the epoch, run the backfill, then clear with the epoch you read —
        never unconditionally. The REST fetch is a network round-trip, and the
        socket can drop and reconnect while it is in flight; that reconnect raises
        a NEW request, for a gap the in-flight window closed before it existed.
        An unconditional clear would retire that newer request too, and the fills
        from the second outage would land in neither the socket (it was down) nor
        any REST window (the request that would have fetched them was cleared) —
        a silently short ledger. Returns False when the request has been superseded,
        which means: run another backfill.
        """
        with self._lock:
            if epoch != self._backfill_epoch:
                return False
            self._needs_backfill = False
            # BOTH obligations retire together, HERE and nowhere else — not on
            # reconnect — so a backfill that never ran leaves them standing. Together,
            # because the pass being acknowledged read its window from
            # ``backfill_since()``, which folds them into one start: a pass that covered
            # that start covered both. The epoch gate cannot see COVERAGE, though: a
            # pass that ran but could not prove it covered its window reports
            # ``BackfillSummary.complete = False``, and it is the CALLER's obligation not
            # to call this method for that pass — same contract as ``needs_backfill``.
            self._gap_since = None
            self._startup_floor = None
            return True

    def disconnected_for(self, now: datetime | None = None) -> float:
        """Seconds the socket has been down; ``0`` while connected or never-connected."""
        stamp = now or self._clock.now()
        with self._lock:
            if self._connected or self._disconnected_since is None:
                return 0.0
            return (stamp - self._disconnected_since).total_seconds()

    def silent_for(self, now: datetime | None = None) -> float:
        """Seconds since the last event on a socket that CLAIMS to be connected.

        Measured from the last event, or — before any event has arrived — from the
        connect itself, so a socket that comes up and immediately goes quiet is not
        given an infinite grace period. Reads ``0`` while disconnected: that case is
        :meth:`disconnected_for`'s, and reporting both would double-count one outage.
        """
        stamp = now or self._clock.now()
        with self._lock:
            if not self._connected:
                return 0.0
            since = self._last_event_at or self._connected_since
            if since is None:
                return 0.0
            return (stamp - since).total_seconds()

    def is_stale(self, now: datetime | None = None) -> bool:
        """True once the feed cannot be trusted — §11.2 rule 7, both ways it fails.

        A socket fails in two shapes, and only one of them announces itself:

        - it CLOSES — ``disconnected_for`` passes ``stale_after_seconds`` (300);
        - it goes SILENT while still claiming to be open — a half-open TCP
          connection, the usual outcome behind NAT or a load balancer. Nothing
          calls ``note_closed``, ``_connected`` stays True, and a liveness test
          that only measured downtime would read a dead feed as healthy forever.
          ``webData2`` pushes account state continuously, so silence past
          ``silent_after_seconds`` is unambiguous.

        The signal PR 4's state machine reads to enter recoverable safe mode; this
        module only reports it, never acts on it.
        """
        return (
            self.disconnected_for(now) > self._stale_after
            or self.silent_for(now) > self._silent_after
        )

    def last_event_at(self) -> datetime | None:
        with self._lock:
            return self._last_event_at


def user_stream_subscriptions(wallet_address: str) -> list[dict[str, Any]]:
    """The §11.1 subscription payloads for one wallet.

    ``userFills`` and the clearinghouse (``webData2``) stream are per-wallet;
    ``orderUpdates`` is not (it multiplexes on the connection). Kept as one
    function so the production binder and the tests subscribe to the same set.
    """
    return [
        {"type": USER_FILLS_CHANNEL, "user": wallet_address},
        {"type": ORDER_UPDATES_CHANNEL},
        {"type": CLEARINGHOUSE_CHANNEL, "user": wallet_address},
    ]


class _WsSubscriber(Protocol):
    """The SDK ``Info``'s subscribe surface — the only method the binder needs."""

    def subscribe(self, subscription: dict[str, Any], callback: Callable[[Any], None]) -> int: ...


def bind_user_subscriptions(
    subscriber: _WsSubscriber, wallet_address: str, stream: LiveWsStream
) -> list[int]:
    """Register the §11.1 subscriptions so every event lands in ``stream``.

    Each subscription's callback is ``stream.enqueue`` and nothing else (§11.4
    rule 1). Returns the subscription ids the SDK assigns. Takes only the
    ``subscribe`` surface (Protocol), so a test binds a fake and drives the
    callbacks directly, no socket required.
    """
    return [
        subscriber.subscribe(subscription, stream.enqueue)
        for subscription in user_stream_subscriptions(wallet_address)
    ]


class WsConnection(Protocol):
    """A live subscription handle — the supervisor only needs to close it."""

    def close(self) -> None: ...


class WsConnectionSupervisor:
    """Tick-driven (re)connection for one WS stream (§11.2 rules 4–5).

    Fits the single-thread tick model: :meth:`ensure_connected` is called each
    tick and opens a connection whenever there is none, honouring a minimum
    backoff so a hard-down endpoint is not hammered every 30 s. The socket layer
    reports a drop through :meth:`note_closed`. ``connect`` — which builds the SDK
    Info and binds the subscriptions to the stream — is injected, so the whole
    policy is testable with a fake that can be made to fail or succeed.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], WsConnection],
        stream: LiveWsStream,
        clock: Clock | None = None,
        reconnect_min_interval_seconds: float = 5.0,
    ) -> None:
        if reconnect_min_interval_seconds < 0:
            raise ValueError("reconnect_min_interval_seconds must be >= 0")
        self._connect = connect
        self._stream = stream
        self._clock = clock or WallClock()
        self._min_interval = reconnect_min_interval_seconds
        # This object is touched from BOTH threads — note_closed() runs on the
        # SDK's socket thread (on_close / on_error) while ensure_connected() and
        # close() run on the tick thread — so its state is locked, exactly like
        # LiveWsStream's. The lock is never held across the connect() call (that
        # is network I/O and would block the socket thread's close callback);
        # instead a generation counter detects a close that raced the connect.
        self._lock = threading.Lock()
        self._handle: WsConnection | None = None
        self._last_attempt_at: datetime | None = None
        self._generation = 0

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._handle is not None

    def note_closed(self) -> None:
        """The socket dropped (on_close / on_error). Mark disconnected, drop the handle.

        Safe to call more than once — ``mark_disconnected`` anchors the disconnect
        clock only on the first drop, so a close followed by a failed reconnect
        does not reset how long the run has been stale.

        Bumping the generation is what makes a close that lands DURING an
        in-flight ``ensure_connected`` win: that connect will see the generation
        has moved, discard the connection it just opened, and refuse to mark the
        stream connected. Without it, the tick thread would overwrite this
        disconnect with ``mark_connected`` and the stream would report a healthy
        socket that is actually dead — silently defeating §11.2 rule 6 (no new
        entries while down) and postponing rule 7's 5-minute stale detection.

        The stream transition is published INSIDE the lock, with the handle drop
        and the generation bump — see :meth:`ensure_connected`.
        """
        with self._lock:
            self._handle = None
            self._generation += 1
            self._stream.mark_disconnected(self._clock.now())

    def ensure_connected(self) -> bool:
        """Open a connection if there is none, respecting the backoff. Returns connected.

        A connect failure marks the stream disconnected and returns False — the
        caller keeps ticking (monitoring / protection stay live — §11.2 rule 6
        blocks new ENTRIES while down, not the loop), and the next tick past the
        backoff retries. A success marks the stream connected, which raises
        ``needs_backfill`` for the caller to satisfy before trusting live data.
        """
        with self._lock:
            if self._handle is not None:
                return True
            now = self._clock.now()
            if (
                self._last_attempt_at is not None
                and (now - self._last_attempt_at).total_seconds() < self._min_interval
            ):
                return False  # backing off from the previous failed attempt
            self._last_attempt_at = now
            generation = self._generation

        # Outside the lock: connect() is network I/O, and holding the lock across
        # it would block the socket thread's note_closed().
        try:
            handle = self._connect()
        except Exception as exc:  # noqa: BLE001 — any connect failure is "still down"
            self._stream.mark_disconnected(now)
            logger.warning("websocket connect failed: %s", exc)
            return False

        # The connect is done; re-read the clock. ``now`` above was stamped BEFORE the
        # network round-trip (it anchors the backoff), and a slow handshake can take
        # tens of seconds. Stamping "connected since" with the attempt time would
        # charge that latency to the silence budget, so a socket that connects slowly
        # and then delivers promptly could still be judged half-open (is_stale).
        connected_at = self._clock.now()
        with self._lock:
            # Superseded on EITHER count: a close bumped the generation while we
            # were connecting, or another caller already installed a handle. The
            # second is out of contract (only the tick thread calls this), but
            # without the check a concurrent pair would both open a socket and the
            # loser would overwrite ``_handle`` — leaking the winner's live
            # connection with nothing left holding a reference to close it.
            superseded = self._generation != generation or self._handle is not None
            if not superseded:
                self._handle = handle
                # Publish the stream transition HERE, under the same lock that
                # installed the handle — not after releasing it. The supervisor's
                # handle/generation and the stream's connected flag describe one
                # fact and must move as one unit: a note_closed() landing in the
                # gap between them would drop the handle, bump the generation and
                # mark the stream disconnected, and then this call would overwrite
                # that with "connected" — leaving a stream that reports a healthy
                # socket the supervisor no longer holds, with the disconnect clock
                # reset so rule 7's stale trip never accumulates. (The mirror case
                # is worse: publishing note_closed's mark_disconnected outside the
                # lock let a connect that started AFTER the generation bump install
                # a live handle and then be overwritten by the late disconnect —
                # every later tick short-circuits on ``_handle is not None``, so the
                # stream stayed permanently "disconnected" on a working socket and
                # the run sat in safe mode forever.)
                self._stream.mark_connected(connected_at)
        if superseded:
            # A close landed while we were connecting: the socket we just opened
            # is already stale. Drop it rather than claim a healthy connection —
            # the disconnect note_closed() recorded must stand.
            logger.warning("websocket connect superseded by a concurrent close; discarding it")
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning("discarding superseded websocket failed to close: %s", exc)
            return False

        logger.info("websocket connected")
        return True

    def close(self) -> None:
        """Tear down the connection on shutdown; a close failure is logged, not raised."""
        with self._lock:
            handle = self._handle
            self._handle = None
            # Retire this generation too: a connect racing shutdown must not
            # install a fresh handle on a supervisor we have just torn down.
            self._generation += 1
            # Published under the lock, with the handle drop and the generation bump,
            # exactly as in note_closed() / ensure_connected() — the supervisor's
            # state and the stream's flag describe one fact and move as one unit.
            self._stream.mark_disconnected(self._clock.now())
        if handle is None:
            return
        try:
            handle.close()
        except Exception as exc:  # noqa: BLE001 — shutdown teardown must not raise
            logger.warning("websocket close failed: %s", exc)
