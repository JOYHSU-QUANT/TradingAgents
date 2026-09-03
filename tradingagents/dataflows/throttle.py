"""Which vendors refused this client with a rate limit recently (#86, #114).

Stdlib-only, like ``utils``, so every vendor module can share it. The router
holds one latch keyed by the vendor name its chain is built from; yfinance
holds its own instance at its network boundary, behind its OHLCV cache (see
``stockstats_utils``), and its rate-limit type tells the router not to latch
it a second time in front of that cache.
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

# How long a vendor that has just refused this client with a rate limit is
# kept away from. The design treats a 429 as a fact about this client's
# standing with the vendor rather than about one endpoint: once a call has
# been refused (for yfinance, after paying the full backoff ladder), the tools
# queued behind it have nothing new to learn by each re-discovering the same
# refusal and adding requests to a host already turning this client away. The
# window is a judgement call, not a measured property of any vendor's
# throttling: long enough to cover the tool calls of one decision cycle, and
# far shorter than the perp scheduler's CYCLE_INTERVAL (hours), so the next
# cycle always re-probes the vendor rather than inheriting a latch.
THROTTLE_LATCH_TTL_S = 300.0


class _Latched(NamedTuple):
    """One key's stand-off: when it ends, and when it was recorded (monotonic)."""

    deadline: float
    armed_at: float


class ThrottleLatch:
    """Per-key record of "this vendor refused us recently; stay away for now".

    Each key holds a monotonic deadline; a key with none, or with one already
    passed, may be contacted. Process-global by design — the throttle is a
    property of this client's relationship with a vendor, not of any one
    caller — and lock-guarded because ToolNode runs the tool calls of one
    model message on a thread pool, so arming and reading race without it.
    The deadline is exclusive, like the other windows in this codebase: at
    the deadline itself the vendor is contacted again.

    Alongside the deadline each key remembers WHEN it was armed, so that a
    call which returns can tell a deadline that predates it (lapsed, since a
    live one is refused before the call) from one a sibling thread recorded
    while the call was in flight — see :meth:`clear` (#153).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deadlines: dict[str, _Latched] = {}

    def remaining_s(self, key: str) -> float | None:
        """Seconds left on ``key``'s latch, or None when it may be contacted."""
        with self._lock:
            entry = self._deadlines.get(key)
        if entry is None:
            return None
        remaining = entry.deadline - time.monotonic()
        return remaining if remaining > 0 else None

    def arm(self, key: str, ttl_s: float | None = None) -> float:
        """Stand ``key`` off for ``ttl_s`` seconds from now; returns the window in force.

        ``None`` is the shared window above; a raise that says how long its
        refusal lasts (``VendorRateLimitError.latch_ttl_s``) passes its own.
        The default is resolved here only — a caller that wants to log the
        window reads the return value. A stand-off already recorded is never
        cut short: a burst throttle met while a daily quota is spent keeps
        the quota's deadline (and the return value is what remains of it),
        while the arm instant is the newer refusal's either way.
        """
        ttl_s = THROTTLE_LATCH_TTL_S if ttl_s is None else ttl_s
        with self._lock:
            now = time.monotonic()
            deadline = now + ttl_s
            existing = self._deadlines.get(key)
            if existing is not None and existing.deadline > deadline:
                deadline, ttl_s = existing.deadline, existing.deadline - now
            self._deadlines[key] = _Latched(deadline, now)
        return ttl_s

    def clear(self, key: str, *, before: float) -> None:
        """Drop ``key``'s deadline if it was armed before the instant ``before``.

        ``before`` is the monotonic instant the caller's request was SENT. A
        deadline armed before it is a lapsed one (a live one would have been
        read as a refusal without sending), and is dropped rather than left
        behind to reason about later. One armed at or after it was recorded
        by a sibling thread while this request was in flight, and is kept:
        the vendor decided this request no later than it was sent, so a
        refusal it issued after that instant is the later verdict, and
        dropping it would have the next tool call re-pay the discovery of
        the same throttle (#153). "At" counts as after — two events in one
        clock tick cannot be ordered (``time.monotonic`` ticks every 15.6ms
        on Windows before Python 3.13), and keeping a latch costs one
        stand-off where dropping a real one costs a backoff ladder.
        """
        with self._lock:
            entry = self._deadlines.get(key)
            if entry is not None and entry.armed_at < before:
                del self._deadlines[key]

    def has_deadline(self, key: str) -> bool:
        """Whether a deadline is recorded at all — lapsed or not.

        Not "may the vendor be contacted" (that is ``remaining_s``): for tests
        pinning that a served call *drops* a lapsed deadline rather than
        merely outliving it.
        """
        with self._lock:
            return key in self._deadlines

    def reset(self) -> None:
        """Forget every key. For the test suite's autouse fixture."""
        with self._lock:
            self._deadlines.clear()


VENDOR_THROTTLE_LATCH = ThrottleLatch()
