"""The past papers: a question put to a variant, its answer gated, stored, and resumed.

The fixture is :mod:`.papers`: a run whose answers the real gate wrote,
with its payloads on disk. The model is :class:`~.papers.Echo`, which says
for each question what the paper trader's model said, so the gate's
verdict on a replayed answer can be held against the verdict it recorded.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.replay import cli
from contrib.replay.paper_store import InputFacts, gate_config, load_decisions
from contrib.replay.replay import (
    BACKOFF_SECONDS,
    Completion,
    ReplayError,
    human_message,
    judge,
    position_state,
    prepare,
    select,
)
from contrib.replay.score import ScoreError, build_split
from contrib.replay.upstream import (
    Database,
    RiskConfig,
    SegmentName,
    TargetSide,
    payload_dir,
)

from . import papers
from .conftest import RUN_ID as FIXTURE_RUN, run_config, write_paper_store
from .papers import (
    FORMAT,
    HOLDOUT,
    PAPERS,
    RUN_ID,
    SYSTEM,
    TRAIN,
    VALIDATION,
    Echo,
    replay_argv,
    write_gate_store,
    write_variant,
)

STEP_MS = 4 * 3_600_000


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db")


@pytest.fixture
def variant_file(tmp_path: Path) -> Path:
    return write_variant(tmp_path, "echo")


@pytest.fixture
def echo(monkeypatch) -> Echo:
    model = Echo()

    def build(variant):
        model.built.append(variant.name)
        return model

    monkeypatch.setattr(cli, "_build_model", build)
    monkeypatch.setattr(cli, "_sleep", lambda seconds: None)
    return model


_replay = replay_argv


def _papers(store: Path, *segments: SegmentName):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
        risk, decision = gate_config(db, RUN_ID)
    split = build_split(decisions.questions, interval="4h", step_ms=STEP_MS)
    chosen = select(
        decisions.questions,
        decisions.inputs,
        split=split,
        step_ms=STEP_MS,
        segments=set(segments),
    )
    return decisions, chosen, risk, decision


def _answers(replay_db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(replay_db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM answers ORDER BY input_id, repeat").fetchall()
    finally:
        conn.close()


# -- the gate a replayed answer meets ---------------------------------------------


def test_a_replayed_answer_meets_the_gate_the_recorded_one_met(store):
    """Same question, same text, same verdict: every field the scorecard reads agrees."""
    decisions, chosen, risk, decision = _papers(store, *SegmentName)
    prepared = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)
    recorded = {a.input_id: a for a in decisions.answers}
    assert len(prepared) == len(PAPERS) == len(recorded)
    for item, paper in zip(prepared, PAPERS, strict=True):
        _, _, gate = judge(
            Completion(text=paper.text, truncated=paper.truncated),
            item,
            risk=risk,
            decision=decision,
        )
        was = recorded[item.input_id]
        requested = gate.requested_target_margin_pct
        approved = gate.approved_target_margin_pct
        assert (
            gate.decision_mode,
            gate.target_side,
            None if requested is None else float(requested),
            None if approved is None else float(approved),
            gate.risk_action,
            gate.risk_reason,
            gate.order_created,
            gate.no_order_reason,
        ) == (
            was.decision_mode,
            was.target_side,
            was.requested_margin_pct,
            was.approved_margin_pct,
            was.risk_action,
            was.risk_reason,
            was.order_created,
            was.no_order_reason,
        ), item.input_id


def test_the_gate_is_the_runs_own_not_the_defaults(store):
    """Slot 3's resize at 0.5 clears the default resize bar's base, not the run's 0.62."""
    _, chosen, risk, decision = _papers(store, SegmentName.TRAIN)
    assert decision.resize_min_confidence == Decimal("0.62")
    prepared = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)
    (item,) = [p for p in prepared if p.input_id == papers.input_id(3)]
    _, _, gate = judge(Completion(PAPERS[3].text), item, risk=risk, decision=decision)
    assert (gate.risk_action.value, gate.risk_reason) == ("rejected", "low_confidence_resize")


def test_a_cut_off_answer_is_filed_as_truncated_output(store):
    _, chosen, risk, decision = _papers(store, SegmentName.HOLDOUT)
    item = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)[0]
    text = PAPERS[8].text
    cut = judge(Completion(text, truncated=True), item, risk=risk, decision=decision)
    whole = judge(Completion(text, truncated=False), item, risk=risk, decision=decision)
    assert (cut[0], whole[0]) == ("truncated_output", "invalid_output")


# -- the messages ------------------------------------------------------------------


def test_the_human_message_is_the_context_the_engine_was_given(store):
    _, chosen, risk, _ = _papers(store, SegmentName.TRAIN)
    item = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)[0]
    context = papers.context_text(0)
    assert human_message(item, None) == f"## Perpetual market context\n{context}\n\n{FORMAT}"
    # A lesson goes after the market context, and the format block stays last.
    assert human_message(item, "Lesson: fade the funding spike.") == (
        f"## Perpetual market context\n{context}\n\nLesson: fade the funding spike.\n\n{FORMAT}"
    )


def test_the_system_message_is_the_variants_prompt(store, variant_file, echo):
    assert cli.main(_replay(store, variant_file, "--repeats", "1")) == 0
    assert {system for system, _ in echo.calls} == {SYSTEM}


# -- the holdout lock --------------------------------------------------------------


def test_train_is_asked_by_default_and_nothing_else_is_read(store, variant_file, echo, capsys):
    root = payload_dir(store, RUN_ID)
    for slot in VALIDATION + HOLDOUT:
        # Unreadable if opened: the command succeeding is the proof it was not.
        (root / papers.payload_name(slot)).write_bytes(b"not the payload")
    assert cli.main(_replay(store, variant_file, "--repeats", "2")) == 0
    asked = sorted({human.splitlines()[1] for _, human in echo.calls})
    assert asked == [papers.context_text(slot) for slot in TRAIN]
    assert len(echo.calls) == len(TRAIN) * 2
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"replay: run {RUN_ID} (BTC, 4h cycle), train segment: {len(TRAIN)} question(s) x "
        "2 repeat(s)"
    )
    assert out[2] == (
        "gate: the run's genesis risk/decision blocks (leverage 1, max target margin 60%, "
        "deadband 4%, min_confidence 0.3, resize_min_confidence 0.62)"
    )
    assert "asked: 12 new answer(s); already stored, skipped: 0" in out


@pytest.mark.parametrize(
    "flags",
    [("--segment", "holdout"), ("--holdout",), ("--segment", "validation", "--holdout")],
    ids=["segment-alone", "flag-alone", "flag-on-validation"],
)
def test_the_holdout_is_asked_only_with_both_flags(store, variant_file, echo, capsys, flags):
    assert cli.main(_replay(store, variant_file, *flags)) == 1
    assert "asking the holdout spends its one look" in capsys.readouterr().err
    assert echo.calls == []


def test_a_dry_run_of_the_holdout_is_refused(store, variant_file, echo, capsys):
    """A dry run reads payloads and records nothing: on the holdout, an unrecorded look."""
    root = payload_dir(store, RUN_ID)
    for slot in HOLDOUT:
        (root / papers.payload_name(slot)).unlink()
    argv = _replay(store, variant_file, "--segment", "holdout", "--holdout", "--dry-run")
    assert cli.main(argv) == 1
    assert "--dry-run reads the payloads it checks; it does not run on the holdout" in (
        capsys.readouterr().err
    )
    assert not (store.parent / "replay.sqlite").exists()


def test_asking_the_holdout_writes_the_ledger_before_any_payload_is_read(
    store, variant_file, echo, capsys
):
    (payload_dir(store, RUN_ID) / papers.payload_name(HOLDOUT[0])).unlink()
    assert cli.main(_replay(store, variant_file, "--segment", "holdout", "--holdout")) == 1
    assert "cannot be read" in capsys.readouterr().err
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    try:
        looks = conn.execute("SELECT action, run_id, questions FROM ledger").fetchall()
    finally:
        conn.close()
    assert looks == [("ask", RUN_ID, len(HOLDOUT))]
    assert echo.calls == []


# -- resuming, retrying, refusing -----------------------------------------------------


def test_a_replay_resumes_and_never_asks_twice(store, variant_file, echo, capsys):
    total = len(TRAIN) * 3
    assert cli.main(_replay(store, variant_file, "--limit", "4")) == 0
    assert len(echo.calls) == 4
    assert "stopped at --limit; the same command continues from here" in (
        capsys.readouterr().out
    )
    assert cli.main(_replay(store, variant_file)) == 0
    assert len(echo.calls) == total
    assert f"asked: {total - 4} new answer(s); already stored, skipped: 4" in (
        capsys.readouterr().out
    )
    assert cli.main(_replay(store, variant_file)) == 0
    assert len(echo.calls) == total
    rows = _answers(store.parent / "replay.sqlite")
    assert [(r["input_id"], r["repeat"]) for r in rows] == [
        (papers.input_id(slot), repeat) for slot in TRAIN for repeat in range(3)
    ]


def test_a_stored_answer_keeps_the_text_and_the_gates_verdict(store, variant_file, echo):
    assert cli.main(_replay(store, variant_file, "--repeats", "1")) == 0
    rows = {r["input_id"]: r for r in _answers(store.parent / "replay.sqlite")}
    row = rows[papers.input_id(4)]
    assert row["raw_response"] == PAPERS[4].text
    assert (row["decision_mode"], row["target_side"], row["risk_action"]) == (
        "set_target",
        "short",
        "clamped",
    )
    assert (row["requested_target_margin_pct"], row["approved_target_margin_pct"]) == ("80", "60")
    assert (row["segment"], row["model_reported"], row["input_tokens"]) == ("train", "echo-1", 100)


def test_a_failed_call_is_retried_after_the_backoff(store, variant_file, echo, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cli, "_sleep", slept.append)
    echo.fail = 2
    assert cli.main(_replay(store, variant_file, "--repeats", "1", "--limit", "1")) == 0
    assert slept == list(BACKOFF_SECONDS)
    assert len(_answers(store.parent / "replay.sqlite")) == 1


def test_a_call_that_keeps_failing_ends_the_replay_by_name_and_keeps_the_answers(
    store, variant_file, echo, capsys
):
    assert cli.main(_replay(store, variant_file, "--repeats", "1", "--limit", "2")) == 0
    echo.fail = 3
    assert cli.main(_replay(store, variant_file, "--repeats", "1")) == 1
    err = capsys.readouterr().err
    assert (
        f"error: {papers.input_id(2)} repeat 0: the model call failed 3 time(s), last with "
        "ConnectionError: the provider hung up; 0 new answer(s) were stored before it"
    ) in err
    assert len(_answers(store.parent / "replay.sqlite")) == 2


def test_an_altered_payload_is_refused_before_any_call(store, variant_file, echo, capsys):
    path = payload_dir(store, RUN_ID) / papers.payload_name(TRAIN[-1])
    path.write_bytes(path.read_bytes().replace(b"question", b"Question"))
    assert cli.main(_replay(store, variant_file)) == 1
    err = capsys.readouterr().err
    assert f"error: {papers.input_id(TRAIN[-1])}: payload" in err
    assert "is not the one the input row recorded" in err
    assert echo.calls == []


def test_a_missing_payload_directory_is_named(store, variant_file, echo, capsys, tmp_path):
    nowhere = str(tmp_path / "nowhere")
    assert cli.main(_replay(store, variant_file, "--payload-root", nowhere)) == 1
    assert "no payload directory at" in capsys.readouterr().err


def test_payloads_copied_elsewhere_are_found_by_file_name(store, variant_file, echo, tmp_path):
    moved = tmp_path / "copied"
    payload_dir(store, RUN_ID).rename(moved)
    flags = ("--repeats", "1", "--payload-root", str(moved))
    assert cli.main(_replay(store, variant_file, *flags)) == 0
    assert len(echo.calls) == len(TRAIN)


def test_a_dry_run_checks_everything_and_builds_and_writes_nothing(
    store, variant_file, echo, capsys
):
    assert cli.main(_replay(store, variant_file, "--dry-run", "--limit", "5")) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        f"payloads read: {len(TRAIN)}, each checked against its input row's digest where one "
        "was recorded"
    ) in out
    assert (
        f"dry run: 0 answer(s) already stored, {len(TRAIN) * 3} to ask; this command would "
        "ask for 5 of them"
    ) in out
    assert echo.built == []
    assert not (store.parent / "replay.sqlite").exists()


def test_a_dry_run_counts_what_is_already_stored(store, variant_file, echo, capsys):
    assert cli.main(_replay(store, variant_file, "--limit", "4")) == 0
    capsys.readouterr()
    assert cli.main(_replay(store, variant_file, "--dry-run")) == 0
    pending = len(TRAIN) * 3 - 4
    assert (
        f"dry run: 4 answer(s) already stored, {pending} to ask; this command would "
        f"ask for {pending} of them"
    ) in capsys.readouterr().out.splitlines()


def test_a_dry_run_does_not_turn_an_empty_file_into_a_store(store, variant_file, echo, capsys):
    empty = store.parent / "replay.sqlite"
    empty.touch()
    assert cli.main(_replay(store, variant_file, "--dry-run")) == 1
    assert "holds no replay store (the file has no tables)" in capsys.readouterr().err
    assert empty.stat().st_size == 0


def test_answers_the_provider_said_nothing_about_are_counted(
    store, variant_file, monkeypatch, capsys
):
    def silent(system: str, human: str) -> Completion:
        return Completion(text="no json", usage_reported=False)

    monkeypatch.setattr(cli, "_build_model", lambda _variant: silent)
    assert cli.main(_replay(store, variant_file, "--repeats", "1", "--limit", "2")) == 0
    assert (
        "answers whose call the usage collector recorded nothing for: 2 (truncation unknown, "
        "read as not truncated, as the daemon reads it)"
    ) in capsys.readouterr().out.splitlines()


def test_the_replays_refusals_name_the_replay(tmp_path, variant_file, echo, capsys):
    config = {**run_config(interval="1h"), "risk": papers.RISK, "decision": papers.DECISION}
    path = write_paper_store(tmp_path / "hourly.db", payload_root=tmp_path / "p", config=config)
    argv = [
        "replay",
        "--db",
        str(path),
        "--run-id",
        FIXTURE_RUN,
        "--variant",
        str(variant_file),
        "--replay-db",
        str(tmp_path / "r.sqlite"),
    ]
    assert cli.main(argv) == 1
    assert "was traded on 1h candles; the replay reads runs on 4h / 1d candles" in (
        capsys.readouterr().err
    )


def test_a_changed_variant_under_its_old_name_is_refused(store, variant_file, echo, capsys):
    assert cli.main(_replay(store, variant_file, "--limit", "1")) == 0
    (variant_file.parent / "system.md").write_text(SYSTEM + " Carefully.", encoding="utf-8")
    assert cli.main(_replay(store, variant_file)) == 1
    assert "variant name 'echo' already stands for" in capsys.readouterr().err
    assert len(echo.calls) == 1


def test_a_client_that_cannot_be_built_is_named(store, variant_file, monkeypatch, capsys):
    def refuse(variant):
        raise ValueError("Unsupported LLM provider: nope")

    monkeypatch.setattr(cli, "_build_model", refuse)
    assert cli.main(_replay(store, variant_file)) == 1
    assert (
        "error: the openrouter/fixture/echo client could not be built: "
        "Unsupported LLM provider: nope"
    ) in capsys.readouterr().err


def test_a_run_without_a_recorded_gate_is_refused(tmp_path, variant_file, echo, capsys):
    path = write_paper_store(tmp_path / "old.db", payload_root=tmp_path / "p", config=run_config())
    argv = [
        "replay",
        "--db",
        str(path),
        "--run-id",
        FIXTURE_RUN,
        "--variant",
        str(variant_file),
        "--replay-db",
        str(tmp_path / "r.sqlite"),
    ]
    assert cli.main(argv) == 1
    assert (
        f"error: run {FIXTURE_RUN!r}: the genesis config lacks risk, decision, so the gate its "
        "answers met is unknown"
    ) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--repeats", "0", "--repeats must be at least 1, got 0"),
        ("--limit", "0", "--limit must be at least 1, got 0"),
        ("--replay-db", "", "--replay-db needs a path, got ''"),
        ("--payload-root", "", "--payload-root needs a directory, got ''"),
    ],
)
def test_bad_numbers_and_empty_paths_are_refused_up_front(
    store, variant_file, echo, capsys, flag, value, message
):
    assert cli.main([*_replay(store, variant_file), flag, value]) == 1
    assert f"error: {message}" in capsys.readouterr().err
    assert echo.calls == []


# -- the gate's account inputs -------------------------------------------------------


def _facts(**overrides) -> InputFacts:
    base = {
        "input_id": "in-x",
        "payload_name": "x.json",
        "payload_hash": None,
        "mark": Decimal("100"),
        "account_equity": Decimal("1000"),
        "side": TargetSide.LONG,
        "size": Decimal("3"),
        "margin_pct": Decimal("30"),
        "leverage": Decimal("1"),
    }
    base.update(overrides)
    return InputFacts(**base)


def test_the_position_is_rebuilt_from_the_row():
    equity, current = position_state(_facts(), RiskConfig())
    assert equity == Decimal("1000")
    assert (current.side, current.signed_notional, current.margin_pct, current.leverage) == (
        TargetSide.LONG,
        Decimal("300"),
        Decimal("30"),
        Decimal("1"),
    )
    flat_row = _facts(side=TargetSide.FLAT, size=Decimal("0"), margin_pct=None)
    assert position_state(flat_row, RiskConfig())[1].side is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"account_equity": None}, "in-x: account_equity is NULL"),
        (
            {"leverage": Decimal("2")},
            "in-x: configured_leverage 2 is not the genesis risk.leverage 1",
        ),
        ({"side": TargetSide.FLAT, "size": Decimal("1")}, "in-x: a flat position with size 1"),
        ({"size": None}, "in-x: a long position with no recorded size"),
        (
            {"size": Decimal("-3")},
            "in-x: the recorded position cannot be put back through the gate",
        ),
        ({"margin_pct": None}, "in-x: a long position with no recorded margin"),
    ],
    ids=[
        "no-equity",
        "foreign-leverage",
        "sized-flat",
        "unsized-long",
        "long-with-short-size",
        "unmargined-long",
    ],
)
def test_a_row_the_gate_cannot_take_is_refused_by_name(overrides, message):
    with pytest.raises(ReplayError, match=message):
        position_state(_facts(**overrides), RiskConfig())


def test_gate_config_refuses_a_run_without_a_genesis(tmp_path):
    path = write_paper_store(tmp_path / "bare.db", payload_root=tmp_path / "p", config=None)
    with (
        Database(path, migrate=False) as db,
        pytest.raises(ScoreError, match="recorded no genesis config"),
    ):
        gate_config(db, FIXTURE_RUN)
