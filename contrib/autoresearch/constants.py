"""Venue cadence facts more than one module here has to agree on.

Small enough to inline at each use, which is exactly why they are not: the
funding step is asserted by the gap scan and assumed by the backfill's page
size, and two independent spellings of "an hour" would let one be adjusted
while the other silently kept describing a different history.
"""

from __future__ import annotations

__all__ = [
    "CANDLE_STAMP_TOLERANCE_MS",
    "FUNDING_INTERVAL_MS",
    "FUNDING_STAMP_TOLERANCE_MS",
    "MS_PER_DAY",
    "STUDIED_INTERVALS",
]

MS_PER_DAY = 24 * 60 * 60_000

# The candle intervals this package studies, NOT the venue's whole vocabulary.
# Plan §1 names 4h (the paper cycle) and 1d (the daily backdrop) and nothing
# else, and the venue's ~5000-bar depth limit is what makes that a correctness
# matter rather than taste: at 1h it reaches about 208 days and at 15m about
# 52, and such a series scans as having no holes and is far too short for a
# train/validation/holdout split to mean anything. One tuple, read by the CLI
# (as ``--interval``'s choices) and by the split (as the cadence a window is
# measured on), so a window cannot be built on an interval the CLI would
# refuse to fetch.
STUDIED_INTERVALS = ("4h", "1d")

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
# oversight: the venue's ``open_time`` stamps are the grid, confirmed on the
# 2026-09-11 read — 132 consecutive 4h bars, every one exact — and the scan
# measures ``open_time`` alone. ``close_time`` is the venue's own statement of
# where the bar ended, and it is ONE MILLISECOND before the next open
# (measured 2026-09-12: 14399999 ms on all 4999 4h rows, 86399999 on all
# 2215 daily rows), not the next open itself. Arithmetic that mixes a close
# with an open, or with an interval, has to say which of the two it means.
# A venue that started jittering bar stamps would be changing what a bar IS,
# so the scan should say so loudly rather than absorb it.
CANDLE_STAMP_TOLERANCE_MS = 0
