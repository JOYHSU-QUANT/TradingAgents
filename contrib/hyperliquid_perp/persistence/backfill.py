"""Offline backfill of ``ai_inputs.format_fingerprint`` from the payload files.

A run that crossed the schema v11 deployment point carries rows written
before the column existed (``NULL``), so ``validate`` prints them as one
extra ``prompt_regime:`` bucket with ``format_fingerprint=n/a`` — the same
regime as the rows after the point, indistinguishable in the store (issue
#163). The payload JSON each row points at stores the ``format_instructions``
text the model was shown that cycle, and ``target_decision.format_fingerprint``
is a pure digest of that text, so the value can be recomputed offline.

NOT a migration, on purpose: a schema step must not do file I/O, and a
missing payload file has to be tolerated rather than fail the upgrade. Run
by hand through ``export --backfill-format-fingerprint`` (RUNBOOK §6),
against a store already at v11 — the command refuses an older one.

Trust rules, per row — a row that fails one stays ``NULL`` and is counted
under that reason, never guessed:

- ``pre_v10``: the row has no ``context_shape`` (or no ``prompt_version``)
  either. The three keys are one set (``DecisionInput``); a row carrying a
  fingerprint but no shape would be a half-stamped triple the daemon never
  writes, and ``validate`` would print it as a NEW bucket instead of folding
  one. The writer refuses it too.
- ``missing_payload``: no path on the row, or no file at it.
- ``unreadable``: the file cannot be read (permissions), is not JSON, or
  carries no string ``format_instructions``.
- ``unverified``: the file's bytes do not hash to the row's own
  ``input_payload_hash`` (``common.digest.payload_digest``) — edited,
  truncated, restored from elsewhere — or the row recorded no hash. The row
  describes THAT artifact; anything else is not evidence of what the model
  was shown.

Only ``NULL`` cells are ever written (the repository writer carries the
``IS NULL`` predicate), so a second pass stamps nothing and a value the
daemon wrote can never be replaced by a recomputation. Every file is read
BEFORE the one write transaction opens: the store may be a running daemon's,
and holding its write lock across file reads would be a needless stall.
Paths are the absolute ones the daemon recorded, so run this on the host
that wrote them (or with the payload tree at the same path).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..common.digest import payload_digest

logger = logging.getLogger(__name__)

__all__ = ["FingerprintBackfill", "backfill_format_fingerprints"]


@dataclass(frozen=True)
class FingerprintBackfill:
    """What one pass did, and what it left ``NULL`` and why (module docstring)."""

    stamped: int
    pre_v10: int
    missing_payload: int
    unreadable: int
    unverified: int

    def summary(self, run_id: str) -> str:
        return (
            f"format_fingerprint backfill for {run_id!r}: stamped={self.stamped}"
            f" pre_v10={self.pre_v10} missing_payload={self.missing_payload}"
            f" unreadable={self.unreadable} unverified={self.unverified}"
            " (rows left NULL are counted, never guessed)"
        )


def _recorded_format_text(row) -> tuple[str | None, str]:
    """The payload's ``format_instructions``, or ``(None, <reason left NULL>)``."""
    if row["context_shape"] is None or row["prompt_version"] is None:
        return None, "pre_v10"
    path = row["input_payload_path"]
    if path is None:
        return None, "missing_payload"
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, "missing_payload"
    except OSError as exc:
        logger.warning("payload %s for %s could not be read: %s", path, row["input_id"], exc)
        return None, "unreadable"
    digest = payload_digest(raw)
    if row["input_payload_hash"] != digest:
        logger.warning(
            "payload %s does not hash to the digest recorded on %s (%s vs %s) — left NULL",
            path,
            row["input_id"],
            digest,
            row["input_payload_hash"],
        )
        return None, "unverified"
    try:
        text = json.loads(raw)["format_instructions"]
    except (ValueError, KeyError, TypeError):
        return None, "unreadable"
    if not isinstance(text, str):
        return None, "unreadable"
    return text, ""


def backfill_format_fingerprints(db, *, run_id: str) -> FingerprintBackfill:
    """Stamp every ``NULL`` ``format_fingerprint`` the run's payloads can prove."""
    from ..domains.perp.target_decision import format_fingerprint
    from . import repository as repo

    counts = {"pre_v10": 0, "missing_payload": 0, "unreadable": 0, "unverified": 0}
    stamps: list[tuple[str, str]] = []
    for row in repo.ai_inputs_without_format_fingerprint(db.conn, run_id):
        text, why_not = _recorded_format_text(row)
        if text is None:
            counts[why_not] += 1
            continue
        stamps.append((row["input_id"], format_fingerprint(text)))
    stamped = 0
    if stamps:
        with db.transaction() as conn:
            for input_id, fingerprint in stamps:
                stamped += repo.stamp_ai_input_format_fingerprint(conn, input_id, fingerprint)
    return FingerprintBackfill(stamped=stamped, **counts)
