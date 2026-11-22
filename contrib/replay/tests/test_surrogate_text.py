"""Text SQLite cannot take (issue #428): stored with U+FFFD, where it used to end a replay.

A model's reply, the sentence that says why one was not read, a provider's
refusal or the model name it reported can hold a lone surrogate. The bind
raised from inside the write: the question had been paid for, its row was
not stored, and a resume asked it again and stopped at the same place.

Three commands write such text — ``replay``, ``replay --probe`` and
``backtest --ask`` — and each is run here through ``cli.main`` with the
suite's own fakes. A decision's reply was already made storable by the parse
seam this package borrows (issue #427); those cases pass without this
package's own replacement and are pinned here so that the borrow is not the
only thing holding them.

Every such character is built with ``chr``: a source file cannot hold one,
and an escape for one is easy to get wrong without noticing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.replay import cli, replay_store
from contrib.replay.replay_store import ReplayStore
from contrib.replay.tests import papers
from contrib.replay.tests.test_backtest_cli import GATE_MARKS, write_research
from contrib.replay.tests.test_probe import H4, Forecaster, forecast_text, write_probe

HIGH = chr(0xD83D)  # the first half of a UTF-16 pair, on its own
FFFD = chr(0xFFFD)
ESCAPED = chr(0x5C) + "ud83d"  # the six ASCII characters JSON writes for it
TARGET = 1  # the fixture question whose answer is the odd one
TARGET_ID = "gate-in-01"

_SELECT = {
    "answers": "SELECT * FROM answers ORDER BY input_id, repeat",
    "failures": "SELECT * FROM failures ORDER BY input_id, repeat",
    "probe_answers": "SELECT * FROM probe_answers ORDER BY input_id, repeat",
}


def test_the_characters_these_tests_are_about_are_what_they_are_meant_to_be():
    # No product code: a test about a lone surrogate that held something
    # else would pass for the wrong reason.
    assert len(HIGH) == 1 and 0xD800 <= ord(HIGH) <= 0xDFFF
    with pytest.raises(UnicodeEncodeError):
        HIGH.encode("utf-8")
    assert ESCAPED.isascii() and len(ESCAPED) == 6 and json.loads(f'"{ESCAPED}"') == HIGH


# -- the fixture: one directory a command runs in -------------------------------------------------


def _work(directory: Path) -> Path:
    directory.mkdir()
    papers.write_gate_store(directory / "paper_trading.db", reports=True)
    papers.write_variant(directory, "echo")
    papers.write_graph_variant(directory, "g")
    write_probe(directory)
    write_research(directory / "autoresearch.sqlite", GATE_MARKS, slots=12)
    with ReplayStore(directory / "replay.sqlite", create=True):
        pass
    return directory


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
    return _work(tmp_path / "odd")


@pytest.fixture
def twin(tmp_path, work) -> Path:
    """A second directory like ``work``, for the same run told in words SQLite always took."""
    return _work(tmp_path / "twin")


def _argv(work: Path, command: str) -> list[str]:
    store = work / "paper_trading.db"
    if command == "replay":
        return papers.replay_argv(store, work / "echo.yaml", "--repeats", "1")
    if command == "probe":
        probe = str(work / "direction-t.yaml")
        return papers.replay_argv(store, work / "echo.yaml", "--probe", probe, "--repeats", "1")
    return [
        "backtest",
        "--db",
        str(store),
        "--run-id",
        papers.RUN_ID,
        "--research-db",
        str(work / "autoresearch.sqlite"),
        "--replay-db",
        str(work / "replay.sqlite"),
        "--ask",
        str(work / "g.yaml"),
        "--repeats",
        "1",
    ]


def _rows(work: Path, table: str) -> list[dict]:
    conn = sqlite3.connect(work / "replay.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(_SELECT[table])]
    finally:
        conn.close()


def _target(work: Path, table: str) -> dict:
    (row,) = (row for row in _rows(work, table) if row["input_id"] == TARGET_ID)
    return row


# -- the fakes ----------------------------------------------------------------------------------


class _ProviderError(Exception):
    """A provider SDK's error as the suite fakes one: a status on the exception."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


def _is_target(text: str) -> bool:
    return papers.question_line(TARGET) in text


class _Echo(papers.Echo):
    """The echo; on the target question it refuses with ``refusal``, or reports ``model``."""

    def __init__(self, *, refusal: str | None = None, model: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refusal = refusal
        self.model = model

    def __call__(self, system, human):
        if _is_target(human) and self.refusal is not None:
            self.calls.append((system, human))
            raise _ProviderError(400, self.refusal)
        completion = super().__call__(system, human)
        if _is_target(human) and self.model is not None:
            return replace(completion, model=self.model)
        return completion


class _Forecaster(Forecaster):
    """The forecaster; on the target question it refuses with ``refusal``, or reports ``model``."""

    def __init__(self, *, refusal: str | None = None, model: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refusal = refusal
        self.model = model

    def __call__(self, system, human):
        if _is_target(human) and self.refusal is not None:
            self.calls.append((system, human))
            raise _ProviderError(400, self.refusal)
        completion = super().__call__(system, human)
        if _is_target(human) and self.model is not None:
            return replace(completion, model=self.model)
        return completion


class _Graph(papers.EchoGraph):
    """The echo graph; on the target question it refuses with ``refusal``."""

    refusal: str | None = None

    def ask(self, **kwargs):
        if _is_target(kwargs["context_text"]) and self.refusal is not None:
            self.asked.append(kwargs)
            raise _ProviderError(422, self.refusal)
        return super().ask(**kwargs)


def _model(monkeypatch, model) -> None:
    monkeypatch.setattr(cli, "_build_model", lambda _variant: model)


def _graph(monkeypatch, *, text_for=None, refusal=None) -> list[_Graph]:
    built: list[_Graph] = []

    def build(variant):
        graph = _Graph(variant, text_for=text_for)
        graph.refusal = refusal
        built.append(graph)
        return graph

    monkeypatch.setattr(cli, "_build_graph", build)
    return built


def _decision(*, ensure_ascii: bool) -> str:
    block = {
        "decision_mode": "set_target",
        "target_side": "long",
        "requested_target_margin_pct": 30,
        "confidence": 0.8,
        "rationale": f"Momentum {HIGH} holds.",
        "key_risks": [f"A reversal {HIGH}."],
    }
    return "Final decision:\n\n```json\n" + json.dumps(block, ensure_ascii=ensure_ascii) + "\n```\n"


# -- the store's own seam -----------------------------------------------------------------------


def test_the_store_replaces_one_for_one_and_hands_clean_text_back_as_it_is():
    assert replay_store._storable(f"a{HIGH}b{HIGH}") == f"a{FFFD}b{FFFD}"
    clean = f"plain, {chr(0x1F600)}, and {FFFD} a model wrote itself"
    assert replay_store._storable(clean) is clean
    assert replay_store._storable(None) is None


# -- a probe's reply ------------------------------------------------------------------------------


def _probe_rows_after(work: Path, monkeypatch, model) -> list[dict]:
    """Run the probe with ``model``; the run finishes, every question is stored, and a rerun asks none."""
    _model(monkeypatch, model)
    assert cli.main(_argv(work, "probe")) == 0
    rows = _rows(work, "probe_answers")
    asked = len(model.calls)
    assert len(rows) == asked > TARGET
    assert cli.main(_argv(work, "probe")) == 0
    assert len(model.calls) == asked
    return rows


def test_a_probe_reply_holding_one_is_stored_and_its_forecast_is_read(work, monkeypatch):
    text = f"Up {HIGH} I think.\n\n{forecast_text()}"
    rows = _probe_rows_after(work, monkeypatch, _Forecaster(text_for={TARGET: text}))
    row = _target(work, "probe_answers")
    assert row["raw_response"] == text.replace(HIGH, FFFD)
    # The forecast is the one the reply gives, as every other question's is.
    others = {r["forecast_json"] for r in rows if r["input_id"] != TARGET_ID}
    assert row["invalid_reason"] is None and {row["forecast_json"]} == others


def test_a_probe_reply_that_is_no_forecast_and_holds_one_is_stored_as_invalid(work, monkeypatch):
    text = f"I think it goes up {HIGH}."
    _probe_rows_after(work, monkeypatch, _Forecaster(text_for={TARGET: text}))
    row = _target(work, "probe_answers")
    assert (row["raw_response"], row["invalid_reason"]) == (text.replace(HIGH, FFFD), "invalid_probe")
    assert row["forecast_json"] is None


def test_a_probe_reply_that_is_ascii_and_names_one_in_a_key_is_stored(work, monkeypatch):
    # The reply stores as it is: only the sentence that refuses it, built
    # from the keys ``json.loads`` decoded, holds the character.
    text = forecast_text(h4={**H4, f"why {HIGH}": 0.0})
    assert text.isascii() and ESCAPED in text
    _probe_rows_after(work, monkeypatch, _Forecaster(text_for={TARGET: text}))
    row = _target(work, "probe_answers")
    assert (row["raw_response"], row["invalid_reason"]) == (text, "invalid_probe")
    assert f"why {FFFD}" in row["invalid_detail"] and HIGH not in row["invalid_detail"]


def test_a_model_name_holding_one_is_stored_on_a_probe_answer_and_on_a_decision(
    work, twin, monkeypatch
):
    _probe_rows_after(work, monkeypatch, _Forecaster(model=f"forecaster {HIGH}"))
    assert _target(work, "probe_answers")["model_reported"] == f"forecaster {FFFD}"
    _model(monkeypatch, _Echo(model=f"echo {HIGH}"))
    assert cli.main(_argv(twin, "replay")) == 0
    assert _target(twin, "answers")["model_reported"] == f"echo {FFFD}"


# -- a provider's refusal ---------------------------------------------------------------------------


def _without_times(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if not k.endswith("_at")} for row in rows]


@pytest.mark.parametrize("command", ["replay", "probe", "ask"])
def test_a_refusal_whose_words_hold_one_is_recorded_as_any_refusal_is(
    work, twin, monkeypatch, command
):
    # The same run twice: refused in words that hold a lone surrogate, and
    # in the words those become. What is stored has to be the same, and the
    # command has to end the same way.
    said = f"content filter tripped on {HIGH}"

    def run(directory: Path, refusal: str) -> int:
        if command == "replay":
            _model(monkeypatch, _Echo(refusal=refusal))
        elif command == "probe":
            _model(monkeypatch, _Forecaster(refusal=refusal))
        else:
            _graph(monkeypatch, refusal=refusal)
        return cli.main(_argv(directory, command))

    code = run(work, said)
    assert code == run(twin, said.replace(HIGH, FFFD))
    tables = ("probe_answers",) if command == "probe" else ("answers", "failures")
    for table in tables:
        assert _without_times(_rows(work, table)) == _without_times(_rows(twin, table))
    # And the refusal itself is there, in the words it could be stored in.
    if command == "probe":
        row = _target(work, "probe_answers")
        assert row["invalid_reason"] == "refused"
        assert f"tripped on {FFFD}" in row["invalid_detail"]
    else:
        assert f"tripped on {FFFD}" in _target(work, "failures")["error"]
        # Every other question was answered: the run went on past the refusal.
        answered = {row["input_id"] for row in _rows(work, "answers")}
        assert TARGET_ID not in answered and len(answered) > TARGET + 1


# -- a decision's reply (already storable through the borrowed parse seam) ------------------------


@pytest.mark.parametrize("command", ["replay", "ask"])
@pytest.mark.parametrize(
    ("text", "stored", "invalid"),
    [
        (f"I would rather not say {HIGH}.", f"I would rather not say {FFFD}.", True),
        (_decision(ensure_ascii=False), _decision(ensure_ascii=False).replace(HIGH, FFFD), False),
        (_decision(ensure_ascii=True), _decision(ensure_ascii=True), False),
    ],
    ids=["in-prose", "in-the-rationale", "escaped-in-the-rationale"],
)
def test_a_decision_reply_holding_one_is_stored_and_the_run_goes_on(
    work, monkeypatch, command, text, stored, invalid
):
    if command == "replay":
        fake = _Echo(text_for={TARGET: text})
        _model(monkeypatch, fake)
        asked = lambda: len(fake.calls)  # noqa: E731 - read twice, after each run
    else:
        built = _graph(monkeypatch, text_for={TARGET: text})
        asked = lambda: sum(len(graph.asked) for graph in built)  # noqa: E731
    assert cli.main(_argv(work, command)) == 0
    row = _target(work, "answers")
    assert row["raw_response"] == stored
    assert (row["invalid_reason"] is not None) is invalid
    # Every question that was asked is stored, and a rerun asks none again.
    first = asked()
    assert len(_rows(work, "answers")) == first > TARGET + 1
    assert cli.main(_argv(work, command)) == 0
    assert asked() == first
