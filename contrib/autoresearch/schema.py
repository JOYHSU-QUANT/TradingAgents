"""DDL for ``autoresearch.sqlite`` — this package's own store.

Separate from ``contrib.hyperliquid_perp``'s paper store in every way that
matters: its own file, its own version counter, its own bookkeeping table.
Nothing here is ever applied to that store, and ``schema_migrations`` (its
name for the same job) is deliberately NOT the name used here, so a mistyped
``--db`` pointing at a paper store cannot half-match: the version read finds
no ``schema_version`` table and :mod:`.store` refuses the file by name.

Storage conventions, copied from the paper store because the two are read
side by side in a post-mortem and a second convention would be a trap:

- prices, sizes, volumes and rates are **TEXT** — the string form of a
  :class:`~decimal.Decimal`, so no precision is lost to a REAL float;
- venue timestamps are **INTEGER** UTC epoch milliseconds, the form the
  venue sends and the form :func:`~contrib.autoresearch.upstream.from_epoch_ms`
  decodes exactly.

Only the two HISTORY tables exist at v1. The plan's ``experiments`` and
``trials`` ledger arrives with the code that writes and reads it (plan §5,
PR A4). Creating them now would add two tables with no producer and no
reader — a lane nothing populates reads to a later maintainer as a feature
that broke, not as one that has not been built.
"""

from __future__ import annotations

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SCHEMA_VERSION_DDL"]

SCHEMA_VERSION = 1

# Created by ``store.apply_migrations`` before any migration runs, so it is
# kept out of the versioned list below (it is the bookkeeping, not a step).
SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# One row per closed bar. The primary key is the identity a bar HAS — the
# series it belongs to plus the instant it opened — so re-fetching an
# overlapping window is an upsert, never a duplicate: the backfill pages
# backwards and a re-run after an interrupted pass must be able to walk over
# ground it already covered. ``close_time`` is carried rather than derived
# from ``open_time + interval`` because it is the venue's own statement of
# where the bar ended, and deriving it would replace a fact with an
# assumption. Nothing compares the two claims yet — the gap scan measures
# ``open_time`` alone — so a bar whose ``close_time`` disagreed with its
# interval is stored faithfully and goes unremarked. Keeping the column is
# what leaves that check possible later without a re-fetch.
_CANDLES = """
CREATE TABLE candles (
    coin        TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    close_time  INTEGER NOT NULL,
    open        TEXT    NOT NULL,
    high        TEXT    NOT NULL,
    low         TEXT    NOT NULL,
    close       TEXT    NOT NULL,
    volume      TEXT    NOT NULL,
    PRIMARY KEY (coin, interval, open_time)
)
"""

# One row per funding settlement. ``premium`` is nullable because the venue's
# ``FundingPoint`` carries it as optional — a point without one is a real
# point, not a broken row, and must not be dropped or defaulted to zero (a
# zero premium is a market statement; a missing one is not).
_FUNDING = """
CREATE TABLE funding (
    coin     TEXT    NOT NULL,
    time     INTEGER NOT NULL,
    rate     TEXT    NOT NULL,
    premium  TEXT,
    PRIMARY KEY (coin, time)
)
"""

# What the last backfill of each series actually reached. One row per series,
# upserted, because the question it answers is about the present: "does this
# store cover the span an experiment is about to be measured on".
#
# It exists because the gap scan structurally cannot answer that. The scan
# anchors its grid on the first stamp it finds, so a series whose FRONT was
# truncated - an interrupted backfill, a Ctrl-C - is internally consistent and
# scans as having no holes at all. Without this row there is no way to tell
# that from a series that is short because the venue genuinely serves no more
# (the 4h case, which stops about 833 days back whatever --since says).
#
# ``stopped`` is what separates them, and it is the one fact here that cannot
# be recovered later: the rows themselves never say which ending produced
# them, so a store backfilled today and read in six months would have to be
# re-fetched to find out. ``venue_clock_ms`` dates the answer, so a trial's
# metrics can be tied to the data vintage they were computed on.
_SERIES_STATE = """
CREATE TABLE series_state (
    coin           TEXT    NOT NULL,
    series         TEXT    NOT NULL,
    venue_clock_ms INTEGER NOT NULL,
    since_ms       INTEGER NOT NULL,
    earliest_ms    INTEGER,
    latest_ms      INTEGER,
    rows           INTEGER NOT NULL,
    stopped        TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (coin, series)
)
"""

# version -> ordered DDL statements applied in one transaction for that version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (_CANDLES, _FUNDING, _SERIES_STATE),
}
