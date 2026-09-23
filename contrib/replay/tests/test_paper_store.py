"""The store reader: the fixture table, written by the daemon's repository, read back as records."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.integration import decision_reports
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.replay.paper_store import (
    REPORTS_SUFFIX,
    load_decisions,
    load_research_closes,
    run_facts,
)
from contrib.replay.score import ScoreError
from contrib.replay.upstream import (
    CostModel,
    Database,
    FillRole,
    ResearchStore,
    TargetSide,
    from_epoch_ms,
)

from .conftest import (
    ATTEMPTS_WITHOUT_INPUT,
    COIN,
    IN_PROGRESS_SLOT,
    RESEARCH_CLOSES,
    RETRY_INPUTS,
    RUN_ID,
    STEP_MS,
    at_ms,
    fixture_answers,
    fixture_questions,
    input_id,
    payload_name,
    retry_input_id,
    run_config,
    write_paper_store,
    write_research_store,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_paper_store(
        tmp_path / "paper_trading.db", payload_root=tmp_path / "payloads", config=run_config()
    )


def _decision(margin: int, confidence: float) -> str:
    """A set_target long the engine could have emitted, fenced the way it emits it."""
    payload = {
        "decision_mode": "set_target",
        "target_side": "long",
        "requested_target_margin_pct": margin,
        "confidence": confidence,
        "rationale": "Trend and funding support a long.",
        "key_risks": ["Funding is rising"],
    }
    return f"Final decision:\n\n```json\n{json.dumps(payload)}\n```\n"


def test_the_store_reads_back_the_fixture_table(store):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
    assert decisions.questions == fixture_questions()
    assert decisions.answers == fixture_answers()
    assert decisions.without_input == ATTEMPTS_WITHOUT_INPUT


def test_a_question_is_the_attempts_final_input_not_every_input_row(store):
    """The earlier tries of a retried cycle are in ``ai_inputs`` and are not questions."""
    with Database(store, migrate=False) as db:
        stored = {
            row[0] for row in db.conn.execute("SELECT input_id FROM ai_inputs WHERE run_id = ?", (RUN_ID,))
        }
        decisions = load_decisions(db, RUN_ID)
    asked = {q.input_id for q in decisions.questions}
    retries = {retry_input_id(slot) for slot in RETRY_INPUTS}
    assert retries and retries <= stored
    assert asked == stored - retries - {input_id(IN_PROGRESS_SLOT)}


def test_an_api_failed_attempt_is_an_unanswered_question(store):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
    answered = {a.input_id for a in decisions.answers}
    assert input_id(11) in {q.input_id for q in decisions.questions}
    assert input_id(11) not in answered


def test_an_attempt_still_in_progress_is_counted_and_left_out(store):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
    assert decisions.in_progress == 1
    assert input_id(IN_PROGRESS_SLOT) not in {q.input_id for q in decisions.questions}
    assert (decisions.retried, decisions.extra_tries) == (1, 1)


def test_an_attempt_naming_an_input_the_store_lacks_is_refused(store):
    stamp = from_epoch_ms(at_ms(14))
    with Database(store) as db, db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="att-dangling",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            scheduled_at=stamp,
            input_id="in-missing",
            status="completed",
        )
    with Database(store, migrate=False) as db, pytest.raises(ScoreError, match="in-missing"):
        load_decisions(db, RUN_ID)


def test_a_sized_position_with_no_margin_is_refused_by_name(store):
    stamp = from_epoch_ms(at_ms(14))
    with Database(store) as db, db.transaction() as conn:
        repo.insert_ai_input(
            conn,
            input_id="in-ruin",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            symbol=COIN,
            candle_end=stamp,
            mark_price=Decimal("1"),
            current_position_side="long",
            current_margin_pct=None,
            configured_leverage=Decimal("1"),
            max_target_margin_pct=Decimal("60"),
        )
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="att-ruin",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            scheduled_at=stamp,
            input_id="in-ruin",
            status="api_failed",
        )
    with (
        Database(store, migrate=False) as db,
        pytest.raises(ScoreError, match="in-ruin: current_margin_pct is NULL"),
    ):
        load_decisions(db, RUN_ID)


@pytest.mark.parametrize(
    ("raw", "cap", "expect"),
    [
        (_decision(35, 0.78), 60, ("approved", True, 35.0, "set_target")),
        (_decision(35, 0.78), 20, ("clamped", True, 20.0, "set_target")),
        (_decision(35, 0.2), 60, ("rejected", True, None, "maintain_current")),
        ("no json here", 60, ("invalid_fail_closed", False, None, "maintain_current")),
    ],
    ids=["approved", "clamped", "rejected", "fail-closed"],
)
def test_an_answer_written_by_the_real_gate_reads_back_as_the_call_it_made(tmp_path, raw, cap, expect):
    """Round trip: parse -> gate -> ``write_ai_output`` -> ``load_decisions`` -> ``Answer``.

    The one test that ties the fixture's hand-written shapes to the gate's
    real ones, so the two cannot drift apart again.
    """
    from contrib.hyperliquid_perp.domains.perp.risk_gate import (
        CurrentPositionState,
        RiskConfig,
        evaluate,
    )
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        DecisionConfig,
        parse_target_decision,
    )
    from contrib.hyperliquid_perp.persistence.audit_rows import write_ai_output

    path = write_paper_store(tmp_path / "gate.db", payload_root=tmp_path / "p", config=run_config(), rows=())
    stamp = from_epoch_ms(at_ms(0))
    parsed = parse_target_decision(raw, DecisionConfig())
    gate = evaluate(
        parsed,
        account_equity=Decimal("1000"),
        current=CurrentPositionState.flat(),
        risk=RiskConfig(max_target_margin_pct=cap),
        decision_cfg=DecisionConfig(),
    )
    with Database(path) as db, db.transaction() as conn:
        repo.insert_ai_input(
            conn,
            input_id="in-gate",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            symbol=COIN,
            candle_end=stamp,
            mark_price=Decimal("100"),
            current_position_side="flat",
            configured_leverage=Decimal("1"),
            max_target_margin_pct=Decimal(cap),
        )
        write_ai_output(
            conn,
            now=stamp,
            output_id="out-gate",
            input_id="in-gate",
            decision_attempt_id="att-gate",
            mode="paper",
            run_id=RUN_ID,
            symbol=COIN,
            gate=gate,
            parsed=parsed,
            mark_price=Decimal("100"),
            account_equity=Decimal("1000"),
        )
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="att-gate",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            scheduled_at=stamp,
            input_id="in-gate",
            output_id="out-gate",
            status="completed",
        )
    with Database(path, migrate=False) as db:
        (answer,) = load_decisions(db, RUN_ID).answers
    risk_action, asked, approved, mode = expect
    assert answer.risk_action.value == risk_action
    assert answer.decision_mode.value == mode
    assert answer.asked_target is asked
    assert answer.approved_margin_pct == approved
    if asked:
        assert (answer.target_side, answer.requested_margin_pct) == (TargetSide.LONG, 35.0)


def test_run_facts_take_the_fill_model_from_the_run_config(store, tmp_path):
    with Database(store, migrate=False) as db:
        facts = run_facts(db, RUN_ID)
    assert facts is not None
    assert (facts.run_id, facts.mode, facts.coin, facts.interval, facts.step_ms) == (
        RUN_ID,
        "paper",
        COIN,
        "4h",
        STEP_MS,
    )
    assert facts.config_recorded
    assert facts.costs == CostModel(
        taker_fee_rate=0.00045,
        maker_fee_rate=0.00015,
        slippage_bps=5.0,
        fill_role=FillRole.TAKER,
        leverage=1.0,
    )
    maker = write_paper_store(
        tmp_path / "maker.db", payload_root=tmp_path / "p", config=run_config(style="maker")
    )
    with Database(maker, migrate=False) as db:
        facts = run_facts(db, RUN_ID)
    assert facts is not None and facts.costs.fill_role is FillRole.MAKER


def test_run_facts_fall_back_to_the_config_defaults_without_a_genesis_config(tmp_path):
    path = write_paper_store(tmp_path / "old.db", payload_root=tmp_path / "p", config=None)
    with Database(path, migrate=False) as db:
        facts = run_facts(db, RUN_ID)
        assert run_facts(db, "no-such-run") is None
    assert facts is not None
    assert not facts.config_recorded
    assert facts.coin == COIN  # from the first ai_inputs row's symbol
    assert facts.costs == CostModel(
        taker_fee_rate=0.00045,
        maker_fee_rate=0.00015,
        slippage_bps=5.0,
        fill_role=FillRole.TAKER,
        leverage=1.0,
    )


def test_a_corrupt_genesis_config_is_refused_by_name(tmp_path):
    path = tmp_path / "corrupt.db"
    with Database(path) as db, db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r",
            mode="paper",
            initial_balance_usdc=Decimal("1"),
            schema_version=1,
            config_json="{not json",
        )
    with Database(path, migrate=False) as db, pytest.raises(ScoreError, match="config_json"):
        run_facts(db, "r")


def test_reports_are_counted_by_payload_name_under_the_root(store, tmp_path):
    root = tmp_path / "payloads"
    root.mkdir()
    for slot in (0, 7):
        (root / payload_name(slot)).with_suffix(REPORTS_SUFFIX).write_text("{}", encoding="utf-8")
    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID, reports_root=root).questions
    present = {q.input_id for q in questions if q.reports_present}
    assert present == {input_id(0), input_id(7)}
    assert all(q.reports_present is False for q in questions if q.input_id not in present)


def test_the_reports_suffix_is_the_one_the_engine_writes():
    source = Path(decision_reports.__file__).read_text(encoding="utf-8")
    assert f'suffix="{REPORTS_SUFFIX}"' in source


def test_research_closes_are_keyed_by_close_time(tmp_path):
    path = write_research_store(tmp_path / "autoresearch.sqlite")
    with ResearchStore(path) as research:
        closes = load_research_closes(research, coin=COIN, interval="4h")
    assert dict(closes) == RESEARCH_CLOSES


def test_a_question_is_placed_at_its_decision_instant_not_its_closed_bar(store):
    """A cycle that ran at 15:53 against the bar closed at 12:00 sits at 15:53."""
    decided, closed = from_epoch_ms(at_ms(15)), from_epoch_ms(at_ms(14))
    with Database(store) as db, db.transaction() as conn:
        repo.insert_ai_input(
            conn,
            input_id="in-late",
            timestamp=decided,
            mode="paper",
            run_id=RUN_ID,
            symbol=COIN,
            candle_end=closed,
            mark_price=Decimal("1"),
            current_position_side="flat",
            configured_leverage=Decimal("1"),
            max_target_margin_pct=Decimal("60"),
        )
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="att-late",
            timestamp=decided,
            mode="paper",
            run_id=RUN_ID,
            scheduled_at=decided,
            input_id="in-late",
            status="api_failed",
        )
    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID).questions
    assert next(q for q in questions if q.input_id == "in-late").at_ms == at_ms(15)


def test_an_attempt_naming_an_output_the_store_lacks_is_refused(store):
    stamp = from_epoch_ms(at_ms(15))
    with Database(store) as db, db.transaction() as conn:
        repo.insert_ai_input(
            conn,
            input_id="in-half",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            symbol=COIN,
            candle_end=stamp,
            mark_price=Decimal("1"),
            current_position_side="flat",
            configured_leverage=Decimal("1"),
            max_target_margin_pct=Decimal("60"),
        )
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="att-half",
            timestamp=stamp,
            mode="paper",
            run_id=RUN_ID,
            scheduled_at=stamp,
            input_id="in-half",
            output_id="out-missing",
            status="completed",
        )
    with Database(store, migrate=False) as db, pytest.raises(ScoreError, match="out-missing"):
        load_decisions(db, RUN_ID)


def test_the_genesis_config_the_daemon_records_parses_here(store):
    """The config subset is what ``cli/_drift._run_config_subset`` writes: whole blocks."""
    with Database(store, migrate=False) as db:
        stored = json.loads(db.conn.execute("SELECT config_json FROM runs").fetchone()[0])
    assert set(stored) == {"coin", "market_data", "paper_trading"}
