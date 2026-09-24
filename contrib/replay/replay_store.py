"""``replay.sqlite``: the past papers' own store (replay plan PR 2).

Three tables, and nothing here ever writes the paper store:

- ``variants``: one row per variant this store has asked with, keyed by its
  :attr:`~.variant.Variant.sha`. The name is UNIQUE too, so a name means
  one variant for the life of the store: a changed file under an old name
  is refused rather than filed as the old variant or quietly beside it.
- ``answers``: one row per question, variant and repeat; the key is
  ``(variant_sha, run_id, input_id, repeat)``, which is what makes a replay
  resumable (an answer already stored is never asked for again). A row
  keeps the model's raw text, what the parse seam made of it, and every
  field the gate returned that the scorecard reads, in the gate's own
  spelling: enum values as text, margins and confidence as ``Decimal``
  text.
- ``ledger``: one row per look at a holdout (plan §3-9). ``ask`` is written
  before the first holdout payload is read, and ``score`` before a holdout
  answer is scored; either way the row exists even if what follows fails.

The schema is versioned by ``PRAGMA user_version``. A file that holds
tables but not ours is refused before anything writes to it, and a store
a newer build wrote is refused rather than written through.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from .paper_store import answer_from_row
from .score import Answer
from .upstream import RiskGateResult
from .variant import Variant, short_sha

__all__ = [
    "SCHEMA_VERSION",
    "HoldoutLook",
    "ReplayStore",
    "ReplayStoreError",
    "StoredAnswer",
]

SCHEMA_VERSION: Final = 1

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
    CREATE TABLE ledger (
        entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        who         TEXT NOT NULL,
        action      TEXT NOT NULL CHECK (action IN ('ask', 'score')),
        run_id      TEXT NOT NULL,
        variant_sha TEXT NOT NULL REFERENCES variants (variant_sha),
        questions   INTEGER NOT NULL
    )
    """,
)
_TABLES: Final = frozenset({"variants", "answers", "ledger"})
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
class HoldoutLook:
    """A ``ledger`` row as read back."""

    at: str
    who: str
    action: str
    variant_name: str
    questions: int


def _text(value: Decimal | int | None) -> str | None:
    # The gate's margins are ints on the grid, its confidence a ``Decimal``.
    return None if value is None else str(value)


def _enum_text(value: object) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


class ReplayStore:
    """An open ``replay.sqlite``. Use as a context manager.

    ``create`` decides what a missing file means: the ``replay`` command
    creates it, and a reader (``score --replay-db``) refuses rather than
    leave an empty store behind a typo.
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
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
            with self.transaction() as conn:
                for ddl in _DDL:
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
        """One write transaction, IMMEDIATE so the write lock is taken up front."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            with suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

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

    # -- ledger --------------------------------------------------------------

    def record_look(
        self, *, action: str, run_id: str, variant_sha: str, questions: int, who: str, now: datetime
    ) -> None:
        """One ledger row: ``who`` looked at ``questions`` holdout questions of ``run_id``."""
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
                variant_name=row["name"],
                questions=row["questions"],
            )
            for row in self.conn.execute(
                "SELECT l.at, l.who, l.action, l.questions, v.name FROM ledger AS l "
                "JOIN variants AS v ON v.variant_sha = l.variant_sha WHERE l.run_id = ? "
                "ORDER BY l.entry_id",
                (run_id,),
            )
        ]
