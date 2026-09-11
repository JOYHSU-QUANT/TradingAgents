"""Venue cadence facts more than one module here has to agree on.

Small enough to inline at each use, which is exactly why they are not: the
funding step is asserted by the gap scan and assumed by the backfill's page
size, and two independent spellings of "an hour" would let one be adjusted
while the other silently kept describing a different history.
"""

from __future__ import annotations

__all__ = ["CANDLE_STAMP_TOLERANCE_MS", "FUNDING_INTERVAL_MS", "FUNDING_STAMP_TOLERANCE_MS", "MS_PER_DAY"]

MS_PER_DAY = 24 * 60 * 60_000

# Hyperliquid settles perp funding every HOUR (the perp package's engine pays
# it hourly, and ``fundingHistory`` publishes one point per hour), so this is
# the step a complete funding series advances by. The gap scan measures
# against it and reports anything else — including a venue that changed its
# cadence, which is a thing a research store must surface rather than absorb.
FUNDING_INTERVAL_MS = 60 * 60_000

# How far off its hourly slot a funding stamp may sit and still count as being
# in it. The venue stamps a settlement when it POSTS, not on an ideal grid:
# measured against mainnet BTC on 2026-09-11, 531 consecutive settlements
# carried offsets of 2ms to 99ms (e.g. 01:00:00.057, 02:00:00.030). Judged on
# an exact grid, 524 of those 531 read as off-grid — a report that flags
# everything answers nothing, which is the same failure the grid's anchoring
# choice exists to avoid. Five seconds is fifty times the jitter observed and
# one seven-hundred-and-twentieth of the step, so it absorbs posting latency
# while leaving any stamp genuinely in the wrong slot to be named.
FUNDING_STAMP_TOLERANCE_MS = 5_000

# Candle stamps get NO tolerance, and that is a measurement rather than an
# oversight: the venue's bar stamps are the grid (``open_time`` on the
# interval, ``close_time`` exactly one interval later), confirmed on the same
# 2026-09-11 read — 132 consecutive 4h bars, every one exact. A venue that
# started jittering bar stamps would be changing what a bar IS, so the scan
# should say so loudly rather than absorb it.
CANDLE_STAMP_TOLERANCE_MS = 0
