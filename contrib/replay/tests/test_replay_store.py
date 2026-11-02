"""``replay.sqlite``: what it refuses to open, the one-name-one-variant rule, the round trip."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.replay.account import Path as AccountPath
from contrib.replay.replay_store import (
    SCHEMA_VERSION,
    BacktestRecord,
    ReplayStore,
    ReplayStoreError,
    StoredAnswer,
    StoredBook,
)
from contrib.replay.upstream import (
    CurrentPositionState,
    DecisionConfig,
    GuardrailSide,
    RiskConfig,
    evaluate,
    parse_target_decision,
)

from .conftest import downgrade_to_v3, downgrade_to_v4, terms
from .papers import decision_text, graph_variant, variant as make_variant

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
    with (
        ReplayStore(path) as store,
        pytest.raises(ReplayStoreError, match="the row was changed after it was stored"),
    ):
        store.variant("v")


class _CommitFailsOnce:
    """The store's connection, except that the first COMMIT raises: a SQLITE_BUSY at commit."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.failed = False

    def execute(self, sql: str, *args):
        if sql == "COMMIT" and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_a_failed_commit_is_rolled_back_so_the_next_write_can_begin(tmp_path):
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        real = store.conn
        store.conn = _CommitFailsOnce(real)  # type: ignore[assignment]
        with pytest.raises(ReplayStoreError, match="write failed at commit: database is locked"):
            store.register(make_variant(), now=NOW)
        store.conn = real
        # Without the rollback the first transaction would still be open,
        # and this BEGIN would fail inside it.
        assert store.register(make_variant(), now=NOW) == []
        assert store.variant("v").name == "v"


def test_a_split_is_pinned_once_and_read_back(tmp_path):
    from contrib.replay.upstream import Split

    step = 14_400_000
    start = 1_800_000_000_000 - 1_800_000_000_000 % step
    first = Split.by_shares("4h", start_ms=start, end_ms=start + 10 * step)
    other = Split.by_shares("4h", start_ms=start, end_ms=start + 15 * step)
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        assert store.pinned_split("run") is None
        pinned, at = store.pin_split("run", first, now=NOW)
        assert (pinned, at) == (first, NOW.isoformat())
        again, _ = store.pin_split("run", other, now=NOW)
        assert again == first  # the first pin stands


def test_a_failure_is_recorded_listed_and_cleared(tmp_path):
    variant = make_variant()
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.record_failure(
            variant.sha, run_id="run", input_id="in-1", repeat=0, error="400 too long", now=NOW
        )
        assert store.failed(variant.sha, "run") == {0: {"in-1"}}
        assert store.failed(variant.sha, "other") == {}
        assert store.clear_failures(variant.sha, "run") == 1
        assert store.failed(variant.sha, "run") == {}


def test_a_look_at_the_paper_traders_answers_names_no_variant(tmp_path):
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.record_look(
            action="score", run_id="run", variant_sha=None, questions=2, who="joy", now=NOW
        )
        (look,) = store.looks("run")
    assert (look.action, look.variant_name, look.questions) == ("score", "paper", 2)


def test_an_unreadable_pin_is_refused_by_name(tmp_path):
    path = tmp_path / "r.sqlite"
    with ReplayStore(path, create=True):
        pass
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO splits VALUES ('run', '{not json', 't')")
    conn.commit()
    conn.close()
    with (
        ReplayStore(path) as store,
        pytest.raises(ReplayStoreError, match="the split pinned for run 'run' cannot be read"),
    ):
        store.pinned_split("run")


def test_a_v3_store_is_read_as_written_and_takes_no_backtest(tmp_path):
    path = tmp_path / "r.sqlite"
    with ReplayStore(path, create=True) as store:
        store.register(make_variant(), now=NOW)
    downgrade_to_v3(path)
    record = BacktestRecord(NOW, "run", "flat", None, None, False)
    with ReplayStore(path) as store:
        assert store.probe_tables and not store.backtest_tables
        assert store.backtests("run") == []
        with pytest.raises(ReplayStoreError, match="before the backtest tables"):
            store.write_backtest(record, AccountPath(terms(), ()))
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
    finally:
        conn.close()
    with ReplayStore(path, create=True) as store:
        assert store.backtest_tables
        store.write_backtest(record, AccountPath(terms(), ()))
        (walked,) = store.backtests("run")
        assert (walked.driver, walked.variant_name, walked.final_equity) == (
            "flat",
            None,
            Decimal(1000),
        )
    with pytest.raises(ValueError, match="names a variant with its repeat, or neither"):
        BacktestRecord(NOW, "run", "variant", "sha256:x", None, False)


def _walked_path():
    """A two-step maker path with a fill and a resting post, built through the account itself."""
    from contrib.replay.account import Bar, Decision, Series, Target, simulate
    from contrib.replay.upstream import CostModel, FillRole, TargetSide

    from .conftest import STEP_MS, at_ms

    maker = terms(CostModel(fill_role=FillRole.MAKER))
    bars = [
        Bar(at_ms(0) - STEP_MS + 1, at_ms(0), Decimal(103), Decimal(97), Decimal(100)),
        Bar(at_ms(1) - STEP_MS + 1, at_ms(1), Decimal(101), Decimal(99), Decimal(100)),
    ]
    decisions = [
        Decision(
            "q0", at_ms(0), None, Decimal(100), Decimal(1), Target(TargetSide.LONG, Decimal(50))
        ),
        Decision(
            "q1", at_ms(1), None, Decimal(100), Decimal(1), Target(TargetSide.FLAT, Decimal(0))
        ),
    ]
    return simulate(decisions, terms=maker, series=Series.of(bars, []))


def test_a_walked_path_reads_back_column_for_column(tmp_path):
    path = _walked_path()
    filled, resting = path.steps
    assert filled.filled is True and resting.filled is False  # the fixture has both shapes
    record = BacktestRecord(NOW, "run", "paper", None, None, False)
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        backtest_id = store.write_backtest(record, path)
        row = store.conn.execute(
            "SELECT * FROM backtests WHERE backtest_id = ?", (backtest_id,)
        ).fetchone()
        steps = store.conn.execute(
            "SELECT * FROM backtest_steps WHERE backtest_id = ? ORDER BY at", (backtest_id,)
        ).fetchall()
    assert (row["driver"], row["tick"], row["half_spread_bps"], row["initial_balance"]) == (
        "paper",
        "1",
        "0.5",
        "1000",
    )
    # Four hours held with no settlement in the series: four counted missing.
    assert (row["final_equity"], row["ruined"], row["funding_missing"]) == (
        str(path.final_equity),
        0,
        4,
    )
    assert path.funding_missing == 4
    assert json.loads(row["costs_json"])["fill_role"] == "maker"
    for stored, step in zip(steps, path.steps, strict=True):
        values = step.values()
        assert stored["input_id"] == values[0]
        assert stored["at"] == values[1]
        assert stored["target_side"] == values[4]
        assert stored["target_margin_pct"] == str(values[5])
        assert (stored["carried"], stored["filled"]) == (int(values[6]), int(values[7]))
        assert stored["fill_price"] == str(values[8])
        assert (stored["fee"], stored["funding"]) == (str(values[9]), str(values[10]))
        assert (stored["size"], stored["entry"]) == (
            str(values[12]),
            None if values[13] is None else str(values[13]),
        )
        assert (stored["wallet"], stored["equity"], stored["ret"]) == (
            str(values[14]),
            str(values[15]),
            values[16],
        )


def test_write_backtests_is_one_transaction(tmp_path):
    path = _walked_path()
    good = BacktestRecord(NOW, "run", "flat", None, None, False)
    unknown = BacktestRecord(NOW, "run", "variant", "sha256:nobody", 0, False)
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        with pytest.raises(ReplayStoreError, match="write failed, rolled back"):
            store.write_backtests([(good, path), (unknown, path)])
        assert store.backtests("run") == []


def test_trials_counts_each_variant_repeat_once_failures_included(tmp_path):
    variant = make_variant()
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        assert store.trials() == 0
        store.register(variant, now=NOW)
        gate = _gate(decision_text("set_target", "long", 30, 0.8))
        store.write_answer(
            variant.sha,
            StoredAnswer("run", "in-1", 0, NOW, "train", "text", False, None, gate),
        )
        store.record_failure(
            variant.sha, run_id="run", input_id="in-2", repeat=0, error="400", now=NOW
        )
        assert store.trials() == 1  # answers and failures of one repeat are one trial
        store.record_failure(
            variant.sha, run_id="other", input_id="in-1", repeat=1, error="400", now=NOW
        )
        assert store.trials() == 2  # a repeat that only failed still counts


def test_a_half_formed_later_table_set_is_refused_untouched(tmp_path):
    path = tmp_path / "r.sqlite"
    with ReplayStore(path, create=True):
        pass
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE backtest_steps")
    conn.execute("DROP TABLE backtests")
    conn.execute("DROP TABLE probe_answers")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    # Not an older store to bring forward: refused by its version, untouched.
    with pytest.raises(ReplayStoreError, match="was written at replay schema v2"):
        ReplayStore(path, create=True)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "backtests" not in tables
    finally:
        conn.close()


def test_a_backtest_record_couples_its_driver_to_its_variant():
    with pytest.raises(ValueError, match="names its variant and a control none"):
        BacktestRecord(NOW, "run", "paper", "sha256:x", 0, False)
    with pytest.raises(ValueError, match="names its variant and a control none"):
        BacktestRecord(NOW, "run", "variant", None, None, False)


def test_a_store_from_the_earlier_schema_is_named_not_taken_for_a_foreign_file(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for table in ("variants", "answers", "ledger"):
        conn.execute(f"CREATE TABLE {table} (x)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(ReplayStoreError, match="was written at replay schema v1, before"):
        ReplayStore(path)


# -- graph variants and books (plan PR 3b) -----------------------------------------


def _book(**overrides) -> StoredBook:
    fields = {
        "mark": Decimal("100"),
        "size": Decimal("3"),
        "entry": Decimal("99"),
        "wallet": Decimal("999.95545"),
        "equity": Decimal("1002.95545"),
        "last_fill_ms": 1_800_000_000_000,
        "guardrail_side": GuardrailSide.LONG,
        "graph_input_tokens": 500,
        "graph_output_tokens": 50,
    }
    fields.update(overrides)
    return StoredBook(**fields)


def test_a_graph_variant_registers_with_its_quick_model_and_guardrail_and_reads_back(tmp_path):
    variant = graph_variant(quick_provider="openrouter", quick_model="q/q", guardrail="rule")
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        assert store.register(variant, now=NOW) == []
        assert store.variant("g") == variant
        # The same file under another name is the same sha: refused as for any variant.
        same = graph_variant(
            name="h", quick_provider="openrouter", quick_model="q/q", guardrail="rule"
        )
        with pytest.raises(ReplayStoreError, match="already stored as 'g'"):
            store.register(same, now=NOW)
        # A simple variant beside it reads back simple.
        store.register(make_variant(), now=NOW)
        assert store.variant("v").mode == "simple"


def test_a_graph_answer_and_its_book_are_written_together_and_read_back_exactly(tmp_path):
    variant = graph_variant()
    text = decision_text("set_target", "long", 30, 0.8)
    answer = StoredAnswer(
        run_id="run",
        input_id="q1",
        repeat=0,
        asked_at=NOW,
        segment="train",
        raw_response=text,
        truncated=False,
        invalid_reason=None,
        gate=_gate(text),
        model_reported="m",
        input_tokens=100,
        output_tokens=10,
    )
    flat_book = _book(
        size=Decimal(0),
        entry=None,
        last_fill_ms=None,
        guardrail_side=None,
        graph_input_tokens=None,
        graph_output_tokens=None,
    )
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.write_graph_answer(variant.sha, answer, _book())
        store.record_graph_failure(
            variant.sha, run_id="run", input_id="q2", repeat=0, error="422", now=NOW, book=flat_book
        )
        books = store.books(variant.sha, "run")[0]
        held = books["q1"]
        assert (held.size, held.entry, held.wallet, held.equity) == (
            Decimal("3"),
            Decimal("99"),
            Decimal("999.95545"),
            Decimal("1002.95545"),
        )
        assert held.last_fill_ms == 1_800_000_000_000
        assert held.guardrail_side is GuardrailSide.LONG
        assert (held.graph_input_tokens, held.graph_output_tokens) == (500, 50)
        flat = books["q2"]
        assert flat.size == 0 and flat.entry is None and flat.guardrail_side is None
        assert flat.graph_input_tokens is None
        assert [a.input_id for a in store.answers(variant.sha, "run")[0]] == ["q1"]
        assert store.failed(variant.sha, "run") == {0: {"q2"}}
        assert store.trials() == 1
        # The same question and repeat cannot be stored twice; the book goes with the answer.
        with pytest.raises(ReplayStoreError, match="write failed"):
            store.write_graph_answer(variant.sha, answer, _book())
        assert len(store.books(variant.sha, "run")[0]) == 2


def test_a_v4_store_is_read_as_written_and_takes_no_graph_variant(tmp_path):
    path = tmp_path / "r.sqlite"
    with ReplayStore(path, create=True) as store:
        store.register(make_variant(), now=NOW)
    downgrade_to_v4(path)
    with ReplayStore(path) as store:
        assert store.backtest_tables and not store.graph_tables
        assert store.variant("v") == make_variant()
        assert store.books(make_variant().sha, "run") == {}
        with pytest.raises(ReplayStoreError, match="before the graph tables"):
            store.register(graph_variant(), now=NOW)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (4,)
    finally:
        conn.close()
    with ReplayStore(path, create=True) as store:
        assert store.graph_tables
        store.register(graph_variant(), now=NOW)
        assert store.variant("g").mode == "graph"


def test_clearing_failures_takes_the_refused_questions_books_with_them(tmp_path):
    variant = graph_variant()
    text = decision_text("set_target", "long", 30, 0.8)
    answer = StoredAnswer("run", "q1", 0, NOW, "train", text, False, None, _gate(text), "m", 1, 1)
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        store.write_graph_answer(variant.sha, answer, _book())
        store.record_graph_failure(
            variant.sha, run_id="run", input_id="q2", repeat=0, error="422", now=NOW, book=_book()
        )
        assert store.clear_failures(variant.sha, "run") == 1
        assert set(store.books(variant.sha, "run")[0]) == {"q1"}
    with pytest.raises(ValueError, match="in and out together, or not at all"):
        _book(graph_input_tokens=1, graph_output_tokens=None)
    assert _book().identity == _book(graph_input_tokens=9, graph_output_tokens=9).identity
    assert _book().describe().startswith("mark 100, size 3, entry 99, wallet 999.95545, equity")


def test_an_answer_and_its_book_are_one_write_or_none(tmp_path):
    variant = graph_variant()
    text = decision_text("set_target", "long", 30, 0.8)
    answer = StoredAnswer("run", "q1", 0, NOW, "train", text, False, None, _gate(text), "m", 1, 1)
    with ReplayStore(tmp_path / "r.sqlite", create=True) as store:
        store.register(variant, now=NOW)
        # A book already stands for q1 (a refusal), so the book insert fails
        # after the answer insert succeeded: the answer must not stay behind.
        store.record_graph_failure(
            variant.sha, run_id="run", input_id="q1", repeat=0, error="422", now=NOW, book=_book()
        )
        with pytest.raises(ReplayStoreError, match="write failed"):
            store.write_graph_answer(variant.sha, answer, _book())
        assert store.answers(variant.sha, "run") == {}
