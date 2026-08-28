"""Which vendors refused this client with a rate limit recently (#86, #114).

Stdlib-only, like ``utils``, so every vendor module can share it. One latch,
keyed by the vendor name the router's chain is built from, read and written
at two boundaries: ``route_to_vendor`` for every vendor, and the yfinance
retry wrapper under its own name for the one caller that does not route. An
answer Yahoo gives on either path clears the same deadline, and a refusal met
on either path is honoured by both.
"""

from __future__ import annotations

import threading
import time

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


class ThrottleLatch:
    """Per-key record of "this vendor refused us recently; stay away for now".

    Each key holds a monotonic deadline; a key with none, or with one already
    passed, may be contacted. Process-global by design — the throttle is a
    property of this client's relationship with a vendor, not of any one
    caller — and lock-guarded because ToolNode runs the tool calls of one
    model message on a thread pool, so arming and reading race without it.
    The deadline is exclusive, like the other windows in this codebase: at
    the deadline itself the vendor is contacted again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}

    def remaining_s(self, key: str) -> float | None:
        """Seconds left on ``key``'s latch, or None when it may be contacted."""
        with self._lock:
            deadline = self._deadlines.get(key)
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        return remaining if remaining > 0 else None

    def arm(self, key: str) -> None:
        with self._lock:
            self._deadlines[key] = time.monotonic() + THROTTLE_LATCH_TTL_S

    def clear(self, key: str) -> None:
        """Drop ``key``'s deadline rather than leaving an expired one behind."""
        with self._lock:
            self._deadlines.pop(key, None)

    def has_deadline(self, key: str) -> bool:
        """Whether a deadline is recorded at all — lapsed or not.

        Not "may the vendor be contacted" (that is ``remaining_s``): for tests
        pinning that a served call *drops* a lapsed deadline rather than
        merely outliving it.
        """
        with self._lock:
            return key in self._deadlines

    def reset(self) -> None:
        """Forget every key. Public for the test suite's autouse fixture."""
        with self._lock:
            self._deadlines.clear()


VENDOR_THROTTLE_LATCH = ThrottleLatch()
