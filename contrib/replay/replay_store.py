"""``replay.sqlite``: the past papers' own store (replay plan PR 2).

Seven tables, and nothing here ever writes the paper store:

- ``variants``: one row per variant this store has asked with, keyed by its
  :attr:`~.variant.Variant.sha`. The name is UNIQUE too, so a name means
  one variant for the life of the store: a changed file under an old name
  is refused rather than filed as the old variant or quietly beside it.
- ``answers``: one row per question, variant and repeat; the key is
  ``(variant_sha, run_id, input_id, repeat)``, which is what makes a replay
  resumable (an answer already stored is never asked for again). A row
  keeps the model's raw text, what the parse seam made of it, and every
  field the gate returned that the scorecard reads, in the gate's own
  spelling: enum values as text, margins as integer text (the grid is
  integral) and confidence as ``Decimal`` text.
- ``failures``: one row per question, variant and repeat the provider
  refused for the question's own sake (a 4xx other than 401/403/404, the
  key or the model, and other than 408/409/429, which are retried; for
  instance the context too long, or a content filter). A question
  recorded here is not asked again unless the replay is told to, and the
  scorecard counts it as unanswered, as the daemon counts an ``api_failed``
  cycle.
- ``splits``: one row per run, the train / validation / holdout split the
  run was first replayed (or first looked at) under. Every later command on
  the run through this store uses it, however many questions the run has
  gained since (``score`` without a store still cuts the run as it stands): cut
  afresh each time, a split over a run still trading would move its
  boundaries every cycle and walk questions out of the holdout into
  validation and train (decided 2026-09-24, as the research ledger pins
  its holdout).
- ``ledger``: one row per look at a holdout (plan §3-9). ``ask`` is written
  before the first holdout payload is read, and ``score`` before a holdout
  answer is scored; either way the row exists even if what follows fails.
  A look at the paper trader's own answers (``score --holdout`` with no
  variant) is recorded too, with no variant. A probe asked on the holdout
  is recorded as an ``ask`` by its variant: the look is spent either way.
- ``probes``: one row per direction probe (plan PR 2.1) this store has
  asked with, keyed by :attr:`~.probe.Probe.sha`, its name UNIQUE, as a
  variant's is.
- ``probe_answers``: one row per question, variant, probe and repeat; the
  key is ``(variant_sha, probe_sha, run_id, input_id, repeat)``, so a probe
  run resumes as a replay does. A row keeps the raw text and the
  normalised forecast as JSON, or why there is none: ``invalid_probe`` (the
  answer was not a forecast the probe takes; not asked again) or
  ``refused`` (the provider refused the question for its own sake, as a
  ``failures`` row records for a decision; asked again only when told).

The schema is versioned by ``PRAGMA user_version``. A file that holds
tables but not ours is refused before anything writes to it, a store a
newer build wrote is refused rather than written through, and a reader
never creates the tables: an empty file opened to be read is refused. A
store written at v2 (before the probe) is brought to v3 by a command that
may create a store (``replay``, ``register``, ``score --holdout`` on the
paper trader's own answers), which adds the two probe tables in one
transaction and changes nothing already there; any other command reads it
as written, as a store with no probe answers. A v1 store is refused: v1
never left its pull request, so there is no migration from it. A write
that fails raises :class:`ReplayStoreError` naming the store, after
whatever it had begun is rolled back.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from .paper_store import answer_from_row
from .probe import REFUSED, Forecast, Probe, ProbeAnswer, ProbeError
from .score import Answer
from .upstream import RiskGateResult, Split, SplitError
from .variant import Variant, short_sha

__all__ = [
    "SCHEMA_VERSION",
    "HoldoutLook",
    "ReplayStore",
    "ReplayStoreError",
    "StoredAnswer",
    "StoredProbeAnswer",
]

# v2 added ``failures`` and ``splits`` and let a ledger row name no variant;
# v3 added ``probes`` and ``probe_answers``. v1 never left its pull request,
# so there is no migration from it: a v1 file is named and refused, not
# taken for someone else's database. A v2 file gains the v3 tables when a
# command that may create a store opens it.
SCHEMA_VERSION: Final = 3

_DDL: Final = (
    """
    CREATE TABLE variants (
        variant_sha   TEXT PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE,
        provider      TEXT NOT NULL,
        model         TEXT NOT NULL,
        temperature   REAL,
        max_tokens    INTEGER NOT NULL,
        system_prompt TEXT NOT NULL,
        extra_context TEXT,
        model_cutoff  TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE answers (
        variant_sha                 TEXT NOT NULL REFERENCES variants (variant_sha),
        run_id                      TEXT NOT NULL,
        input_id                    TEXT NOT NULL,
        repeat                      INTEGER NOT NULL CHECK (repeat >= 0),
        asked_at                    TEXT NOT NULL,
        segment                     TEXT NOT NULL,
        raw_response                TEXT NOT NULL,
        truncated                   INTEGER NOT NULL,
        model_reported              TEXT,
        input_tokens                INTEGER,
        output_tokens               INTEGER,
        invalid_reason              TEXT,
        decision_mode               TEXT NOT NULL,
        target_side                 TEXT,
        requested_target_margin_pct TEXT,
        approved_target_margin_pct  TEXT,
        risk_action                 TEXT NOT NULL,
        risk_reason                 TEXT,
        confidence                  TEXT,
        order_created               INTEGER NOT NULL,
        no_order_reason             TEXT,
        PRIMARY KEY (variant_sha, run_id, input_id, repeat)
    )
    """,
    """
    CREATE TABLE failures (
        variant_sha TEXT NOT NULL REFERENCES variants (variant_sha),
        run_id      TEXT NOT NULL,
        input_id    TEXT NOT NULL,
        repeat      INTEGER NOT NULL CHECK (repeat >= 0),
        failed_at   TEXT NOT NULL,
        error       TEXT NOT NULL,
        PRIMARY KEY (variant_sha, run_id, input_id, repeat)
    )
    """,
    """
    CREATE TABLE splits (
        run_id     TEXT PRIMARY KEY,
        split_json TEXT NOT NULL,
        pinned_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE ledger (
        entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        who         TEXT NOT NULL,
        action      TEXT NOT NULL CHECK (action IN ('ask', 'score')),
        run_id      TEXT NOT NULL,
        variant_sha TEXT REFERENCES variants (variant_sha),
        questions   INTEGER NOT NULL
    )
    """,
)
# The tables v3 added, created on their own when a v2 store is brought to v3.
_PROBE_DDL: Final = (
    """
    CREATE TABLE probes (
        probe_sha    TEXT PRIMARY KEY,
        name         TEXT NOT NULL UNIQUE,
        system       TEXT NOT NULL,
        instructions TEXT NOT NULL,
        created_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE probe_answers (
        variant_sha    TEXT NOT NULL REFERENCES variants (variant_sha),
        probe_sha      TEXT NOT NULL REFERENCES probes (probe_sha),
        run_id         TEXT NOT NULL,
        input_id       TEXT NOT NULL,
        repeat         INTEGER NOT NULL CHECK (repeat >= 0),
        asked_at       TEXT NOT NULL,
        segment        TEXT NOT NULL,
        raw_response   TEXT,
        truncated      INTEGER NOT NULL,
        model_reported TEXT,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        invalid_reason TEXT CHECK (invalid_reason IN ('invalid_probe', 'refused')),
        invalid_detail TEXT,
        forecast_json  TEXT,
        CHECK ((invalid_reason IS NULL) = (forecast_json IS NOT NULL)),
        CHECK (raw_response IS NOT NULL OR invalid_reason = 'refused'),
        PRIMARY KEY (variant_sha, probe_sha, run_id, input_id, repeat)
    )
    """,
)
_PROBE_TABLES: Final = frozenset({"probes", "probe_answers"})
_V2_TABLES: Final = frozenset({"variants", "answers", "failures", "splits", "ledger"})
_TABLES: Final = _V2_TABLES | _PROBE_TABLES
_BUSY_TIMEOUT_MS: Final = 5000


class ReplayStoreError(Exception):
    """The store cannot be opened or written as asked; the sentence says why."""


@dataclass(frozen=True)
class StoredAnswer:
    """One ``answers`` row to write: the question it answers and what came back."""

    run_id: str
    input_id: str
    repeat: int
    asked_at: datetime
    segment: str
    raw_response: str
    truncated: bool
    invalid_reason: str | None
    gate: RiskGateResult
    model_reported: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class StoredProbeAnswer:
    """One ``probe_answers`` row to write.

    ``forecast`` is the normalised forecast, set exactly when
    ``invalid_reason`` is ``None``; ``raw_response`` is ``None`` only on a
    ``refused`` row, whose ``invalid_detail`` is the provider's error.
    """

    run_id: str
    input_id: str
    repeat: int
    asked_at: datetime
    segment: str
    raw_response: str | None
    truncated: bool
    invalid_reason: str | None
    invalid_detail: str | None
    forecast: Forecast | None
    model_reported: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class HoldoutLook:
    """A ``ledger`` row as read back."""

    at: str
    who: str
    action: str
    variant_name: str  # "paper" for a look at the paper trader's own answers
    questions: int


def _text(value: Decimal | int | None) -> str | None:
    # The gate's margins are ints on the grid, its confidence a ``Decimal``.
    return None if value is None else str(value)


def _enum_text(value: object) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


class ReplayStore:
    """An open ``replay.sqlite``. Use as a context manager.

    ``create`` decides what a missing file means: ``replay``, ``register``
    and ``score --holdout`` without a variant (it has a look to record)
    create it, and any other reader refuses rather than leave an empty store
    behind a typo. The same three bring a v2 store to v3; any other open
    reads it as written (module docstring).
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
        self._create = create
        # False only for a v2 store opened by a reader (module docstring).
        self.probe_tables = True
        if path.exists() and not path.is_file():
            raise ReplayStoreError(f"{path} is not a regular file")
        if not path.exists() and not create:
            raise ReplayStoreError(f"replay store {str(path)!r} does not exist")
        try:
            self.conn = sqlite3.connect(str(path), isolation_level=None)
        except sqlite3.Error as exc:
            raise ReplayStoreError(f"{path} cannot be opened: {exc}") from exc
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._open_or_create()
        except sqlite3.Error as exc:
            self.close()
            raise ReplayStoreError(f"{path} cannot be opened as a replay store: {exc}") from exc
        except BaseException:
            self.close()
            raise

    def _open_or_create(self) -> None:
        # Read before anything writes: a foreign file is refused untouched.
        names = {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 2 and names >= _V2_TABLES and not names & _PROBE_TABLES:
            if not self._create:
                # A reader leaves a v2 store as it was written: it holds no
                # probe answer, and the probe reads say so by finding none.
                self.probe_tables = False
                return
            # The one migration: v3 only added tables, so a v2 store gains
            # them and keeps everything it holds.
            with self.transaction() as conn:
                for ddl in _PROBE_DDL:
                    conn.execute(ddl)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            names |= _PROBE_TABLES
            version = SCHEMA_VERSION
        if names and 0 < version < SCHEMA_VERSION:
            raise ReplayStoreError(
                f"{self.path} was written at replay schema v{version}, before this build's "
                f"v{SCHEMA_VERSION}, and there is no migration for it; point --replay-db at a "
                "new file"
            )
        if names and not names >= _TABLES:
            raise ReplayStoreError(
                f"{self.path} is a SQLite database but not a replay store (it lacks "
                f"{sorted(_TABLES - names)}); refusing to write into someone else's file"
            )
        if version > SCHEMA_VERSION:
            raise ReplayStoreError(
                f"{self.path} was written at replay schema v{version}; this build knows "
                f"v{SCHEMA_VERSION}"
            )
        if not names:
            if not self._create:
                raise ReplayStoreError(
                    f"{self.path} holds no replay store (the file has no tables); a reader "
                    "does not create one"
                )
            with self.transaction() as conn:
                for ddl in (*_DDL, *_PROBE_DDL):
                    conn.execute(ddl)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ReplayStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with suppress(sqlite3.Error):
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, IMMEDIATE so the write lock is taken up front.

        A SQLite failure inside it (a lock, a constraint, a full disk) is
        rolled back and raised as :class:`ReplayStoreError` naming the store:
        the commands word it as a failed write, not as a failed read.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise ReplayStoreError(f"{self.path}: write failed: {exc}") from exc
        try:
            yield self.conn
        except BaseException as exc:
            with suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise ReplayStoreError(f"{self.path}: write failed, rolled back: {exc}") from exc
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            # A failed COMMIT can leave the transaction open on the
            # connection; close it, or the next write fails on BEGIN.
            with suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise ReplayStoreError(f"{self.path}: write failed at commit: {exc}") from exc

    # -- variants ------------------------------------------------------------

    def register(self, variant: Variant, *, now: datetime) -> list[str]:
        """Store ``variant`` if new; return the notes worth printing.

        A name already standing for a different sha is refused, and so is a
        sha already stored under a different name: one name, one variant. A
        corrected ``model_cutoff`` is written over the stored one and said
        (it is not part of the sha, so the answers stand).
        """
        cutoff = None if variant.model_cutoff is None else variant.model_cutoff.isoformat()
        with self.transaction() as conn:
            by_name = conn.execute(
                "SELECT variant_sha FROM variants WHERE name = ?", (variant.name,)
            ).fetchone()
            if by_name is not None and by_name[0] != variant.sha:
                raise ReplayStoreError(
                    f"variant name {variant.name!r} already stands for "
                    f"{short_sha(by_name[0])} in {self.path}, and this file is "
                    f"{variant.short_sha} (its model, prompt, temperature, cap or extra context "
                    "changed); a changed variant needs a new name"
                )
            by_sha = conn.execute(
                "SELECT name, model_cutoff FROM variants WHERE variant_sha = ?", (variant.sha,)
            ).fetchone()
            if by_sha is not None and by_sha["name"] != variant.name:
                raise ReplayStoreError(
                    f"this variant ({variant.short_sha}) is already stored as "
                    f"{by_sha['name']!r}; ask it under that name"
                )
            if by_sha is None:
                conn.execute(
                    "INSERT INTO variants (variant_sha, name, provider, model, temperature, "
                    "max_tokens, system_prompt, extra_context, model_cutoff, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        variant.sha,
                        variant.name,
                        variant.provider,
                        variant.model,
                        variant.temperature,
                        variant.max_tokens,
                        variant.system_prompt,
                        variant.extra_context,
                        cutoff,
                        now.isoformat(),
                    ),
                )
                return []
            if by_sha["model_cutoff"] != cutoff:
                conn.execute(
                    "UPDATE variants SET model_cutoff = ? WHERE variant_sha = ?",
                    (cutoff, variant.sha),
                )
                return [
                    f"model_cutoff of {variant.name!r} changed from {by_sha['model_cutoff']} to "
                    f"{cutoff}; the answers stand (the cutoff is not part of the variant's sha)"
                ]
            return []

    def variant(self, name: str) -> Variant:
        """The variant stored as ``name``; refused by name, listing the ones there are.

        Rebuilt from its row (the prompt's text is stored, not its path) and
        held to the sha it is keyed by: a row edited by hand is refused
        rather than scored under a name it no longer describes.
        """
        row = self.conn.execute("SELECT * FROM variants WHERE name = ?", (name,)).fetchone()
        if row is None:
            known = [r[0] for r in self.conn.execute("SELECT name FROM variants ORDER BY name")]
            raise ReplayStoreError(
                f"no variant named {name!r} in {self.path}; it holds "
                + (", ".join(repr(k) for k in known) if known else "none")
            )
        cutoff = row["model_cutoff"]
        variant = Variant(
            name=row["name"],
            provider=row["provider"],
            model=row["model"],
            system_prompt=row["system_prompt"],
            temperature=row["temperature"],
            max_tokens=row["max_tokens"],
            extra_context=row["extra_context"],
            model_cutoff=None if cutoff is None else date.fromisoformat(cutoff),
        )
        if variant.sha != row["variant_sha"]:
            raise ReplayStoreError(
                f"variant {name!r} in {self.path} is keyed {short_sha(row['variant_sha'])} but its "
                f"row describes {variant.short_sha}; the row was changed after it was stored"
            )
        return variant

    # -- answers -------------------------------------------------------------

    def answered(self, variant_sha: str, run_id: str) -> set[tuple[str, int]]:
        """``(input_id, repeat)`` of every answer already stored for this variant and run."""
        return {
            (row[0], row[1])
            for row in self.conn.execute(
                "SELECT input_id, repeat FROM answers WHERE variant_sha = ? AND run_id = ?",
                (variant_sha, run_id),
            )
        }

    def write_answer(self, variant_sha: str, answer: StoredAnswer) -> None:
        """One answer in its own transaction, so an interrupted replay keeps what it paid for."""
        gate = answer.gate
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO answers (variant_sha, run_id, input_id, repeat, asked_at, segment, "
                "raw_response, truncated, model_reported, input_tokens, output_tokens, "
                "invalid_reason, decision_mode, target_side, requested_target_margin_pct, "
                "approved_target_margin_pct, risk_action, risk_reason, confidence, "
                "order_created, no_order_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    variant_sha,
                    answer.run_id,
                    answer.input_id,
                    answer.repeat,
                    answer.asked_at.isoformat(),
                    answer.segment,
                    answer.raw_response,
                    int(answer.truncated),
                    answer.model_reported,
                    answer.input_tokens,
                    answer.output_tokens,
                    answer.invalid_reason,
                    _enum_text(gate.decision_mode),
                    _enum_text(gate.target_side),
                    _text(gate.requested_target_margin_pct),
                    _text(gate.approved_target_margin_pct),
                    _enum_text(gate.risk_action),
                    gate.risk_reason,
                    _text(gate.confidence),
                    int(gate.order_created),
                    gate.no_order_reason,
                ),
            )

    def record_failure(
        self,
        variant_sha: str,
        *,
        run_id: str,
        input_id: str,
        repeat: int,
        error: str,
        now: datetime,
    ) -> None:
        """One question the provider refused for its own sake: not asked again, scored unanswered."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO failures (variant_sha, run_id, input_id, repeat, failed_at, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (variant_sha, run_id, input_id, repeat, now.isoformat(), error),
            )

    def failed(self, variant_sha: str, run_id: str) -> Mapping[int, set[str]]:
        """The ``input_id`` of every recorded failure for this variant and run, keyed by repeat."""
        by_repeat: dict[int, set[str]] = {}
        for row in self.conn.execute(
            "SELECT input_id, repeat FROM failures WHERE variant_sha = ? AND run_id = ?",
            (variant_sha, run_id),
        ):
            by_repeat.setdefault(row["repeat"], set()).add(row["input_id"])
        return by_repeat

    def clear_failures(self, variant_sha: str, run_id: str) -> int:
        """Forget this variant's recorded failures on the run, so they are asked again."""
        with self.transaction() as conn:
            return conn.execute(
                "DELETE FROM failures WHERE variant_sha = ? AND run_id = ?", (variant_sha, run_id)
            ).rowcount

    def answers(self, variant_sha: str, run_id: str) -> Mapping[int, Sequence[Answer]]:
        """The stored answers of one variant on one run, as scorecard records, keyed by repeat.

        Decoded by the paper store's own row decoder: an answer row keeps the
        ``ai_outputs`` columns under their ``ai_outputs`` names, so one
        decoder serves both stores.
        """
        by_repeat: dict[int, list[Answer]] = {}
        for row in self.conn.execute(
            "SELECT * FROM answers WHERE variant_sha = ? AND run_id = ? ORDER BY repeat, input_id",
            (variant_sha, run_id),
        ):
            by_repeat.setdefault(row["repeat"], []).append(answer_from_row(row))
        return by_repeat

    # -- probes --------------------------------------------------------------

    def register_probe(self, probe: Probe, *, now: datetime) -> None:
        """Store ``probe`` if new. One name, one probe, as for variants."""
        with self.transaction() as conn:
            by_name = conn.execute(
                "SELECT probe_sha FROM probes WHERE name = ?", (probe.name,)
            ).fetchone()
            if by_name is not None and by_name[0] != probe.sha:
                raise ReplayStoreError(
                    f"probe name {probe.name!r} already stands for {short_sha(by_name[0])} in "
                    f"{self.path}, and this file is {probe.short_sha} (its system or "
                    "instructions text changed); a changed probe needs a new name"
                )
            by_sha = conn.execute(
                "SELECT name FROM probes WHERE probe_sha = ?", (probe.sha,)
            ).fetchone()
            if by_sha is not None and by_sha[0] != probe.name:
                raise ReplayStoreError(
                    f"this probe ({probe.short_sha}) is already stored as {by_sha[0]!r}; ask it "
                    "under that name"
                )
            if by_sha is None:
                conn.execute(
                    "INSERT INTO probes (probe_sha, name, system, instructions, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (probe.sha, probe.name, probe.system, probe.instructions, now.isoformat()),
                )

    def probe_done(
        self, variant_sha: str, probe_sha: str, run_id: str
    ) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
        """``(answered, refused)``: the ``(input_id, repeat)`` of every probe row stored.

        ``answered`` holds the forecasts and the ``invalid_probe`` answers
        (neither is asked again); ``refused`` holds the provider's refusals.
        """
        answered: set[tuple[str, int]] = set()
        refused: set[tuple[str, int]] = set()
        if not self.probe_tables:
            return answered, refused
        for row in self.conn.execute(
            "SELECT input_id, repeat, invalid_reason FROM probe_answers "
            "WHERE variant_sha = ? AND probe_sha = ? AND run_id = ?",
            (variant_sha, probe_sha, run_id),
        ):
            target = refused if row["invalid_reason"] == REFUSED else answered
            target.add((row["input_id"], row["repeat"]))
        return answered, refused

    def write_probe_answer(
        self, variant_sha: str, probe_sha: str, answer: StoredProbeAnswer
    ) -> None:
        """One probe answer in its own transaction, as :meth:`write_answer` writes a decision."""
        forecast = (
            None
            if answer.forecast is None
            else json.dumps(answer.forecast, sort_keys=True, separators=(",", ":"))
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO probe_answers (variant_sha, probe_sha, run_id, input_id, repeat, "
                "asked_at, segment, raw_response, truncated, model_reported, input_tokens, "
                "output_tokens, invalid_reason, invalid_detail, forecast_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    variant_sha,
                    probe_sha,
                    answer.run_id,
                    answer.input_id,
                    answer.repeat,
                    answer.asked_at.isoformat(),
                    answer.segment,
                    answer.raw_response,
                    int(answer.truncated),
                    answer.model_reported,
                    answer.input_tokens,
                    answer.output_tokens,
                    answer.invalid_reason,
                    answer.invalid_detail,
                    forecast,
                ),
            )

    def clear_probe_refusals(self, variant_sha: str, probe_sha: str, run_id: str) -> int:
        """Forget the provider's refusals of this probe on the run, so they are asked again."""
        with self.transaction() as conn:
            return conn.execute(
                "DELETE FROM probe_answers WHERE variant_sha = ? AND probe_sha = ? AND "
                "run_id = ? AND invalid_reason = ?",
                (variant_sha, probe_sha, run_id, REFUSED),
            ).rowcount

    def probe_answers(
        self, variant_sha: str, run_id: str
    ) -> list[tuple[Probe, Mapping[int, Sequence[ProbeAnswer]]]]:
        """Every probe this variant was asked on the run, by name, with its answers by repeat.

        A probe row edited after it was stored is refused, as a variant row is.
        """
        found: dict[str, tuple[Probe, dict[int, list[ProbeAnswer]]]] = {}
        if not self.probe_tables:
            return []
        for row in self.conn.execute(
            "SELECT a.input_id, a.repeat, a.invalid_reason, a.forecast_json, p.probe_sha, "
            "p.name, p.system, p.instructions FROM probe_answers AS a "
            "JOIN probes AS p ON p.probe_sha = a.probe_sha "
            "WHERE a.variant_sha = ? AND a.run_id = ? ORDER BY p.name, a.repeat, a.input_id",
            (variant_sha, run_id),
        ):
            sha = row["probe_sha"]
            if sha not in found:
                probe = Probe(row["name"], row["system"], row["instructions"])
                if probe.sha != sha:
                    raise ReplayStoreError(
                        f"probe {row['name']!r} in {self.path} is keyed {short_sha(sha)} but its "
                        f"row describes {probe.short_sha}; the row was changed after it was stored"
                    )
                found[sha] = (probe, {})
            try:
                raw = row["forecast_json"]
                forecast = None if raw is None else json.loads(raw)
                answer = ProbeAnswer(row["input_id"], forecast, row["invalid_reason"])
            except (ValueError, ProbeError) as exc:
                raise ReplayStoreError(
                    f"{self.path}: the probe answer to {row['input_id']} cannot be read ({exc})"
                ) from exc
            found[sha][1].setdefault(row["repeat"], []).append(answer)
        return list(found.values())

    # -- the split ------------------------------------------------------------

    def pinned_split(self, run_id: str) -> tuple[Split, str] | None:
        """``(split, pinned_at)`` the run was pinned under, or ``None`` if it has not been."""
        row = self.conn.execute(
            "SELECT split_json, pinned_at FROM splits WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return Split.from_dict(json.loads(row["split_json"])), row["pinned_at"]
        except (ValueError, SplitError) as exc:
            raise ReplayStoreError(
                f"{self.path}: the split pinned for run {run_id!r} cannot be read ({exc})"
            ) from exc

    def pin_split(self, run_id: str, split: Split, *, now: datetime) -> tuple[Split, str]:
        """Pin ``split`` for the run unless one is pinned already; return the one that stands."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO splits (run_id, split_json, pinned_at) VALUES (?, ?, ?)",
                (run_id, json.dumps(split.to_dict(), sort_keys=True), now.isoformat()),
            )
        pinned = self.pinned_split(run_id)
        assert pinned is not None  # inserted above, or already there
        return pinned

    # -- ledger --------------------------------------------------------------

    def record_look(
        self,
        *,
        action: str,
        run_id: str,
        variant_sha: str | None,
        questions: int,
        who: str,
        now: datetime,
    ) -> None:
        """One ledger row: ``who`` looked at ``questions`` holdout questions of ``run_id``.

        ``variant_sha`` is ``None`` for a look at the paper trader's own answers.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ledger (at, who, action, run_id, variant_sha, questions) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(), who, action, run_id, variant_sha, questions),
            )

    def looks(self, run_id: str) -> list[HoldoutLook]:
        """Every holdout look recorded for ``run_id``, oldest first."""
        return [
            HoldoutLook(
                at=row["at"],
                who=row["who"],
                action=row["action"],
                variant_name=row["name"] or "paper",
                questions=row["questions"],
            )
            for row in self.conn.execute(
                "SELECT l.at, l.who, l.action, l.questions, v.name FROM ledger AS l "
                "LEFT JOIN variants AS v ON v.variant_sha = l.variant_sha WHERE l.run_id = ? "
                "ORDER BY l.entry_id",
                (run_id,),
            )
        ]
