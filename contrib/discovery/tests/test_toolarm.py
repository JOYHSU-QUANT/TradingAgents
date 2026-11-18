"""The tool-using hypothesist: what a round does, where it stops, how it fails, and what it writes down (tools plan §4, T3 a–h).

A scripted chat client stands in for the model: it is bound with the tools
and answers a script, a reply at a time, and keeps every conversation it
was asked with. The look-ups' server is the real one — connected to in
memory for most of the suite, started as a stdio child where the process
is the point (that it answers, that its dying is a failed seam, that its
stderr is kept).

Five groups. A ROUND: the model looks things up, each result goes back
with what is left, and the last message is the answer. THE LIMITS: the
call past the limit is not run, the third backtest is not run, and a model
that will not stop is asked once more without tools. A FAILED SEAM: the
server or the model not answering is a ``HypothesistError`` and spends no
round. THE TRACE: every line parses, carries what the plan lists, and
holds neither prompt. THE PACK: what a model is told is pinned to a
version, and states facts.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.hypothesis import ChatHypothesist, build_system_prompt, search
from contrib.autoresearch.ledger import Ledger, ProposalOutcome
from contrib.autoresearch.ports import Hypothesist, HypothesistError
from contrib.autoresearch.store import ResearchStore
from contrib.discovery import toolarm, tools_server
from contrib.discovery.landing import artifact_digest
from contrib.discovery.manifest import WorldKind
from contrib.discovery.research_tools import call
from contrib.discovery.toolarm import (
    BACKTEST_TOOL,
    BACKTESTS_SPENT,
    DEFAULT_LIMITS,
    FORCED_ASK,
    FORCED_HEADER,
    FORCED_NOTE_CAP,
    NOT_AN_OBJECT,
    NOTE_CAP,
    PACK_VERSION,
    REMAINING,
    SENTENCES,
    SPENT,
    TRACE_KEYS,
    TRACE_VERSION,
    Limits,
    Pack,
    ToolArmError,
    ToolHypothesist,
    ToolServerError,
    addendum,
)
from contrib.discovery.tools_server import (
    IMPORT_ROOT,
    SERVER_NAME,
    STORE_CANNOT_ANSWER,
    TOOLS_VERSION,
    ServerParams,
    build_server,
    load_view,
    tool_specs,
)

from .conftest import BUY, BUY_12, COIN, FLAT, arm_store, open_arm
from .test_research_tools import _rewrite
from .test_tools_server import _refusal

# The digest of everything a model of this arm reads beyond the two
# prompts, at the version it reads it under: the server's tools as bound,
# the paragraph appended to the system prompt, the sentences the arm says.
# Changing any of it moves the digest: bump ``PACK_VERSION`` with it (after
# the wording is frozen, plan §5, that is a new set of runs) and pin both.
PINNED = (1, "ceae0b2e47608083dfb1e1e56c0420e99d9d842ebd2273563e7a4a189a5d25c2")

SYSTEM = "THE-SYSTEM-PROMPT as the research package wrote it."
USER = "THE-USER-PROMPT with what was tried. Propose ONE new rule."
ANSWER = json.dumps(BUY)
LABEL = "scripted/model"

STATS = ("feature_stats", {"feature": "close"})
RETURNS = (
    "conditional_returns",
    {"conditions": [{"left": "rsi_14", "op": "<", "right": 45}], "horizon": 4, "side": "long"},
)
BACKTEST = (BACKTEST_TOOL, {"rule": BUY_12})
# A feature the vocabulary does not have, and one the suite's failing servers fail on.
UNKNOWN = ("feature_stats", {"feature": "sma_9"})
FATAL = ("feature_stats", {"feature": "volume"})

_ids = itertools.count(1)


def says(text="", *calls, tokens=(120, 30), invalid=()):
    """A reply as a chat client hands one over: text, the calls it asks for, what it cost."""
    return SimpleNamespace(
        content=text,
        tool_calls=[
            {"name": tool, "args": arguments, "id": f"call_{next(_ids)}", "type": "tool_call"}
            for tool, arguments in calls
        ],
        invalid_tool_calls=list(invalid),
        usage_metadata={
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    )


class Scripted:
    """A chat client that answers a script, asked with the tools bound or without.

    ``asked`` keeps each conversation it was handed and whether the tools
    were bound for it; a step that is an exception is raised; a script run
    dry is a provider that went away.
    """

    def __init__(self, script):
        self.script = list(script)
        self.tools = None
        self.asked: list[tuple[bool, list]] = []

    def bind_tools(self, tools):
        self.tools = tools
        return SimpleNamespace(invoke=lambda messages: self._answer(True, messages))

    def invoke(self, messages):
        return self._answer(False, messages)

    def _answer(self, bound, messages):
        self.asked.append((bound, messages))
        if not self.script:
            raise RuntimeError("the provider is down")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture(scope="module")
def opened(history, tmp_path_factory):
    """One arm's store with its experiment opened: the path and the experiment."""
    path = tmp_path_factory.mktemp("toolarm") / "arm.sqlite"
    with arm_store(history, path) as store:
        experiment = open_arm(Ledger(store))
    return path, experiment


@pytest.fixture(scope="module")
def params(opened) -> ServerParams:
    return ServerParams.for_experiment(*opened)


@pytest.fixture(scope="module")
def view(params):
    return load_view(params)


@contextmanager
def arm(server, script, tmp_path, **kwargs):
    """A hypothesist over a scripted model and ``server``, its trace under ``tmp_path``."""
    model = Scripted(script)
    chat = ChatHypothesist(model=model, label=LABEL)
    with ToolHypothesist.open(
        chat, server, trace=tmp_path / "trace.jsonl", **kwargs
    ) as hypothesist:
        yield hypothesist, model


def trace_text(tmp_path) -> str:
    return (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def traced(tmp_path, kind: str | None = None) -> list[dict]:
    """The trace's lines, or those of one kind."""
    lines = [json.loads(line) for line in trace_text(tmp_path).splitlines()]
    return lines if kind is None else [line for line in lines if line["kind"] == kind]


@contextmanager
def arm_copy(opened, tmp_path):
    """A fresh copy of the opened arm's store: its ledger, and the experiment already in it."""
    path, experiment = opened
    with arm_store(path, tmp_path / "arm.sqlite") as store:
        yield Ledger(store), experiment


def left(lookups: int, backtests: int) -> str:
    return REMAINING.format(lookups=lookups, backtests=backtests)


def answered(view, tool, arguments, lookups, backtests) -> str:
    """A look-up's result as the model is handed it: the server's text, then what is left."""
    return f"{json.dumps(call(view, tool, arguments))}\n{left(lookups, backtests)}"


def results(model) -> list[dict]:
    """The tool messages of the longest conversation the model was asked with."""
    _bound, messages = max(model.asked, key=lambda asked: len(asked[1]))
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]


# -- (a) a round ----------------------------------------------------------------------------------


def test_a_model_looks_three_things_up_and_its_last_message_is_the_answer(view, tmp_path):
    script = [
        says("I check where the close lies.", STATS),
        says("Then what follows a low RSI.", RETURNS),
        says("Then the whole rule.", BACKTEST),
        says(ANSWER),
    ]
    replies = list(script)
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert isinstance(hypothesist, Hypothesist)
        assert hypothesist.label == LABEL
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
        pack = hypothesist.pack

    # Bound with the tools as the server lists them, which are this build's.
    assert pack == Pack.of(tool_specs(), DEFAULT_LIMITS)
    assert model.tools == list(pack.tools)
    assert [tool["function"]["name"] for tool in model.tools] == list(tools_server.TOOL_NAMES)
    assert all(
        set(tool["function"]) == {"name", "description", "parameters"} for tool in model.tools
    )

    # Four calls of the model, each with the tools bound; the first is the
    # research package's two prompts, the paragraph appended to the first.
    assert [bound for bound, _messages in model.asked] == [True] * 4
    assert model.asked[0][1] == [
        ("system", f"{SYSTEM}\n\n{addendum(DEFAULT_LIMITS)}"),
        ("human", USER),
    ]
    # The last is the whole round: each reply as the client gave it, each
    # result after it — the look-up's own answer, then what is left.
    assert model.asked[-1][1][2:] == [
        replies[0],
        {
            "role": "tool",
            "content": answered(view, *STATS, 4, 2),
            "tool_call_id": replies[0].tool_calls[0]["id"],
            "status": "success",
        },
        replies[1],
        {
            "role": "tool",
            "content": answered(view, *RETURNS, 3, 2),
            "tool_call_id": replies[1].tool_calls[0]["id"],
            "status": "success",
        },
        replies[2],
        {
            "role": "tool",
            "content": answered(view, *BACKTEST, 2, 1),
            "tool_call_id": replies[2].tool_calls[0]["id"],
            "status": "success",
        },
    ]

    lines = traced(tmp_path)
    assert [line["kind"] for line in lines] == [
        "pack",
        "model",
        "tool",
        "model",
        "tool",
        "model",
        "tool",
        "model",
        "answer",
    ]
    assert [line["seq"] for line in lines[1:]] == list(range(1, 9))
    assert {line["round"] for line in lines[1:]} == {1}
    # (g) What the model wrote between calls is kept, and what it cost.
    assert [line["text"] for line in traced(tmp_path, "model")] == [
        "I check where the close lies.",
        "Then what follows a low RSI.",
        "Then the whole rule.",
        ANSWER,
    ]
    assert all(
        (line["input_tokens"], line["output_tokens"], line["bound"]) == (120, 30, True)
        for line in lines
        if line["kind"] == "model"
    )
    tools = traced(tmp_path, "tool")
    assert [(line["tool"], line["arguments"]) for line in tools] == [STATS, RETURNS, BACKTEST]
    assert [line["result"] for line in tools] == [m["content"] for m in results(model)]
    assert all(line["sent"] and not line["tool_error"] and line["limit"] is None for line in tools)
    assert lines[-1] == {
        "kind": "answer",
        "round": 1,
        "seq": 8,
        "forced": False,
        "lookups": 3,
        "backtests": 1,
        "tool_errors": 0,
        "text": ANSWER,
        "text_chars": len(ANSWER),
    }


def test_a_model_that_looks_nothing_up_just_answers_and_each_round_starts_afresh(view, tmp_path):
    script = [says(ANSWER), says("One look.", STATS), says(json.dumps(FLAT))]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
        assert hypothesist.propose(SYSTEM, "THE-SECOND-PROMPT") == json.dumps(FLAT)
    # Nothing of the first round is in the second: it starts from the two
    # prompts it is handed (plan §2.4).
    assert model.asked[1][1] == [
        ("system", f"{SYSTEM}\n\n{addendum(DEFAULT_LIMITS)}"),
        ("human", "THE-SECOND-PROMPT"),
    ]
    lines = traced(tmp_path)[1:]
    assert [(line["round"], line["seq"], line["kind"]) for line in lines] == [
        (1, 1, "model"),
        (1, 2, "answer"),
        (2, 1, "model"),
        (2, 2, "tool"),
        (2, 3, "model"),
        (2, 4, "answer"),
    ]
    # And the limits are a round's: the second round's first call has all of them.
    assert results(model)[0]["content"].endswith(left(4, 2))


def test_the_hypothesist_answers_the_research_package_s_own_loop(opened, view, tmp_path):
    # Through ``search()`` itself, over an arm's own store: two rounds, each
    # an answer the loop parses, measures and files; the look-ups file nothing.
    with arm_copy(opened, tmp_path) as (ledger, experiment):
        script = [
            says("One look.", STATS),
            says(ANSWER),
            says("And a backtest.", BACKTEST),
            says("no rule"),
        ]
        with arm(build_server(view), script, tmp_path) as (hypothesist, model):
            report = search(ledger, experiment, hypothesist, model=hypothesist.label, max_trials=2)
        assert report.stopped is None and report.spent == 2
        assert [round_.outcome for round_ in report.rounds] == [
            ProposalOutcome.MEASURED,
            ProposalOutcome.REFUSED,
        ]
        assert len(ledger.trials(experiment.experiment_id)) == 1
        assert report.rounds[0].proposal.model == LABEL
    # The system prompt is the loop's, with the paragraph after it; the user
    # prompt is the loop's, the same through a round and new for the next.
    assert model.asked[0][1][0] == (
        "system",
        f"{build_system_prompt()}\n\n{addendum(DEFAULT_LIMITS)}",
    )
    prompts = [messages[1][1] for _bound, messages in model.asked]
    assert prompts[0] == prompts[1] and prompts[2] == prompts[3] != prompts[0]
    assert prompts[0].startswith(f"Market: {COIN} perpetual")

    # (f) The second round's prompt lists the first rule's validation
    # figures; no line of either prompt is in the trace.
    scored = [line for line in prompts[2].splitlines() if "validation sharpe" in line]
    assert scored
    written = trace_text(tmp_path)
    for line in (*scored, *prompts[0].splitlines()[:2]):
        assert line.strip() and line.strip() not in written, line


# -- (b) (c) (d) the limits ------------------------------------------------------------------------


def _counted(monkeypatch) -> list[tuple]:
    """Every look-up that reaches the server's side, in order."""
    reached: list[tuple] = []
    real = tools_server.call

    def counting(view, tool, arguments):
        reached.append((tool, arguments))
        return real(view, tool, arguments)

    monkeypatch.setattr(tools_server, "call", counting)
    return reached


def test_the_sixth_call_is_not_run_and_says_the_calls_are_spent(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    script = [*(says(f"look {n}", STATS) for n in range(1, 7)), says(ANSWER)]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [STATS] * 5
    *run, sixth = results(model)
    assert [message["content"] for message in run] == [
        answered(view, *STATS, remaining, 2) for remaining in (4, 3, 2)
    ] + [answered(view, *STATS, 1, 1), answered(view, *STATS, 0, 0)]
    # The sixth: the sentence and nothing else, marked as a call that failed.
    assert sixth["content"] == SPENT and sixth["status"] == "error"
    assert all(message["status"] == "success" for message in run)
    lines = traced(tmp_path)
    over = traced(tmp_path, "tool")[-1]
    assert (over["limit"], over["sent"], over["tool_error"], over["result"]) == (
        "lookups",
        False,
        False,
        SPENT,
    )
    # The model answered once told: nothing was forced, and five were spent.
    assert (lines[-1]["forced"], lines[-1]["lookups"], lines[-1]["backtests"]) == (False, 5, 0)
    assert all(bound for bound, _messages in model.asked)


def test_the_third_backtest_is_not_run_and_the_other_look_ups_go_on(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    script = [
        says("first", BACKTEST),
        says("second", BACKTEST),
        says("third", BACKTEST),
        says("something else", STATS),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [BACKTEST, BACKTEST, STATS]
    first, second, third, other = results(model)
    assert first["content"] == answered(view, *BACKTEST, 4, 1)
    assert second["content"] == answered(view, *BACKTEST, 3, 0)
    # Not run, and it counted: a call is a call.
    assert third["content"] == f"{BACKTESTS_SPENT}\n{left(2, 0)}" and third["status"] == "error"
    assert other["content"] == answered(view, *STATS, 1, 0)
    tools = traced(tmp_path, "tool")
    assert [(line["limit"], line["sent"]) for line in tools] == [
        (None, True),
        (None, True),
        ("backtests", False),
        (None, True),
    ]
    answer = traced(tmp_path)[-1]
    assert (answer["lookups"], answer["backtests"], answer["forced"]) == (4, 2, False)


def test_what_is_left_of_the_backtests_is_never_more_than_what_is_left_at_all(view, tmp_path):
    script = [says("", STATS), says("", STATS), says(ANSWER)]
    with arm(build_server(view), script, tmp_path, limits=Limits(lookups=3, backtests=2)) as (
        hypothesist,
        model,
    ):
        hypothesist.propose(SYSTEM, USER)
        assert "at most 3 tool call(s), of which at most 2 may be" in model.asked[0][1][0][1]
    assert [message["content"].splitlines()[-1] for message in results(model)] == [
        left(2, 2),
        left(1, 1),
    ]


def test_a_model_that_will_not_stop_is_asked_once_more_with_no_tools(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    script = [
        *(says(f"look {n}", STATS) for n in range(1, 6)),
        says("a sixth", STATS),  # told the calls are spent
        says("a seventh", RETURNS),  # and calls again
        says(ANSWER),  # asked without tools
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [STATS] * 5
    assert [bound for bound, _messages in model.asked] == [True] * 7 + [False]
    # The last conversation is a fresh one: the research package's system
    # prompt with no paragraph about tools, and the user prompt followed by
    # the round's look-ups as text and the ask.
    system, human = model.asked[-1][1]
    assert system == ("system", SYSTEM)
    body = json.dumps(call(view, *STATS))
    looked = "\n".join(
        f"note: look {n}\n{STATS[0]} {json.dumps(STATS[1])}\n-> {body}" for n in range(1, 6)
    )
    assert human == (
        "human",
        f"{USER}\n\n{FORCED_HEADER}\n{looked}\nnote: a sixth\n\n{FORCED_ASK}",
    )
    lines = traced(tmp_path)
    assert [line["bound"] for line in traced(tmp_path, "model")] == [True] * 7 + [False]
    assert lines[-1]["forced"] is True and lines[-1]["lookups"] == 5
    # The seventh call was never answered: the conversation it was made in was left.
    assert len(traced(tmp_path, "tool")) == 6


def test_calls_made_in_one_message_are_counted_one_by_one(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    burst = says("all at once", *([STATS] * 4), BACKTEST, RETURNS, STATS)
    with arm(build_server(view), [burst, says(ANSWER)], tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [STATS] * 4 + [BACKTEST]
    messages = results(model)
    # Every call is answered, in the order it was made: a provider refuses
    # a conversation in which one is left hanging.
    assert [message["tool_call_id"] for message in messages] == [
        made["id"] for made in burst.tool_calls
    ]
    assert [message["content"] for message in messages[5:]] == [SPENT, SPENT]
    assert messages[4]["content"] == answered(view, *BACKTEST, 0, 0)


# -- (h) a mistake of the model's ------------------------------------------------------------------


def test_a_mistake_of_the_model_s_is_shown_to_it_marked_and_counted(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    no_such_feature = UNKNOWN
    no_such_tool = ("no_such_tool", {})
    not_a_number = ("conditional_returns", {**RETURNS[1], "horizon": float("nan")})
    unparsed = {
        "name": "feature_stats",
        "args": '{"feature": ',
        "id": "call_unparsed",
        "error": "x",
    }
    script = [
        says("three mistakes", no_such_feature, no_such_tool, not_a_number, invalid=[unparsed]),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER

    # What the server refuses comes back in the look-up's own sentence;
    # arguments that are no JSON object never reach it. All four count.
    assert reached == [no_such_feature, no_such_tool]
    messages = results(model)
    assert [message["content"] for message in messages] == [
        f"{_refusal(view, *no_such_feature)}\n{left(4, 2)}",
        f"{_refusal(view, *no_such_tool)}\n{left(3, 2)}",
        f"{NOT_AN_OBJECT}\n{left(2, 2)}",
        f"{NOT_AN_OBJECT}\n{left(1, 1)}",
    ]
    assert [message["status"] for message in messages] == ["error"] * 4
    assert messages[3]["tool_call_id"] == "call_unparsed"
    lines = traced(tmp_path)
    tools = traced(tmp_path, "tool")
    assert [(line["tool_error"], line["sent"], line["limit"]) for line in tools] == [
        (True, True, None),
        (True, True, None),
        (True, False, None),
        (True, False, None),
    ]
    assert [line["arguments"] for line in tools] == [{"feature": "sma_9"}, {}, None, None]
    assert (lines[-1]["lookups"], lines[-1]["tool_errors"]) == (4, 4)


# -- (e) a failed seam ----------------------------------------------------------------------------


def _unanswerable(monkeypatch, feature: str = "volume") -> None:
    """The server's history cannot answer a look-up of ``feature``; the sentence names an instant."""
    real = tools_server.call

    def failing(view, tool, arguments):
        if arguments == {"feature": feature}:
            raise EvaluationError("the window ending 2026-03-01T00:00:00+00:00 has no coverage")
        return real(view, tool, arguments)

    monkeypatch.setattr(tools_server, "call", failing)


def test_a_look_up_the_server_cannot_answer_is_a_failed_seam(view, tmp_path, monkeypatch):
    _unanswerable(monkeypatch)
    script = [says("fine", STATS), says("not fine", FATAL)]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        with pytest.raises(ToolServerError) as failed:
            hypothesist.propose(SYSTEM, USER)
        # The server's fixed sentence, and nothing of the research package's.
        assert isinstance(failed.value, HypothesistError)
        assert STORE_CANNOT_ANSWER in str(failed.value) and "2026" not in str(failed.value)
        # The connection is not tried again: the next round fails as the
        # first did, and no model call is spent finding that out.
        asked = len(model.asked)
        with pytest.raises(ToolServerError, match=re.escape(STORE_CANNOT_ANSWER)):
            hypothesist.propose(SYSTEM, USER)
        assert len(model.asked) == asked == 2
    lines = traced(tmp_path)
    assert lines[-1] == {
        "kind": "failed",
        "round": 1,
        "seq": 5,
        "side": "server",
        "error": "MCPError",
    }
    # The call the search ended on is written down, with no result: which
    # look-up it was is otherwise on the server's stderr alone.
    ended_on = lines[-2]
    assert (ended_on["kind"], ended_on["seq"], ended_on["tool"]) == ("tool", 4, "feature_stats")
    assert ended_on["arguments"] == {"feature": "volume"} and ended_on["sent"] is True
    assert (ended_on["result"], ended_on["result_chars"], ended_on["tool_error"]) == (
        None,
        0,
        False,
    )
    assert "2026" not in trace_text(tmp_path)


def test_a_server_that_fails_ends_the_search_and_spends_no_round(
    opened, view, tmp_path, monkeypatch
):
    _unanswerable(monkeypatch)
    with arm_copy(opened, tmp_path) as (ledger, experiment):
        script = [says(ANSWER), says("", FATAL), says(ANSWER)]
        with arm(build_server(view), script, tmp_path) as (hypothesist, _model):
            report = search(ledger, experiment, hypothesist, model=LABEL, max_trials=5)
        # One round filed; the second is not an answer, so it is no refusal
        # and no round, and the run stops there.
        assert report.spent == 1 and STORE_CANNOT_ANSWER in report.stopped
        assert len(ledger.trials(experiment.experiment_id)) == 1
        assert ledger.refusals(experiment.experiment_id, 10) == []


# The look-ups' server with one look-up that goes wrong in the server's own
# way: asked for the volume's stats, the process does ``{fate}``.
_FAILING = """
import os, sys, time
from contrib.discovery import tools_server
real = tools_server.call
def failing(view, tool, arguments):
    if arguments == {{"feature": "volume"}}:
        {fate}
    return real(view, tool, arguments)
tools_server.call = failing
raise SystemExit(tools_server.main(sys.argv[1:]))
"""


def _failing_child(params, fate: str):
    code = _FAILING.format(fate=fate)
    return stdio_client(
        StdioServerParameters(
            command=sys.executable, args=["-c", code, *params.argv()], cwd=str(IMPORT_ROOT)
        )
    )


def test_a_server_that_dies_mid_round_is_a_failed_seam_and_not_a_hang(params, view, tmp_path):
    # The real child, over stdio, leaving without a word in the middle of a
    # call: what the plan means by the server dying.
    script = [says("fine", STATS), says("fatal", FATAL)]
    child = _failing_child(params, "os._exit(3)")
    with arm(child, script, tmp_path, call_timeout=30) as (hypothesist, model):
        with pytest.raises(ToolServerError, match="did not answer 'feature_stats'") as failed:
            hypothesist.propose(SYSTEM, USER)
        # A closed connection, said as that, and at once: not the timeout.
        assert "Connection closed" in str(failed.value)
        assert "timed out" not in str(failed.value)
        with pytest.raises(ToolServerError):
            hypothesist.propose(SYSTEM, USER)
    # The look-up before it was answered as the view answers it.
    assert results(model)[0]["content"] == answered(view, *STATS, 4, 2)
    assert traced(tmp_path)[-1]["side"] == "server"


def test_a_look_up_that_never_comes_back_is_a_failed_seam_after_the_call_s_timeout(
    params, tmp_path
):
    script = [says("fine", STATS), says("stuck", FATAL)]
    child = _failing_child(params, "time.sleep(600)")
    with arm(child, script, tmp_path, call_timeout=1) as (hypothesist, model):
        started = time.monotonic()
        with pytest.raises(ToolServerError, match="did not answer 'feature_stats'") as failed:
            hypothesist.propose(SYSTEM, USER)
        # The call's own timeout, not the handshake's and not the child's nap.
        assert "timed out" in str(failed.value)
        assert time.monotonic() - started < 60
    assert len(results(model)) == 1


def test_a_server_that_does_not_start_is_a_failed_seam_before_any_model_call(params, tmp_path):
    missing = replace(params, db=tmp_path / "missing.sqlite")
    model = Scripted([says(ANSWER)])
    chat = ChatHypothesist(model=model, label=LABEL)
    log = tmp_path / "server.log"
    with (
        pytest.raises(ToolServerError, match="did not start") as failed,
        ToolHypothesist.open(chat, missing, trace=tmp_path / "trace.jsonl", server_log=log),
    ):
        raise AssertionError("opened over a server that refused its parameters")
    assert model.asked == [] and model.tools is None
    # What the client saw is a closed connection, read out of the task
    # group the SDK raises it in.
    assert "MCPError: Connection closed" in str(failed.value)
    # Why is on the child's stderr, which was kept; and no trace was begun.
    assert "is not a file with a store in it" in log.read_text(encoding="utf-8")
    assert not (tmp_path / "trace.jsonl").exists()


def test_a_server_that_never_answers_the_handshake_is_a_failed_seam_after_the_connect_timeout(
    tmp_path, monkeypatch
):
    # The SDK first probes for its newer handshake under a bound of its own
    # (ten seconds), which the caller's does not move: shortened here so the
    # test waits on the bound it is about. Left alone if the SDK renames it.
    monkeypatch.setattr("mcp.client.session.DISCOVER_TIMEOUT_SECONDS", 0.2, raising=False)
    # A child that starts and says nothing: the bound on the start is the
    # caller's, not the life of the child.
    silent = stdio_client(
        StdioServerParameters(command=sys.executable, args=["-c", "import time; time.sleep(600)"])
    )
    model = Scripted([says(ANSWER)])
    started = time.monotonic()
    with (
        pytest.raises(ToolServerError, match="did not start") as failed,
        ToolHypothesist.open(
            ChatHypothesist(model=model, label=LABEL),
            silent,
            trace=tmp_path / "trace.jsonl",
            connect_timeout=1,
        ),
    ):
        raise AssertionError("opened over a server that never answered")
    assert "timed out" in str(failed.value)
    assert time.monotonic() - started < 120
    assert model.asked == [] and not (tmp_path / "trace.jsonl").exists()


def test_a_model_that_does_not_answer_is_a_failed_seam_and_the_next_round_may_go_on(view, tmp_path):
    script = [
        says("one look", STATS),
        RuntimeError("401: the key sk-not-a-real-key was refused"),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, _model):
        with pytest.raises(HypothesistError) as failed:
            hypothesist.propose(SYSTEM, USER)
        assert not isinstance(failed.value, ToolServerError)
        assert str(failed.value).startswith(f"{LABEL} did not answer: 401")
        # Nothing here is broken for good: the server is still there.
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    lines = traced(tmp_path)
    failure = next(line for line in lines if line["kind"] == "failed")
    # The class, and none of the provider's sentence.
    assert failure == {
        "kind": "failed",
        "round": 1,
        "seq": 3,
        "side": "model",
        "error": "RuntimeError",
    }
    assert "sk-not-a-real-key" not in trace_text(tmp_path)


@pytest.mark.parametrize(
    ("reply", "sentence"),
    [
        (SimpleNamespace(content=[{"type": "text", "text": "x"}]), "answered with list, not text"),
        (SimpleNamespace(content="", tool_calls="feature_stats"), "tool_calls as str, not a list"),
        (SimpleNamespace(content="", tool_calls=["feature_stats"]), "a tool call as str"),
        (
            SimpleNamespace(content="", tool_calls=[{"name": "feature_stats", "args": {}}]),
            "a tool call with no id",
        ),
        (
            SimpleNamespace(content="", tool_calls=[{"id": "call_x", "args": {}}]),
            "a tool call with no tool named",
        ),
    ],
)
def test_a_reply_that_cannot_be_read_is_a_failed_seam(view, tmp_path, reply, sentence):
    with (
        arm(build_server(view), [reply], tmp_path) as (hypothesist, _model),
        pytest.raises(HypothesistError, match=re.escape(sentence)),
    ):
        hypothesist.propose(SYSTEM, USER)
    # Written down as the model's side, under the seam's own class: there
    # is no exception underneath a reply that came back and could not be read.
    failure = traced(tmp_path)[-1]
    assert (failure["kind"], failure["side"], failure["error"]) == (
        "failed",
        "model",
        "HypothesistError",
    )


def test_a_reply_with_only_text_is_read_whatever_else_it_lacks(view, tmp_path):
    # The least a client's reply carries: no list of calls, no usage.
    with arm(build_server(view), [SimpleNamespace(content=ANSWER)], tmp_path) as (hypothesist, _m):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    model_line = traced(tmp_path)[1]
    assert (model_line["input_tokens"], model_line["output_tokens"]) == (None, None)


def test_an_interrupt_inside_the_model_s_call_passes_through(view, tmp_path):
    with (
        arm(build_server(view), [KeyboardInterrupt()], tmp_path) as (hypothesist, _model),
        pytest.raises(KeyboardInterrupt),
    ):
        hypothesist.propose(SYSTEM, USER)
    # Not a failed seam, so not written down as one.
    assert [line["kind"] for line in traced(tmp_path)] == ["pack"]


# -- (f) (g) the trace ----------------------------------------------------------------------------

# The trace's shape, written out a second time: the module's own table
# (``TRACE_KEYS``) is what the writer holds each line to, and this is what
# holds the table. A change to either is a change of ``TRACE_VERSION``.
_TRACE_PINNED = 1
_KEYS = {
    "pack": {
        "kind",
        "trace_version",
        "pack_version",
        "pack_digest",
        "server",
        "model",
        "tools_version",
        "limits",
        "addendum",
        "sentences",
        "forced_note_chars",
        "tools",
    },
    "model": {
        "kind",
        "round",
        "seq",
        "bound",
        "text",
        "text_chars",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "elapsed_ms",
    },
    "tool": {
        "kind",
        "round",
        "seq",
        "tool",
        "arguments",
        "sent",
        "tool_error",
        "limit",
        "result",
        "result_chars",
        "elapsed_ms",
    },
    "answer": {
        "kind",
        "round",
        "seq",
        "forced",
        "lookups",
        "backtests",
        "tool_errors",
        "text",
        "text_chars",
    },
    "failed": {"kind", "round", "seq", "side", "error"},
}


def test_the_trace_opens_with_the_pack_and_every_line_is_one_of_five_shapes(
    view, tmp_path, monkeypatch
):
    _unanswerable(monkeypatch)
    note = "a long thought. " * 400
    script = [
        says(note, STATS, UNKNOWN),
        says(ANSWER),
        says("", FATAL),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, _model):
        hypothesist.propose(SYSTEM, USER)
        with pytest.raises(ToolServerError):
            hypothesist.propose(SYSTEM, USER)
        pack = hypothesist.pack
    raw = trace_text(tmp_path)
    assert raw.endswith("\n") and "\n\n" not in raw
    lines = [json.loads(line) for line in raw.splitlines()]
    assert {line["kind"] for line in lines} == set(_KEYS)
    for line in lines:
        assert set(line) == _KEYS[line["kind"]], line["kind"]
    # (g) Once, first: what the model was told, as it was sent.
    opening = lines[0]
    assert [line["kind"] for line in lines].count("pack") == 1
    assert TRACE_VERSION == _TRACE_PINNED
    assert {kind: set(keys) for kind, keys in TRACE_KEYS.items()} == _KEYS
    assert opening == {
        "kind": "pack",
        "trace_version": TRACE_VERSION,
        "pack_version": PACK_VERSION,
        "pack_digest": pack.digest(),
        "server": SERVER_NAME,
        "model": LABEL,
        **pack.document(),
    }
    assert opening["tools"] == list(pack.tools) and opening["addendum"] == addendum(DEFAULT_LIMITS)
    assert opening["limits"] == {"lookups": 5, "backtests": 2}
    assert opening["tools_version"] == TOOLS_VERSION
    # A long text is cut, and its length kept.
    thought = lines[1]
    assert thought["text"] == note[:NOTE_CAP] and thought["text_chars"] == len(note)
    assert all(isinstance(line["elapsed_ms"], int) for line in lines if "elapsed_ms" in line)
    # Neither prompt is written, nor an instant.
    assert "THE-SYSTEM-PROMPT" not in raw and "THE-USER-PROMPT" not in raw
    assert re.search(r"\b20\d\d-\d\d", raw) is None


def _timeless(tmp_path) -> list[dict]:
    return [{k: v for k, v in line.items() if k != "elapsed_ms"} for line in traced(tmp_path)]


def test_rewriting_everything_after_training_changes_no_line_of_the_trace(
    opened, view, history, tmp_path
):
    # (f) The same round over the arm's store and over one whose every bar,
    # daily bar and settlement after the last training bar is rewritten:
    # the traces are the same but for how long things took.
    _path, experiment = opened
    last = view.frame.bundle.bars[-1]
    rewritten = tmp_path / "rewritten.sqlite"
    with arm_store(history, rewritten) as store:
        assert _rewrite(store, since_ms=last.close_time + 1) > 0
    twin = load_view(ServerParams.for_experiment(rewritten, experiment))
    assert twin.frame.bundle.bars[-1] == last

    def script():
        return [
            says("everything at once", STATS, RETURNS, ("feature_stats", {"feature": "close_1d"})),
            says("and the rule", BACKTEST, ("feature_stats", {"feature": "funding_zscore_14"})),
            says(ANSWER),
        ]

    traces = []
    for name, world in (("as-landed", view), ("rewritten", twin)):
        where = tmp_path / name
        where.mkdir()
        with arm(build_server(world), script(), where) as (hypothesist, _model):
            assert hypothesist.propose(SYSTEM, USER) == ANSWER
        traces.append(_timeless(where))
    assert traces[0] == traces[1]
    # The control: the look-ups did read this world's bars.
    close = next(line for line in traces[0] if line["kind"] == "tool")
    assert json.loads(close["result"].splitlines()[0]) == call(view, *STATS)


# -- the pack -------------------------------------------------------------------------------------


def test_what_a_model_is_told_is_pinned_to_the_pack_s_version():
    pack = Pack.of(tool_specs(), DEFAULT_LIMITS)
    assert (PACK_VERSION, pack.digest()) == PINNED, (
        "what a model of the tool arm reads changed: bump PACK_VERSION and pin the new digest"
    )
    # The digest reads the server's wording, the paragraph and the limits.
    assert pack.digest() != Pack.of(tool_specs(), Limits(lookups=4, backtests=2)).digest()
    assert pack.digest() != replace(pack, tools=pack.tools[:2]).digest()
    document = pack.document()
    # Every sentence the arm can say into a conversation is in the table the
    # digest is taken over — the two a forced ask is written out with too,
    # and how much of a model's own line that ask shows it.
    assert document["sentences"] == dict(SENTENCES)
    assert set(SENTENCES.values()) >= {
        REMAINING,
        SPENT,
        BACKTESTS_SPENT,
        NOT_AN_OBJECT,
        FORCED_HEADER,
        FORCED_ASK,
    }
    assert (SENTENCES["forced_note"], SENTENCES["forced_lookup"]) == (
        "note: {text}",
        "{tool} {arguments}\n-> {result}",
    )
    assert document["forced_note_chars"] == FORCED_NOTE_CAP
    # A sentence written into the module and left out of the table would be
    # one a model reads and the digest does not: every public constant of
    # the module that is a sentence — a string with a space in it — is in
    # the table. (The paragraph's template is private, and is in the
    # document as the paragraph.)
    said = {
        value
        for name, value in vars(toolarm).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str) and " " in value
    }
    assert len(said) >= 6
    assert said <= set(SENTENCES.values()), said - set(SENTENCES.values())
    # A pack shares nothing with what it was listed from, or handed out.
    document["tools"][0]["function"]["parameters"]["properties"].clear()
    assert pack.document()["tools"][0]["function"]["parameters"]["properties"]
    assert Limits(lookups=5, backtests=2) == DEFAULT_LIMITS


def test_the_paragraph_states_facts_and_gives_no_advice():
    said = addendum(DEFAULT_LIMITS)
    for fact in (
        # The line of the system prompt the tools make untrue as written.
        "The prompt above says a rule is scored on history you cannot see",
        "A tool reads the training bars and nothing after them",
        "The validation bars stay hidden from you and from every tool",
        "the validation figures a scored rule is listed with are measured on them",
        "at most 5 tool call(s), of which at most 2 may be backtest_train",
        "Every call counts, one refused for a mistake in its arguments included",
        "Each result ends with a line saying how many are left",
        "Nothing you look up is kept for the next round",
        "Before each tool call, write one line saying what you are about to check and why",
        "ONE new rule as a single JSON object, and nothing else, in a message that calls no tool",
    ):
        assert fact in said, fact
    # The sentence the paragraph answers is the system prompt's own.
    assert "scored by a deterministic backtest on history you cannot see" in build_system_prompt()
    # What a tool can see and what a round allows — not how to read a
    # figure, nor what to do with one (decided 2026-11-19).
    lowered = said.lower()
    for advice in (
        "overfit",
        "baseline",
        "t_stat",
        "t-stat",
        "significan",
        "should",
        "careful",
        "beware",
        "prefer",
        "trust",
    ):
        assert advice not in lowered, advice
    # And nothing that names a world's kind, the coin, or an instant.
    everything = json.dumps(Pack.of(tool_specs(), DEFAULT_LIMITS).document()).lower()
    for word in ("planted", "holdout", COIN.lower(), *(kind.value for kind in WorldKind)):
        assert word not in everything, word
    assert re.search(r"\b20\d\d-\d\d", everything) is None


@pytest.mark.parametrize(
    ("limits", "sentence"),
    [
        ({"lookups": 0}, "an arm that may make no call is the model arm"),
        ({"lookups": True}, "limits.lookups: expected a whole number of calls"),
        ({"lookups": 2.0}, "limits.lookups: expected a whole number of calls"),
        ({"backtests": "2"}, "limits.backtests: expected a whole number of calls"),
        ({"backtests": -1}, "limits.backtests: expected 0 to 5"),
        ({"lookups": 2, "backtests": 3}, "limits.backtests: expected 0 to 2"),
    ],
)
def test_limits_that_are_not_limits_are_refused(limits, sentence):
    with pytest.raises(ToolArmError, match=re.escape(sentence)):
        Limits(**limits)


def test_no_backtest_at_all_is_a_limit_an_arm_may_have(view, tmp_path):
    script = [says("", BACKTEST), says(ANSWER)]
    with arm(build_server(view), script, tmp_path, limits=Limits(lookups=2, backtests=0)) as (
        hypothesist,
        model,
    ):
        hypothesist.propose(SYSTEM, USER)
    assert results(model)[0]["content"] == f"{BACKTESTS_SPENT}\n{left(1, 0)}"


# -- how it is started -----------------------------------------------------------------------------


def _refused(view, tmp_path, sentence, *, model=None, server=None, **kwargs) -> None:
    chat = ChatHypothesist(model=Scripted([]) if model is None else model, label=LABEL)
    with (
        pytest.raises(ToolArmError, match=re.escape(sentence)),
        ToolHypothesist.open(
            chat,
            build_server(view) if server is None else server,
            trace=kwargs.pop("trace", tmp_path / "trace.jsonl"),
            **kwargs,
        ),
    ):
        raise AssertionError("opened")


def test_an_arm_that_cannot_be_started_as_asked_is_refused_by_name(
    view, params, tmp_path, monkeypatch
):
    # A client that takes no tools, and one that refuses them.
    _refused(view, tmp_path, "takes no tools", model=SimpleNamespace(invoke=None))

    def unsupported(_tools):
        raise NotImplementedError("this model has no tool calling")

    _refused(
        view,
        tmp_path,
        "could not be given the tools: this model has no tool calling",
        model=SimpleNamespace(bind_tools=unsupported),
    )
    _refused(view, tmp_path, "limits: expected Limits, got dict", limits={"lookups": 5})
    _refused(view, tmp_path, "this server is no child", server_log=tmp_path / "server.log")
    # None of those began a trace, or left a log.
    assert list(tmp_path.iterdir()) == []

    # A trace that exists is another search's: refused before a server is
    # started for it, so the child's log is not so much as created.
    taken = tmp_path / "taken.jsonl"
    taken.write_text("{}\n", encoding="utf-8")
    log = tmp_path / "server.log"
    _refused(
        view,
        tmp_path,
        f"trace: {taken} exists: it is one search's, and is not written over",
        trace=taken,
        server=params,
        server_log=log,
    )
    assert taken.read_text(encoding="utf-8") == "{}\n" and not log.exists()
    # So is the log of an earlier search, the one place its server said why:
    # it is not emptied for a new one.
    _refused(
        view,
        tmp_path,
        f"server_log: {taken} exists: it is one search's, and is not written over",
        server=params,
        server_log=taken,
    )
    assert taken.read_text(encoding="utf-8") == "{}\n"
    # The two are two files, and each goes in a directory that is there:
    # said now, and not after the server has warmed for nothing.
    _refused(
        view,
        tmp_path,
        "trace and server_log name one file",
        trace=log,
        server=params,
        server_log=tmp_path / "." / "server.log",
    )
    nowhere = tmp_path / "nowhere"
    _refused(
        view,
        tmp_path,
        f"trace: {nowhere} is not a directory to write trace.jsonl in",
        trace=nowhere / "trace.jsonl",
        server=params,
    )
    _refused(
        view,
        tmp_path,
        f"server_log: {nowhere} is not a directory to write server.log in",
        server=params,
        server_log=nowhere / "server.log",
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["taken.jsonl"]

    # A server at another version of its wording: this arm's pack is pinned
    # against the one this build has.
    with monkeypatch.context() as patched:
        patched.setattr(tools_server, "TOOLS_VERSION", TOOLS_VERSION + 1)
        _refused(
            view,
            tmp_path,
            f"is pinned against {SERVER_NAME!r} version {TOOLS_VERSION}",
            server=build_server(view),
        )
    assert not (tmp_path / "trace.jsonl").exists()


def test_without_the_sdk_the_arm_says_how_to_install_it_before_it_opens_anything(
    view, tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "mcp", None)
    _refused(view, tmp_path, 'pip install -e ".[discovery-tools]"', server=object())
    assert list(tmp_path.iterdir()) == []


def test_the_hypothesist_is_closed_with_its_block(view, tmp_path):
    with arm(build_server(view), [says(ANSWER), says(ANSWER)], tmp_path) as (hypothesist, _model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    with pytest.raises(RuntimeError, match="closed with its `with` block"):
        hypothesist.propose(SYSTEM, USER)


# -- as a process ---------------------------------------------------------------------------------


def test_the_arm_starts_the_server_as_a_child_and_keeps_its_stderr(params, view, tmp_path):
    before = artifact_digest(params.db)
    log = tmp_path / "server.log"
    script = [
        says("the three of them", STATS, RETURNS, BACKTEST),
        says("and a mistake", UNKNOWN),
        says(ANSWER),
    ]
    with arm(params, script, tmp_path, server_log=log) as (hypothesist, model):
        assert hypothesist.pack == Pack.of(tool_specs(), DEFAULT_LIMITS)
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    stats, returns, backtest, mistake = results(model)
    assert stats["content"] == answered(view, *STATS, 4, 2)
    assert returns["content"] == answered(view, *RETURNS, 3, 2)
    assert backtest["content"] == answered(view, *BACKTEST, 2, 1)
    assert mistake["status"] == "error"
    assert mistake["content"] == f"{_refusal(view, *UNKNOWN)}\n{left(1, 1)}"
    # The child read the arm's store and wrote nothing to it; its stderr
    # went to the file it was given.
    assert artifact_digest(params.db) == before
    assert log.is_file()
    with ResearchStore(params.db) as store:
        assert Ledger(store).rules_tried(COIN) == 0


def test_the_module_reaches_no_manifest_and_needs_no_sdk_to_import():
    code = (
        "import json, sys\n"
        "import contrib.discovery.toolarm\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('contrib.discovery.'))\n"
        "print(json.dumps({'loaded': loaded, 'sdk': 'mcp' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=IMPORT_ROOT, capture_output=True, text=True, check=True
    )
    assert json.loads(done.stdout) == {
        "loaded": [
            "contrib.discovery.research_tools",
            "contrib.discovery.toolarm",
            "contrib.discovery.tools_server",
            "contrib.discovery.upstream",
        ],
        "sdk": False,
    }
