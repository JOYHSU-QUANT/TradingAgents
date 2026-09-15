"""Facts more than one module here has to agree on.

Small enough to inline at each use, which is exactly why they are not: the
funding step is asserted by the gap scan and assumed by the backfill's page
size, and two independent spellings of "an hour" would let one be adjusted
while the other silently kept describing a different history.
"""

from __future__ import annotations

__all__ = [
    "CANDLE_CLOSE_BEFORE_NEXT_OPEN_MS",
    "CANDLE_STAMP_TOLERANCE_MS",
    "DAILY_INTERVAL",
    "DEFAULT_MAX_TRIALS",
    "FUNDING_INTERVAL_MS",
    "FUNDING_STAMP_TOLERANCE_MS",
    "MS_PER_DAY",
    "STUDIED_INTERVALS",
    "bar_span_ms",
]

MS_PER_DAY = 24 * 60 * 60_000

# Plan §3.11's budget for one run of the hypothesis loop, counted in ANSWERS
# from the model rather than in trials filed (a refusal and a rule already
# tried each spend one). Here rather than beside the loop because ``cli``
# prints it as a flag default, and importing the loop to read it would put
# the pandas stack behind every command that merely builds the parser.
DEFAULT_MAX_TRIALS = 10

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

# The daily backdrop. Every experiment reads it whatever its decision
# interval: ``close_1d`` and ``sma_1d_*`` are daily features, so a store with
# a clean 4h series and no daily one is not fit to measure on, and the scan
# names its absence beside whichever interval was asked for.
DAILY_INTERVAL = "1d"

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
# choice exists to avoid.
#
# Twenty minutes, not the five seconds this first was — also a measurement.
# The whole mainnet BTC history from 2024-03-01 (22,254 settlements, read
# 2026-09-14) holds two that posted LATE: 2025-07-19 10:14:47.645 and
# 2025-07-27 12:01:50.963, each alone in an hour whose on-time slot is empty.
# They are those hours' settlements, not strays. Judged at five seconds they
# were off-grid, and because a window holding an off-grid stamp is refused
# (a stray standing in for a missing hour would be charged as its carry), no
# experiment could be opened on the real store at all. Twenty minutes holds
# the latest post seen with room to spare and is still a third of the step:
# a stamp in the middle of an hour — another cadence written into the series
# — is still named, and a late post into an hour that already has its
# settlement is still a duplicate. The bound a bundle is read to, and how old
# a rate may be before it is stale, are derived from this, and move with it.
FUNDING_STAMP_TOLERANCE_MS = 20 * 60_000

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

# Where the venue says a bar ENDS, relative to where the next one opens: the
# millisecond measured above. The gap scan holds every stored bar to this
# shape - ``close_time == open_time + interval - 1`` - because a bar whose
# close disagrees with its interval is the one finding ``open_time`` alone
# cannot see: a daily bar written into the 4h series sits exactly on a 4h
# slot, and is wrong only in how long it says it lasted.
CANDLE_CLOSE_BEFORE_NEXT_OPEN_MS = 1


def bar_span_ms(step_ms: int) -> int:
    """How long a venue bar on a ``step_ms`` grid says it lasted: its ``close_time - open_time``.

    The one spelling of the shape the scan checks and the test fixtures build,
    so the two cannot drift apart into every clean series reading as misshapen.
    """
    return step_ms - CANDLE_CLOSE_BEFORE_NEXT_OPEN_MS
