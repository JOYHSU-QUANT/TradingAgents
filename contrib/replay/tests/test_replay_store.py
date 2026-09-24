"""``replay.sqlite``: what it refuses to open, the one-name-one-variant rule, the round trip."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.replay.replay_store import SCHEMA_VERSION, ReplayStore, ReplayStoreError, StoredAnswer
from contrib.replay.upstream import (
    CurrentPositionState,
    DecisionConfig,
    RiskConfig,
    evaluate,
    parse_target_decision,
)

from .papers import decision_text, variant as make_variant

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _gate(text: str):
    decision = DecisionConfig()
    return evaluate(
        parse_target_decision(text, decision),
        account_equity=Decimal("1000"),
        current=CurrentPositionState.flat(),
        risk=RiskConfig(),
        decision_cfg=decision,
    )


def test_a_new_store_is_created_at_the_current_schema(tmp_path):
    path = tmp_path / "replay.sqlite"
    with ReplayStore(path, create=True):
        pass
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    finally:
        conn.close()


def test_a_missing_store_is_not_created_by_a_reader(tmp_path):
    with pytest.raises(ReplayStoreError, match="does not exist"):
        ReplayStore(tmp_path / "nope.sqlite")
    assert not (tmp_path / "nope.sqlite").exists()


def test_someone_elses_database_is_refused_untouched(tmp_path):
    path = tmp_path / "other.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE things (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ReplayStoreError, match=r"not a replay store \(it lacks"):
        ReplayStore(path, create=True)
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert tables == {"things"}


def test_a_store_from_a_newer_build_is_refused(tmp_path):
    path = tmp_path / "replay.sqlite"
    with ReplayStore(path, create=True):
        pass
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(ReplayStoreError, match=f"replay schema v{SCHEMA_VERSION + 1}"):
        ReplayStore(path)


def test_one_name_is_one_variant(tmp_path):
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        assert store.register(make_variant(), now=NOW) == []
        assert store.register(make_variant(), now=NOW) == []
        with pytest.raises(ReplayStoreError, match="variant name 'v' already stands for"):
            store.register(make_variant(system_prompt="Decide carefully."), now=NOW)
        with pytest.raises(ReplayStoreError, match="is already stored as 'v'"):
            store.register(make_variant(name="w"), now=NOW)


def test_a_corrected_cutoff_is_written_and_said(tmp_path):
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(make_variant(), now=NOW)
        (note,) = store.register(make_variant(model_cutoff=date(2026, 3, 31)), now=NOW)
        assert note.startswith("model_cutoff of 'v' changed from None to 2026-03-31")
        assert store.variant("v").model_cutoff == date(2026, 3, 31)


def test_an_answer_reads_back_as_the_scorecards_record(tmp_path):
    variant = make_variant()
    gate = _gate(decision_text("set_target", "long", 30, 0.8))
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.write_answer(
            variant.sha,
            StoredAnswer(
                run_id="run",
                input_id="in-1",
                repeat=1,
                asked_at=NOW,
                segment="train",
                raw_response="text",
                truncated=False,
                invalid_reason=None,
                gate=gate,
            ),
        )
        assert store.answered(variant.sha, "run") == {("in-1", 1)}
        assert store.answered(variant.sha, "other") == set()
        (answer,) = store.answers(variant.sha, "run")[1]
    assert answer.target_side is not None
    assert (answer.input_id, answer.decision_mode.value, answer.target_side.value) == (
        "in-1",
        "set_target",
        "long",
    )
    assert (answer.requested_margin_pct, answer.approved_margin_pct, answer.confidence) == (
        30.0,
        30.0,
        0.8,
    )
    assert (answer.risk_action.value, answer.order_created, answer.no_order_reason) == (
        "approved",
        True,
        None,
    )


def test_the_same_answer_cannot_be_stored_twice(tmp_path):
    variant = make_variant()
    answer = StoredAnswer(
        run_id="run",
        input_id="in-1",
        repeat=0,
        asked_at=NOW,
        segment="train",
        raw_response="no json",
        truncated=False,
        invalid_reason="invalid_output",
        gate=_gate("no json"),
    )
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.write_answer(variant.sha, answer)
        with pytest.raises(ReplayStoreError, match="write failed, rolled back: UNIQUE constraint"):
            store.write_answer(variant.sha, answer)
        assert len(store.answers(variant.sha, "run")[0]) == 1


def test_a_look_reads_back_with_the_variants_name(tmp_path):
    variant = make_variant()
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.record_look(
            action="ask", run_id="run", variant_sha=variant.sha, questions=4, who="joy", now=NOW
        )
        (look,) = store.looks("run")
        assert store.looks("other") == []
    assert (look.at, look.who, look.action, look.variant_name, look.questions) == (
        NOW.isoformat(),
        "joy",
        "ask",
        "v",
        4,
    )


def test_a_reader_does_not_create_the_tables_in_an_empty_file(tmp_path):
    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with pytest.raises(ReplayStoreError, match="holds no replay store"):
        ReplayStore(empty)
    assert empty.stat().st_size == 0


def test_a_directory_is_not_a_store(tmp_path: Path):
    with pytest.raises(ReplayStoreError, match="is not a regular file"):
        ReplayStore(tmp_path, create=True)


def test_a_variant_row_edited_after_it_was_stored_is_refused(tmp_path):
    path = tmp_path / "r.sqlite"
    with ReplayStore(path, create=True) as store:
        store.register(make_variant(), now=NOW)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE variants SET system_prompt = 'Decide differently.'")
    conn.commit()
    conn.close()
    with ReplayStore(path) as store, pytest.raises(
        ReplayStoreError, match="the row was changed after it was stored"
    ):
        store.variant("v")
