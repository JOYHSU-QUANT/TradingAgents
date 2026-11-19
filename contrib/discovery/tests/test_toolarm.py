"""The tool-using hypothesist: what a round does, where it stops, how it fails, and what it writes down (tools plan §4, T3 a–h).

A scripted chat client stands in for the model: it is bound with the tools
and answers a script, a reply at a time, and keeps every conversation it
was asked with. The look-ups' server is the real one — connected to in
memory for most of the suite, started as a stdio child where the process
is the point (that it answers, that its dying is a failed seam, that its
stderr is kept).

The groups. A ROUND: the model looks things up, each result goes back
with what is left, and the last message is the answer. THE LIMITS: the
call past the limit is not run, the third backtest is not run, and a model
that will not stop, or whose message cannot be sent back, is asked once
more without tools. A FAILED SEAM: the
server or the model not answering is a ``HypothesistError`` and spends no
round. THE TRACE: every line parses, carries what the plan lists, and
holds neither prompt. THE PACK: what a model is told is pinned to a
version, and states facts.
"""

from __future__ import annotations

import io
import itertools
import json
import re
import subprocess
import sys
import time
from concurrent.futures import Future, wait as real_wait
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from anyio.from_thread import start_blocking_portal
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.hypothesis import (
    MAX_RESPONSE_CHARS,
    ChatHypothesist,
    build_system_prompt,
    search,
)
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
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENT_DEPTH,
    MAX_NAME_CHARS,
    NOT_SENDABLE,
    NOTE_CAP,
    PACK_VERSION,
    REMAINING,
    RESULT_CAP,
    SENTENCES,
    SPENT,
    TRACE_KEYS,
    TRACE_VERSION,
    Limits,
    Pack,
    ToolArmError,
    ToolCall,
    ToolHypothesist,
    ToolServerError,
    TraceKind,
    addendum,
    read_reply,
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
from contrib.discovery.tooluse import RoundUse, read_trace, trace_use

from .conftest import BUY, BUY_12, COIN, FLAT, arm_store, open_arm
from .test_research_tools import _rewrite
from .test_tools_server import _refusal

# The digest of everything a model of this arm reads beyond the two
# prompts, at the version it reads it under: the server's tools as bound,
# the paragraph appended to the system prompt, the sentences the arm says.
# Changing any of it moves the digest: bump ``PACK_VERSION`` with it (after
# the wording is frozen, plan §5, that is a new set of runs) and pin both.
PINNED = (1, "35bce47e9f2450191f616f5d05410c3a43da1a19108740320276ceb2f2f06719")

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

    As a real client does, it has to encode the conversation to send it:
    one it cannot — half a surrogate pair, a ``NaN`` — is refused where a
    real request would fail, so a reply put back that should not have been
    is a red test and not something only a provider would notice.
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

    @staticmethod
    def _request(messages) -> bytes:
        said = [
            message
            if isinstance(message, (tuple, dict))
            else [
                message.content,
                getattr(message, "tool_calls", None),
                getattr(message, "invalid_tool_calls", None),
            ]
            for message in messages
        ]
        return json.dumps(said, allow_nan=False, ensure_ascii=False).encode("utf-8")

    def _answer(self, bound, messages):
        self.asked.append((bound, messages))
        self._request(messages)
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
        # Flushed as written: the round is on disk while the search is still
        # open, which is all a run that dies leaves behind.
        assert [line["kind"] for line in traced(tmp_path)][-2:] == ["model", "answer"]

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
    not_an_object = ("conditional_returns", "horizon 6, long")
    unparsed = {
        "name": "feature_stats",
        "args": '{"feature": ',
        "id": "call_unparsed",
        "error": "x",
    }
    script = [
        says("four mistakes", no_such_feature, no_such_tool, not_an_object, invalid=[unparsed]),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER

    # What the server refuses comes back in the look-up's own sentence; a
    # call that cannot be sent never reaches it. All four count.
    assert reached == [no_such_feature, no_such_tool]
    messages = results(model)
    assert [message["content"] for message in messages] == [
        f"{_refusal(view, *no_such_feature)}\n{left(4, 2)}",
        f"{_refusal(view, *no_such_tool)}\n{left(3, 2)}",
        f"{NOT_SENDABLE}\n{left(2, 2)}",
        f"{NOT_SENDABLE}\n{left(1, 1)}",
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


@pytest.mark.parametrize(
    ("note", "made"),
    [
        ("a bad argument", ("feature_stats", {"feature": "clo\ud83dse"})),
        ("a bad name", ("feature_stats\ud83d", {"feature": "close"})),
        ("not a number", ("conditional_returns", {**RETURNS[1], "horizon": float("nan")})),
        ("a bad \ud83d note", STATS),
    ],
    ids=["half a pair in arguments", "half a pair in a name", "a NaN", "half a pair in the text"],
)
def test_a_message_that_cannot_be_sent_back_ends_the_round_s_tool_use_and_not_the_search(
    view, tmp_path, monkeypatch, note, made
):
    # Put back into the conversation, such a message is one no client can
    # encode a request from (measured with the repo's own client, a lone
    # surrogate: the next call of the model raises before anything is
    # sent), and a slip of the model's would end the search as a model
    # that did not answer. So the conversation is left there, and the rule
    # is asked for without tools — with what the round did look up.
    reached = _counted(monkeypatch)
    script = [says("first, a look", STATS), says(note, made, STATS), says(ANSWER)]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    # Neither call of that message was run, the sound one beside it included.
    assert reached == [STATS]
    assert [bound for bound, _messages in model.asked] == [True, True, False]
    body = json.dumps(call(view, *STATS))
    assert model.asked[-1][1] == [
        ("system", SYSTEM),
        (
            "human",
            f"{USER}\n\n{FORCED_HEADER}\nnote: first, a look\n"
            f"{STATS[0]} {json.dumps(STATS[1])}\n-> {body}\n\n{FORCED_ASK}",
        ),
    ]
    lines = traced(tmp_path)
    assert [line["kind"] for line in lines[1:]] == ["model", "tool", "model", "model", "answer"]
    assert (lines[3]["tool_calls"], lines[4]["bound"]) == (2, False)
    assert (lines[-1]["forced"], lines[-1]["lookups"], lines[-1]["tool_errors"]) == (True, 1, 0)


def test_the_scripted_client_refuses_a_conversation_no_request_could_carry():
    # The guard the test above stands on: without it the stand-in would
    # take back a message a real client could not send.
    model = Scripted([says(ANSWER)] * 3)
    garbled = says("", ("feature_stats", {"feature": "clo\ud83dse"}))
    with pytest.raises(UnicodeEncodeError):
        model.invoke([("human", USER), garbled])
    with pytest.raises(ValueError, match="Out of range float values"):
        model.invoke([("human", USER), says("", ("feature_stats", {"feature": float("nan")}))])
    assert model.invoke([("human", USER), says("fine", STATS)]).content == ANSWER


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
    # test waits on the bound it is about. Red if the SDK renames it, on purpose.
    monkeypatch.setattr("mcp.client.session.DISCOVER_TIMEOUT_SECONDS", 0.2)
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
        "argument_bounds",
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
    # One line ending on every platform: the file is written as it reads.
    assert b"\r" not in (tmp_path / "trace.jsonl").read_bytes()
    lines = [json.loads(line) for line in raw.splitlines()]
    assert {line["kind"] for line in lines} == set(_KEYS)
    for line in lines:
        assert set(line) == _KEYS[line["kind"]], line["kind"]
    # (g) Once, first: what the model was told, as it was sent.
    opening = lines[0]
    assert [line["kind"] for line in lines].count("pack") == 1
    assert TRACE_VERSION == _TRACE_PINNED
    assert {kind.value: set(keys) for kind, keys in TRACE_KEYS.items()} == _KEYS
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
    # The digest reads the limits, and with them the paragraph.
    assert pack.digest() != Pack.of(tool_specs(), Limits(lookups=4, backtests=2)).digest()
    document = pack.document()
    # Every sentence the arm can say into a conversation is in the table the
    # digest is taken over — the two a forced ask is written out with too,
    # and how much of a model's own line that ask shows it.
    assert document["sentences"] == dict(SENTENCES)
    assert set(SENTENCES.values()) >= {
        REMAINING,
        SPENT,
        BACKTESTS_SPENT,
        NOT_SENDABLE,
        FORCED_HEADER,
        FORCED_ASK,
    }
    assert (SENTENCES["forced_note"], SENTENCES["forced_lookup"]) == (
        "note: {text}",
        "{tool} {arguments}\n-> {result}",
    )
    assert document["forced_note_chars"] == FORCED_NOTE_CAP
    # So are the bounds past which a call is answered as one that cannot be
    # sent: a model is told something else on the far side of each.
    assert document["argument_bounds"] == {
        "chars": MAX_ARGUMENT_CHARS,
        "depth": MAX_ARGUMENT_DEPTH,
        "name_chars": MAX_NAME_CHARS,
    }
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
    assert (DEFAULT_LIMITS.lookups, DEFAULT_LIMITS.backtests) == (5, 2)


def test_the_paragraph_states_facts_and_gives_no_advice():
    said = addendum(DEFAULT_LIMITS)
    for fact in (
        # The line of the system prompt the tools make untrue as written.
        "The prompt above says a rule is scored on history you cannot see",
        "A tool reads the training bars and nothing after them",
        "The validation bars stay hidden from you and from every tool",
        "the validation figures a scored rule is listed with are measured on them",
        "at most 5 tool call(s), of which at most 2 may be backtest_train call(s) that return a score",
        "a backtest_train call refused that way does not count toward the 2",
        "Every call counts toward the 5, one refused for a mistake in its arguments included",
        "The result of each call that counts ends with a line saying how many are left",
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
        server_log=log,
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
        # What it was opened with is read, not set: the trace's opening line
        # says what this search's model is told, and stays true.
        for name in ("pack", "label"):
            with pytest.raises(AttributeError):
                setattr(hypothesist, name, None)
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
    # is the file it was given, where a server with nothing to report says nothing.
    assert artifact_digest(params.db) == before
    assert log.read_text(encoding="utf-8") == ""
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


# -- the limits, further ---------------------------------------------------------------------------

REFUSED_RULE = (BACKTEST_TOOL, {"rule": {**BUY_12, "stop_loss": 0.02}})


def test_a_backtest_the_parser_refuses_is_a_call_and_not_a_backtest(view, tmp_path, monkeypatch):
    # Decided 2026-11-19: the backtest limit is on training scores seen, and
    # a rule the parser refused showed none. It still counts toward the five.
    reached = _counted(monkeypatch)
    script = [
        says("a slip", REFUSED_RULE),
        says("another", REFUSED_RULE),
        says("first", BACKTEST),
        says("second", BACKTEST),
        says("third", BACKTEST),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [REFUSED_RULE, REFUSED_RULE, BACKTEST, BACKTEST]
    assert [message["content"] for message in results(model)] == [
        f"{_refusal(view, *REFUSED_RULE)}\n{left(4, 2)}",
        f"{_refusal(view, *REFUSED_RULE)}\n{left(3, 2)}",
        answered(view, *BACKTEST, 2, 1),
        answered(view, *BACKTEST, 1, 0),
        f"{BACKTESTS_SPENT}\n{left(0, 0)}",
    ]
    answer = traced(tmp_path, "answer")[0]
    assert (answer["lookups"], answer["backtests"], answer["tool_errors"]) == (5, 2, 2)


def test_what_the_arm_counts_is_what_its_trace_is_read_back_as(view, tmp_path):
    # The bench reads a finished search's trace back and re-derives each
    # round's counts from the round's own lines (``tooluse.trace_use``),
    # refusing an answer line that says otherwise. That is a second
    # statement of this arm's counting rules, so one round here walks the
    # ways a call ends that the two could count differently — a backtest
    # that scores, a mistake, a call past either limit — and the reader has
    # to come to the arm's own counts: were one of those rules to change on
    # one side alone, a paid search that met it would stop at its trace. (A
    # look-up that is no backtest and simply answers is not in this round;
    # the reader's own suite, ``test_tooluse.py``, holds that one.)
    unsendable = ("feature_stats", ["close"])  # arguments that are no object
    script = [
        # A backtest that scores; one the parser refuses (a call and a
        # mistake, no backtest); a call that cannot be sent (the same); a
        # second that scores; a third, past the backtest limit (a call, and
        # neither); and a sixth call, past the round's limit (not counted).
        says("six at once", BACKTEST, REFUSED_RULE, unsendable, BACKTEST, BACKTEST, STATS),
        # Told none is left, it calls again: asked once more with no tools.
        says("one more", STATS),
        says(ANSWER),
        # The next round asks for nothing and answers.
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path) as (hypothesist, _model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    answers = traced(tmp_path, "answer")
    assert [(a["lookups"], a["backtests"], a["tool_errors"], a["forced"]) for a in answers] == [
        (5, 2, 2, True),
        (0, 0, 0, False),
    ]
    pack, use = trace_use(read_trace(tmp_path / "trace.jsonl"), replicate=0)
    assert use.rounds == (RoundUse(5, 2, 2, True), RoundUse(0, 0, 0, False))
    assert (use.model_calls, use.seam_failed) == (4, False)
    assert (pack.model, pack.limits, pack.pack_version) == (LABEL, DEFAULT_LIMITS, PACK_VERSION)
    assert pack.pack_digest == hypothesist.pack.digest()


def test_what_keeps_a_call_from_being_run_is_asked_in_one_order(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    unparsed = {"name": BACKTEST_TOOL, "args": "{", "id": "call_cut", "error": "x"}
    # One call in the round and no backtest: the smallest limits there are.
    burst = says("", BACKTEST, STATS, invalid=[unparsed])
    with arm(build_server(view), [burst, says(ANSWER)], tmp_path, limits=Limits(1, 0)) as (
        hypothesist,
        model,
    ):
        hypothesist.propose(SYSTEM, USER)
    # The backtest took the round's one call and met its own limit; after it
    # nothing is left, and that is said before anything else about a call.
    assert [message["content"] for message in results(model)] == [
        f"{BACKTESTS_SPENT}\n{left(0, 0)}",
        SPENT,
        SPENT,
    ]
    assert [line["limit"] for line in traced(tmp_path, "tool")] == [
        "backtests",
        "lookups",
        "lookups",
    ]
    # With a call left, arguments that cannot be sent are said before the
    # backtest limit: there is no backtest to be out of.
    second = tmp_path / "second"
    second.mkdir()
    script = [says("", invalid=[{**unparsed, "id": "call_cut_2"}]), says(ANSWER)]
    with arm(build_server(view), script, second, limits=Limits(2, 0)) as (hypothesist, model):
        hypothesist.propose(SYSTEM, USER)
    assert results(model)[0]["content"] == f"{NOT_SENDABLE}\n{left(1, 0)}"
    assert reached == []


# -- the forced ask, further ------------------------------------------------------------------------


def _forced_human(model) -> str:
    bound, (system, human) = model.asked[-1]
    assert not bound and system == ("system", SYSTEM)
    return human[1]


def test_a_forced_ask_after_nothing_was_looked_up_lists_nothing(view, tmp_path):
    # No backtest allowed and nothing else asked: the arm refused every
    # call itself, so there is nothing of the training bars to write out.
    script = [says("   ", BACKTEST), says("", BACKTEST), says("", BACKTEST), says(ANSWER)]
    with arm(build_server(view), script, tmp_path, limits=Limits(1, 0)) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert _forced_human(model) == f"{USER}\n\n{FORCED_ASK}"


def test_the_lines_a_model_wrote_are_shown_back_stripped_and_cut(view, tmp_path):
    long = "x" * (FORCED_NOTE_CAP + 500)
    script = [
        says(f"  {long}  ", STATS),
        says("  padded  ", STATS),  # told the round's one call is spent
        says("dropped with the conversation it ended", STATS),
        says(ANSWER),
    ]
    with arm(build_server(view), script, tmp_path, limits=Limits(1, 0)) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    body = json.dumps(call(view, *STATS))
    assert _forced_human(model) == (
        f"{USER}\n\n{FORCED_HEADER}\n"
        f"note: {long[:FORCED_NOTE_CAP]}\n"
        f"{STATS[0]} {json.dumps(STATS[1])}\n-> {body}\n"
        f"note: padded\n\n{FORCED_ASK}"
    )


def test_a_reply_that_asks_for_a_tool_when_none_is_bound_is_no_answer(view, tmp_path):
    # Asked without tools and answering with a call: the client still has
    # them. Its empty text is not handed to the loop as the round's answer.
    script = [says("", STATS), says("", STATS), says("", STATS), says("", STATS)]
    with arm(build_server(view), script, tmp_path, limits=Limits(1, 0)) as (hypothesist, model):
        with pytest.raises(HypothesistError, match="asked for a tool when it was given none") as no:
            hypothesist.propose(SYSTEM, USER)
        assert not isinstance(no.value, ToolServerError)
    assert [bound for bound, _messages in model.asked] == [True, True, True, False]
    assert traced(tmp_path, "answer") == []
    failure = traced(tmp_path)[-1]
    assert (failure["kind"], failure["side"], failure["error"]) == (
        "failed",
        "model",
        "HypothesistError",
    )


# -- arguments that cannot be sent -------------------------------------------------------------------


def _nested(depth: int) -> dict:
    """``{"feature": "close"}`` inside ``depth`` more objects: ``depth + 1`` levels in all."""
    arguments: dict = {"feature": "close"}
    for _ in range(depth):
        arguments = {"x": arguments}
    return arguments


def _listed_deep(depth: int) -> list:
    """``"close"`` inside ``depth`` lists, for a ``depth`` of one or more."""
    value: list = ["close"]
    for _ in range(depth - 1):
        value = [value]
    return value


@pytest.mark.parametrize(
    "arguments",
    [
        # One level past the bound, in objects and in lists; one character
        # past it, as the characters a model typed and as the longer
        # spelling a trace line writes them in.
        _nested(MAX_ARGUMENT_DEPTH),
        {"feature": _listed_deep(MAX_ARGUMENT_DEPTH)},
        {"feature": "c" * (MAX_ARGUMENT_CHARS - len('{"feature": ""}') + 1)},
        {"feature": "漲" * (MAX_ARGUMENT_CHARS // 6)},
        ["feature", "close"],
        {"feature": ("close",)},
        {7: "close"},
    ],
    ids=[
        "nested too deep",
        "lists too deep",
        "too long",
        "too long as written",
        "a list",
        "a tuple inside",
        "a key that is no text",
    ],
)
def test_arguments_that_cannot_be_sent_are_the_model_s_mistake_and_end_no_search(
    view, tmp_path, monkeypatch, arguments
):
    # Past bounds no call a model means comes near, or not what JSON says
    # they are: answered by the arm, and never handed to the server.
    reached = _counted(monkeypatch)
    script = [says("", ("feature_stats", arguments)), says("", STATS), says(ANSWER)]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == [STATS]
    first, second = results(model)
    assert first["content"] == f"{NOT_SENDABLE}\n{left(4, 2)}" and first["status"] == "error"
    assert second["content"] == answered(view, *STATS, 3, 2)
    mistake = traced(tmp_path, "tool")[0]
    assert (mistake["arguments"], mistake["sent"], mistake["tool_error"]) == (None, False, True)
    # The line it is written on is short, whatever was sent.
    assert max(len(line) for line in trace_text(tmp_path).splitlines()[1:]) < 2 * RESULT_CAP


def test_arguments_at_the_bounds_are_sent(view, tmp_path, monkeypatch):
    reached = _counted(monkeypatch)
    # As deep, as long and as long-named as a call may be: each one level
    # or one character short of what the test above sees refused.
    deep = ("feature_stats", _nested(MAX_ARGUMENT_DEPTH - 1))
    lists = ("feature_stats", {"feature": _listed_deep(MAX_ARGUMENT_DEPTH - 1)})
    long = ("feature_stats", {"feature": "c" * (MAX_ARGUMENT_CHARS - len('{"feature": ""}'))})
    named = ("n" * MAX_NAME_CHARS, {"feature": "close"})
    calls = [deep, lists, long, named]
    with arm(build_server(view), [says("", *calls), says(ANSWER)], tmp_path) as (
        hypothesist,
        model,
    ):
        hypothesist.propose(SYSTEM, USER)
    # The server's to refuse, in the look-up's own sentence: not the arm's.
    assert reached == calls
    assert [message["content"].splitlines()[0] for message in results(model)] == [
        _refusal(view, *made) for made in calls
    ]


def test_a_tool_name_longer_than_any_tool_s_is_not_sent_and_is_cut_where_it_is_written(
    view, tmp_path, monkeypatch
):
    reached = _counted(monkeypatch)
    script = [says("", ("x" * 500, {"feature": "close"})), says(ANSWER)]
    with arm(build_server(view), script, tmp_path) as (hypothesist, model):
        assert hypothesist.propose(SYSTEM, USER) == ANSWER
    assert reached == []
    # The one sentence names both halves of a call: here it is the name.
    assert "its tool name or its arguments" in NOT_SENDABLE
    assert [message["content"] for message in results(model)] == [f"{NOT_SENDABLE}\n{left(4, 2)}"]
    assert [line["tool"] for line in traced(tmp_path, "tool")] == ["x" * MAX_NAME_CHARS]
    # A name that is no text a wire carries is read with the half pair
    # replaced, so that it can be written down at all.
    garbled = SimpleNamespace(
        content="", tool_calls=[{"name": "feature_stats\ud83d", "args": {}, "id": "call_g"}]
    )
    read = read_reply(garbled, LABEL)
    assert read.calls == (ToolCall(id="call_g", name="feature_stats?", arguments=None),)
    assert read.replayable is False
    # An id is sent back too, twice: with the reply and with its result.
    sound = {"name": "feature_stats", "args": {"feature": "close"}}
    for call_id, replayable in (("call_ok", True), ("call_" + chr(0xD83D), False)):
        reply = SimpleNamespace(content="", tool_calls=[{**sound, "id": call_id}])
        assert read_reply(reply, LABEL).replayable is replayable


def test_a_call_built_by_hand_is_held_to_what_one_read_is():
    assert ToolCall(id="a", name="n" * MAX_NAME_CHARS, arguments=None).arguments is None
    for fields, sentence in (
        ({"id": "", "name": "t", "arguments": {}}, "answered by its id"),
        ({"id": 5, "name": "t", "arguments": {}}, "answered by its id"),
        ({"id": "a", "name": "n" * (MAX_NAME_CHARS + 1), "arguments": None}, "at most 64"),
        ({"id": "a", "name": None, "arguments": None}, "at most 64"),
        ({"id": "a", "name": "t", "arguments": ["close"]}, "an object or nothing, got list"),
    ):
        with pytest.raises(ValueError, match=sentence):
            ToolCall(**fields)


def test_the_bound_on_a_call_s_length_is_the_loop_s_bound_on_an_answer():
    # A rule too long to be proposed is not worth looking up: the two
    # bounds are one number, and this is what holds them together.
    assert MAX_ARGUMENT_CHARS == MAX_RESPONSE_CHARS


def test_a_long_result_is_cut_in_the_trace_and_not_in_the_conversation(view, tmp_path):
    # A refusal that quotes a long name back: the model reads all of it,
    # the trace keeps the head and the length.
    long = ("feature_stats", {"feature": "f" * (RESULT_CAP + 100)})
    with arm(build_server(view), [says("", long), says(ANSWER)], tmp_path) as (hypothesist, model):
        hypothesist.propose(SYSTEM, USER)
    content = results(model)[0]["content"]
    line = traced(tmp_path, "tool")[0]
    assert len(content) > RESULT_CAP
    assert line["result"] == content[:RESULT_CAP] and line["result_chars"] == len(content)


# -- how it is started, further ---------------------------------------------------------------------


def test_a_server_under_another_name_or_listing_other_tools_is_refused(view, tmp_path, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(tools_server, "SERVER_NAME", "another-server")
        _refused(
            view, tmp_path, "the server answered as 'another-server'", server=build_server(view)
        )
    # The right name and version, and other words: what a model would read
    # is then not the pack the suite pins, whatever the version says.
    served = build_server(view)
    stats, *rest = tool_specs()
    reworded = replace(stats, description=f"{stats.description} And more.")
    monkeypatch.setattr(toolarm, "tool_specs", lambda: (reworded, *rest))
    _refused(view, tmp_path, "lists tools other than this build's", server=served)
    assert list(tmp_path.iterdir()) == []


def test_a_server_that_is_a_path_or_a_child_with_no_log_is_refused(view, params, tmp_path):
    _refused(view, tmp_path, "server: expected the parameters", server="not-a-server")
    _refused(view, tmp_path, "server: expected the parameters", server=tmp_path / "arm.sqlite")
    _refused(view, tmp_path, "server: expected the parameters", server=b"arm.sqlite")
    # A child's stderr is the one place it says why: it is not left to
    # whatever this process's stderr happens to be.
    _refused(view, tmp_path, "a server started as a child has to be given one", server=params)
    assert list(tmp_path.iterdir()) == []


def _with_function(tool: dict, **changes) -> dict:
    """``tool`` with keys of its function set, or — given ``None`` — taken away."""
    function = {**tool["function"], **changes}
    return {**tool, "function": {k: v for k, v in function.items() if v is not None}}


def test_a_trace_and_a_log_are_opened_so_as_never_to_write_over_one(
    view, params, tmp_path, monkeypatch
):
    # The check before a server is started is a courtesy; what holds the
    # rule is how the files are opened. With the check out of the way, a
    # file made in between is still not written over.
    monkeypatch.setattr(toolarm, "_require_fresh", lambda trace, server_log: None)
    chat = ChatHypothesist(model=Scripted([]), label=LABEL)
    kept = tmp_path / "kept.txt"
    kept.write_text("an earlier search's\n", encoding="utf-8")
    with pytest.raises(FileExistsError), ToolHypothesist.open(chat, build_server(view), trace=kept):
        raise AssertionError("opened over a trace that exists")
    with (
        pytest.raises(FileExistsError),
        ToolHypothesist.open(chat, params, trace=tmp_path / "trace.jsonl", server_log=kept),
    ):
        raise AssertionError("opened over a log that exists")
    assert kept.read_text(encoding="utf-8") == "an earlier search's\n"
    assert not (tmp_path / "trace.jsonl").exists()


def test_a_pack_built_by_hand_is_held_to_what_one_listed_is():
    pack = Pack.of(tool_specs(), DEFAULT_LIMITS)
    tools = list(pack.tools)
    assert Pack(tools_json=json.dumps(tools, sort_keys=True), limits=DEFAULT_LIMITS) == pack
    for tools_json, sentence in (
        ("{", "pack.tools_json: not JSON"),
        (None, "pack.tools_json: not JSON"),
        ("{}", "expected a list of tools, got dict"),
        ("[]", "the tools are [], not the look-ups"),
        (json.dumps(tools[:2], sort_keys=True), "not the look-ups"),
        (json.dumps([*tools[:2], {**tools[2], "strict": True}], sort_keys=True), "a tool is"),
        (json.dumps([*tools[:2], {"type": "tool", "function": tools[2]["function"]}]), "a tool is"),
        (json.dumps([*tools[:2], "backtest_train"]), "a tool is"),
        (json.dumps([*tools[:2], {"type": "function", "function": None}]), "a tool is"),
        (json.dumps([*tools[:2], _with_function(tools[2], strict=True)]), "a tool is"),
        (json.dumps([*tools[:2], _with_function(tools[2], description=None)]), "a tool is"),
        (json.dumps([*tools[:2], _with_function(tools[2], description=5)]), "a tool is"),
        (json.dumps([*tools[:2], _with_function(tools[2], parameters=[])]), "a tool is"),
        (json.dumps(tools[::-1], sort_keys=True), "not the look-ups"),
        (json.dumps(tools, sort_keys=True, indent=1), "not the canonical spelling"),
    ):
        with pytest.raises(ToolArmError, match=re.escape(sentence)):
            Pack(tools_json=tools_json, limits=DEFAULT_LIMITS)
    with pytest.raises(ToolArmError, match="pack.limits: expected Limits"):
        replace(pack, limits=(5, 2))
    # What it hands out is the caller's own: changing it changes no pack.
    pack.tools[0]["function"]["name"] = "renamed"
    assert pack.tools[0]["function"]["name"] == "feature_stats"
    assert pack.digest() == Pack.of(tool_specs(), DEFAULT_LIMITS).digest()
    # And the tables it is written from are not an importer's to change.
    with pytest.raises(TypeError):
        SENTENCES["spent"] = "Carry on."
    with pytest.raises(TypeError):
        TRACE_KEYS[TraceKind.ANSWER] = ("kind",)


# -- the small parts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # Arguments that are no object.
        (
            SimpleNamespace(content="", tool_calls=[{"name": "t", "args": "close", "id": "a"}]),
            (ToolCall(id="a", name="t", arguments=None), None, None),
        ),
        # A call the client could not parse, and could not name either.
        (
            SimpleNamespace(content="", invalid_tool_calls=[{"args": "{", "id": "b"}]),
            (ToolCall(id="b", name="", arguments=None), None, None),
        ),
        # The lists as tuples; token counts that are counts, and ones that are not.
        (
            SimpleNamespace(
                content="",
                tool_calls=({"name": "t", "args": {}, "id": "c"},),
                usage_metadata={"input_tokens": 7, "output_tokens": True},
            ),
            (ToolCall(id="c", name="t", arguments={}), 7, None),
        ),
        (
            SimpleNamespace(
                content="",
                tool_calls=[{"name": "t", "args": {"k": 1}, "id": "d"}],
                usage_metadata={"input_tokens": "7", "output_tokens": 2.0},
            ),
            (ToolCall(id="d", name="t", arguments={"k": 1}), None, None),
        ),
    ],
)
def test_a_reply_is_read_field_by_field(reply, expected):
    call_, input_tokens, output_tokens = expected
    read = read_reply(reply, LABEL)
    assert read.calls == (call_,)
    assert (read.input_tokens, read.output_tokens) == (input_tokens, output_tokens)
    assert read.message is reply


@pytest.mark.parametrize("call_id", ["", 5, None])
def test_a_call_whose_id_is_no_text_cannot_be_answered(call_id):
    reply = SimpleNamespace(content="", tool_calls=[{"name": "t", "args": {}, "id": call_id}])
    with pytest.raises(HypothesistError, match="a tool call with no id"):
        read_reply(reply, LABEL)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


@pytest.mark.parametrize(
    ("content", "is_error"),
    [
        ([SimpleNamespace(type="image", data="...")], False),
        ([], False),
        ([_text("a"), _text("b")], False),
        ([_text("  ")], False),
        ([_text("{}")], None),
    ],
    ids=["no text", "nothing", "two texts", "blank text", "no verdict"],
)
def test_an_answer_in_a_shape_the_arm_does_not_read_is_a_failed_seam(content, is_error):
    class Client:
        async def call_tool(self, name, arguments, timeout):
            return SimpleNamespace(content=content, is_error=is_error)

    with start_blocking_portal() as portal:
        session = toolarm._Session(portal, Client(), 5.0)
        with pytest.raises(ToolServerError, match="in a shape this arm does not read"):
            session.call("feature_stats", {"feature": "close"})


def test_a_server_that_does_not_list_its_tools_is_a_failed_seam():
    class Client:
        async def list_tools(self):
            raise RuntimeError("the listing went away")

        async def call_tool(self, name, arguments, timeout):
            return SimpleNamespace(content=[_text('{"bars": 3}')], is_error=True)

    with start_blocking_portal() as portal:
        session = toolarm._Session(portal, Client(), 5.0)
        sentence = "did not list its tools: RuntimeError: the listing went away"
        with pytest.raises(ToolServerError, match=re.escape(sentence)):
            session.listing()
        # The one block of text, and the server's own verdict on the call.
        assert session.call("feature_stats", {}) == ('{"bars": 3}', True)


def test_a_wait_on_the_server_is_taken_in_slices_and_ends_as_the_task_does(monkeypatch):
    # A stand-in portal handing out futures the test settles itself, and a
    # ``wait`` that keeps the bound each slice was waited under. A future
    # read before it is settled says so: a wait that went around ``wait``
    # fails here, where it would otherwise sit on a result that never comes.
    class Unsettled(Future):
        def result(self, timeout=None):
            if not self.done():
                raise AssertionError("the result was read before the task was done")
            return super().result(timeout)

    futures: list[Future] = []

    def start_task_soon(_function, *_arguments):
        futures.append(Unsettled())
        return futures[-1]

    portal = SimpleNamespace(start_task_soon=start_task_soon)
    slices: list[float] = []

    def settling_after(count: int, settle):
        def waited(waiting, timeout):
            slices.append(timeout)
            if len(slices) > 50:
                raise AssertionError("the wait spun")
            if len(slices) == count:
                settle(futures[-1])
            return real_wait(waiting, timeout=0)

        return waited

    def interrupted(_waiting, timeout):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        # Three slices, none longer than the bound, then the task's result.
        patched.setattr(toolarm, "wait", settling_after(3, lambda f: f.set_result("done")))
        assert toolarm._waited(portal, object) == "done"
        assert slices == [toolarm._POLL_S] * 3

        # A timeout that IS the task's result is raised as that, once the
        # task is done: not taken for a slice running out, not waited through.
        slices.clear()
        own = TimeoutError("the look-up's own")
        patched.setattr(toolarm, "wait", settling_after(2, lambda f: f.set_exception(own)))
        with pytest.raises(TimeoutError, match="the look-up's own"):
            toolarm._waited(portal, object)
        assert len(slices) == 2

        # An interrupt between slices is heard, and takes the task with it.
        patched.setattr(toolarm, "wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            toolarm._waited(portal, object)
        assert futures[-1].cancelled()

    # And on a real loop: a task longer than a slice is waited for.
    monkeypatch.setattr(toolarm, "_POLL_S", 0.02)

    async def slow():
        await anyio.sleep(0.1)
        return "done"

    with start_blocking_portal() as real:
        assert toolarm._waited(real, slow) == "done"


def test_what_went_wrong_is_read_out_of_the_groups_it_is_raised_in():
    class Group(Exception):
        """Raised as the SDK's task groups raise: the failures are its ``exceptions``."""

        def __init__(self, *leaves):
            super().__init__("unhandled errors in a TaskGroup")
            self.exceptions = list(leaves)

    group = Group(ValueError("closed"), Group(ValueError("closed"), KeyError("k" * 500)))
    said = toolarm._described(group)
    first, second = said.split("; ")
    # Each leaf once, each cut to a line: a long first one cannot push the next out.
    assert first == "ValueError: closed" and second.startswith("KeyError: 'kkk")
    assert len(second) == 200
    # Something that only looks like a group is described as itself.
    odd = RuntimeError("odd")
    odd.exceptions = 7
    assert toolarm._described(odd) == "RuntimeError: odd"
    assert toolarm._described(Group(RuntimeError("x"))) == "RuntimeError: x"
    # A group with nothing in it still says what it is.
    assert toolarm._described(Group()) == "Group: unhandled errors in a TaskGroup"


def test_closing_a_connection_that_is_already_gone_is_quiet_and_said(caplog):
    class Manager:
        def __enter__(self):
            return "the client"

        def __exit__(self, *exc):
            raise RuntimeError("the pipe is closed")

    portal = SimpleNamespace(wrap_async_context_manager=lambda _client: Manager())
    with (
        caplog.at_level("WARNING", logger=toolarm.__name__),
        toolarm._connected(portal, object(), 1.0) as client,
    ):
        assert client == "the client"
    assert "closing the look-ups' server: RuntimeError: the pipe is closed" in caplog.text


def test_a_trace_line_that_is_not_its_kind_s_shape_is_a_defect_and_takes_no_number():
    out = io.StringIO()
    trace, state = toolarm._Trace(out), toolarm._Round(number=1)
    with pytest.raises(RuntimeError, match="a 'answer' trace line carries"):
        trace.write(TraceKind.ANSWER, state, forced=False)
    assert out.getvalue() == "" and state.seq == 0
    # A key too many is refused as a key too few is.
    with pytest.raises(RuntimeError, match="a 'failed' trace line carries"):
        trace.write(TraceKind.FAILED, state, side="model", error="RuntimeError", sentence="x")
    assert out.getvalue() == "" and state.seq == 0
    trace.write(TraceKind.FAILED, state, side="model", error="RuntimeError")
    assert json.loads(out.getvalue()) == {
        "kind": "failed",
        "round": 1,
        "seq": 1,
        "side": "model",
        "error": "RuntimeError",
    }
    # The vocabularies a line is written in.
    assert [kind.value for kind in TraceKind] == ["pack", "model", "tool", "answer", "failed"]
    assert [limit.value for limit in toolarm.Limit] == ["lookups", "backtests"]
    assert [seam.value for seam in toolarm.Seam] == ["model", "server"]


def test_a_long_answer_is_returned_whole_and_cut_where_it_is_written(view, tmp_path):
    # The loop is handed the answer as the model gave it (what to make of
    # an essay is the loop's rule) and the trace keeps its head and length.
    essay = "not a rule. " * 400
    with arm(build_server(view), [says(essay)], tmp_path) as (hypothesist, _model):
        assert hypothesist.propose(SYSTEM, USER) == essay
    answer = traced(tmp_path, "answer")[0]
    assert answer["text"] == essay[:NOTE_CAP] and answer["text_chars"] == len(essay)
