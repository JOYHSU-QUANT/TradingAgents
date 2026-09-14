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

v1 is the HISTORY (candles, funding, what each backfill reached). v2 is the
LEDGER (plan §3.3, PR A4): the experiments trials are measured inside, and the
trials themselves. It arrived with :mod:`~contrib.autoresearch.ledger`, the
code that writes and reads it, rather than beside the history tables in v1 —
two tables with no producer read to a later maintainer as a feature that
broke, not as one that had not been built.
"""

from __future__ import annotations

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SCHEMA_VERSION_DDL"]

SCHEMA_VERSION = 2

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

# One row per experiment: the fixed conditions every trial inside it is measured
# under, written ONCE (plan §3.7, §3.8). The JSON columns are exactly what
# ``CostModel.to_dict`` / ``Split.to_dict`` / ``Penalty.to_dict`` produce, and each
# ``from_dict`` refuses a record with a key missing or extra.
#
# Two departures from the plan's column list, both on purpose. There is no
# ``family``: plan §10.6 found that two of the five families are an author's
# intent and relabelling is free, so the trial penalty counts distinct rules
# (on the coin, across its experiments — see ``ledger``) and family is a report
# dimension on the TRIAL. And
# ``indicator_lookback`` is here because it changes every indicator a trial
# reads (plan §11): two experiments differing only in it would otherwise write
# identical rows. ``coin`` is here because nothing else in the row names the
# market the split is a window over.
_EXPERIMENTS = """
CREATE TABLE experiments (
    experiment_id      TEXT    PRIMARY KEY,
    coin               TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    cost_params_json   TEXT    NOT NULL,
    split_json         TEXT    NOT NULL,
    indicator_lookback INTEGER NOT NULL,
    penalty_json       TEXT    NOT NULL,
    notes              TEXT    NOT NULL
)
"""

# One row per DISTINCT rule measured inside an experiment. ``spec_hash`` is
# unique per experiment: measuring the same rule twice is not a second look at
# the validation window (the evaluator is deterministic), so it is not a second
# trial and does not raise the penalty.
#
# The holdout lock, as a constraint: a trial has holdout metrics if and only if
# it has been promoted, and it is promoted at most once (``measured`` ->
# ``promoted``, never back). A row that disagreed — holdout figures on a
# measured trial — is the one state no code path here writes, so the store
# refuses it rather than trusting every future writer to.
_TRIALS = """
CREATE TABLE trials (
    trial_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id           TEXT    NOT NULL REFERENCES experiments (experiment_id),
    family                  TEXT    NOT NULL,
    spec_json               TEXT    NOT NULL,
    spec_hash               TEXT    NOT NULL,
    train_metrics_json      TEXT    NOT NULL,
    validation_metrics_json TEXT    NOT NULL,
    holdout_metrics_json    TEXT,
    status                  TEXT    NOT NULL CHECK (status IN ('measured', 'promoted')),
    created_at              TEXT    NOT NULL,
    promoted_at             TEXT,
    UNIQUE (experiment_id, spec_hash),
    CHECK ((status = 'promoted') = (holdout_metrics_json IS NOT NULL)),
    CHECK ((status = 'promoted') = (promoted_at IS NOT NULL))
)
"""

# version -> ordered DDL statements applied in one transaction for that version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (_CANDLES, _FUNDING, _SERIES_STATE),
    2: (_EXPERIMENTS, _TRIALS),
}
