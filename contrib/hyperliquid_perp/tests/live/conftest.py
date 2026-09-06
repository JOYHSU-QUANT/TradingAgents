"""Shared stand-ins for the live-lane tests."""

from __future__ import annotations

from datetime import datetime

from contrib.hyperliquid_perp.live.fill_backfill import DEFAULT_LOOKBACK, BackfillSummary


class StubBackfiller:
    """A fully-wired fill leg that books nothing; ``calls`` records each pass's ``since``.

    Everything ``LiveReconciler`` reads off a backfiller lives here once, for
    both suites (issue #169): the ``lookback`` span and a ``backfill`` taking
    the call shape the reconciler uses (``backfill(now, since=...)``),
    reporting a complete pass.
    """

    lookback = DEFAULT_LOOKBACK  # the reconciler reads it

    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    def backfill(self, now, *, since=None) -> BackfillSummary:
        self.calls.append(since)
        return BackfillSummary(
            fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0, complete=True
        )
