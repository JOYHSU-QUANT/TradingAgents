"""Tests for the offline ``ai_inputs.format_fingerprint`` backfill (issue #163).

A run that crossed the schema v11 deployment point has ``NULL`` fingerprints
on every row written before it — one extra ``prompt_regime:`` bucket that is
really the same regime. The payload JSON kept the format text, so the digest
can be recomputed; these pin what the pass trusts, what it counts, and that
it can never rewrite a value the daemon stamped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    decision_format_instructions,
    format_fingerprint,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.validation import validate_run
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.backfill import (
    FingerprintBackfill,
    backfill_format_fingerprints,
)
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from ..conftest import insert_decision_attempts, stamp_prompt_regimes, write_payload

_T0 = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
_V4 = "phase2-target-v4"
_SHAPE = "price|market|funding|indicators(rsi_14)|position"
# The format block AS THE MODEL SAW IT on those cycles — a fixed text, not
# whatever this build would render today (see the first test).
_FORMAT = "Respond with one JSON block: decision_mode, target_side, ...\n"
_FORMAT_DIGEST = format_fingerprint(_FORMAT)
_NOTHING = FingerprintBackfill(0, 0, 0, 0, 0)


def _payload(tmp_path: Path, name: str, *, body=None) -> tuple[str, str]:
    """A payload file in the daemon's shape; ``(path, digest)`` for the row."""
    if body is None:
        body = {
            "coin": "BTC",
            "as_of": _T0.isoformat(),
            "prompt_version": _V4,
            "context_shape": _SHAPE,
            "context_text": "ctx",
            "format_instructions": _FORMAT,
        }
    return write_payload(tmp_path / "payloads" / f"{name}.json", body)


def _seed(tmp_path: Path, regimes: list[tuple]) -> Database:
    """One completed attempt + ``ai_inputs`` row per regime, in scheduled order."""
    db = Database(tmp_path / "b.db")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=Decimal(1000),
        schema_version=SCHEMA_VERSION,
    )
    insert_decision_attempts(db, ["completed"] * len(regimes), start=_T0)
    stamp_prompt_regimes(db, regimes)
    return db


def _fingerprints(db: Database) -> list[str | None]:
    return [
        row[0]
        for row in db.conn.execute(
            "SELECT format_fingerprint FROM ai_inputs WHERE run_id = 'r' ORDER BY timestamp, rowid"
        )
    ]


def test_the_pass_stamps_the_digest_of_the_text_each_payload_recorded(tmp_path):
    # Two pre-v11 rows and one the daemon stamped after the deployment point:
    # ``validate`` sees two buckets of one regime. The stored text is NOT what
    # this build renders (the daemon's own digest would be), which is what
    # proves the pass digests the payload, not a re-render under today's
    # config.
    rendered_today = format_fingerprint(decision_format_instructions(DecisionConfig(), max_pct=60))
    assert rendered_today != _FORMAT_DIGEST
    p1, h1 = _payload(tmp_path, "one")
    p2, h2 = _payload(tmp_path, "two")
    db = _seed(
        tmp_path,
        [(_V4, _SHAPE, None, p1, h1), (_V4, _SHAPE, None, p2, h2), (_V4, _SHAPE, _FORMAT_DIGEST)],
    )
    before = validate_run(db, run_id="r").prompt_regimes
    assert [r.format_fingerprint for r in before] == [None, _FORMAT_DIGEST]

    report = backfill_format_fingerprints(db, run_id="r")

    assert report == FingerprintBackfill(
        stamped=2, pre_v10=0, missing_payload=0, unreadable=0, unverified=0
    )
    assert _fingerprints(db) == [_FORMAT_DIGEST] * 3
    # The acceptance criterion: one regime, one line.
    assert validate_run(db, run_id="r").prompt_regimes == (
        repo.PromptRegime(_V4, _SHAPE, _FORMAT_DIGEST, 3),
    )
    # Idempotent: nothing left NULL, nothing rewritten.
    assert backfill_format_fingerprints(db, run_id="r") == _NOTHING
    db.close()


def test_a_pre_v10_row_is_not_half_stamped(tmp_path):
    # No shape either: the three keys are one set, and a fingerprint on a
    # shapeless row would be a bucket the daemon never writes — ``validate``
    # would print it as a NEW regime instead of folding one. Left NULL and
    # counted; the writer refuses it on its own too.
    path, digest = _payload(tmp_path, "old")
    db = _seed(tmp_path, [(_V4, None, None, path, digest)])
    report = backfill_format_fingerprints(db, run_id="r")
    assert report == FingerprintBackfill(
        stamped=0, pre_v10=1, missing_payload=0, unreadable=0, unverified=0
    )
    assert _fingerprints(db) == [None]
    [input_id] = [r[0] for r in db.conn.execute("SELECT input_id FROM ai_inputs")]
    with db.transaction() as conn:
        assert repo.stamp_ai_input_format_fingerprint(conn, input_id, _FORMAT_DIGEST) == 0
    assert _fingerprints(db) == [None]
    db.close()


def test_a_row_whose_payload_is_gone_stays_null_and_is_counted(tmp_path):
    db = _seed(
        tmp_path,
        [
            (_V4, _SHAPE, None, str(tmp_path / "payloads" / "rotated-away.json"), "sha256:00"),
            (_V4, _SHAPE, None),
        ],
    )
    report = backfill_format_fingerprints(db, run_id="r")
    assert report == FingerprintBackfill(
        stamped=0, pre_v10=0, missing_payload=2, unreadable=0, unverified=0
    )
    assert _fingerprints(db) == [None, None]
    assert "missing_payload=2" in report.summary("r")
    db.close()


def test_a_payload_that_no_longer_hashes_to_its_row_is_not_trusted(tmp_path):
    # The row describes the artifact it hashed at build time. A file that was
    # edited since (or a row that never recorded a hash) is not evidence of
    # what the model was shown — left NULL, counted, never guessed.
    path, digest = _payload(tmp_path, "edited")
    write_payload(Path(path), {"format_instructions": "something else"})
    unhashed, _ = _payload(tmp_path, "unhashed")
    db = _seed(tmp_path, [(_V4, _SHAPE, None, path, digest), (_V4, _SHAPE, None, unhashed, None)])
    report = backfill_format_fingerprints(db, run_id="r")
    assert report == FingerprintBackfill(
        stamped=0, pre_v10=0, missing_payload=0, unreadable=0, unverified=2
    )
    assert _fingerprints(db) == [None, None]
    db.close()


def test_a_payload_without_a_format_block_is_unreadable_not_guessed(tmp_path):
    no_key, h1 = _payload(tmp_path, "no-key", body={"coin": "BTC", "context_text": "ctx"})
    wrong_type, h2 = _payload(tmp_path, "wrong-type", body={"format_instructions": ["x"]})
    not_json, h3 = _payload(tmp_path, "not-json", body=b"not json at all")
    db = _seed(
        tmp_path,
        [
            (_V4, _SHAPE, None, no_key, h1),
            (_V4, _SHAPE, None, wrong_type, h2),
            (_V4, _SHAPE, None, not_json, h3),
        ],
    )
    report = backfill_format_fingerprints(db, run_id="r")
    assert report == FingerprintBackfill(
        stamped=0, pre_v10=0, missing_payload=0, unreadable=3, unverified=0
    )
    assert _fingerprints(db) == [None] * 3
    db.close()


def test_a_value_the_daemon_stamped_is_never_rewritten(tmp_path):
    # A stamped row is not even a candidate (the reader filters on NULL), and
    # the writer's own predicate refuses it too — so a caller that bypasses
    # the reader cannot replace the daemon's digest with a recomputation.
    path, digest = _payload(tmp_path, "stamped")
    db = _seed(tmp_path, [(_V4, _SHAPE, "cafe0000cafe0000", path, digest)])
    assert backfill_format_fingerprints(db, run_id="r") == _NOTHING
    [input_id] = [r[0] for r in db.conn.execute("SELECT input_id FROM ai_inputs")]
    with db.transaction() as conn:
        assert repo.stamp_ai_input_format_fingerprint(conn, input_id, _FORMAT_DIGEST) == 0
    assert _fingerprints(db) == ["cafe0000cafe0000"]
    # The writer names an empty digest rather than storing it as a value.
    with db.transaction() as conn, pytest.raises(ValueError, match="non-empty"):
        repo.stamp_ai_input_format_fingerprint(conn, input_id, "")
    db.close()


def test_the_pass_is_scoped_to_the_run_it_was_asked_about(tmp_path):
    # Two runs share a store (the live layout); the other run's NULL rows are
    # not this pass's business.
    path, digest = _payload(tmp_path, "mine")
    db = _seed(tmp_path, [(_V4, _SHAPE, None, path, digest)])
    accounting.initialize_run(
        db,
        run_id="other",
        mode="paper",
        initial_balance_usdc=Decimal(1000),
        schema_version=SCHEMA_VERSION,
    )
    insert_decision_attempts(db, ["completed"], start=_T0, run_id="other")
    stamp_prompt_regimes(db, [(_V4, _SHAPE, None, path, digest)], run_id="other")
    assert backfill_format_fingerprints(db, run_id="r").stamped == 1
    other = db.conn.execute(
        "SELECT format_fingerprint FROM ai_inputs WHERE run_id = 'other'"
    ).fetchone()[0]
    assert other is None
    db.close()
