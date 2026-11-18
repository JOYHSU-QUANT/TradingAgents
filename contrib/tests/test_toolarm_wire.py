"""What the discovery package's tool arm hands a chat client, held against the library the clients are built on.

``contrib/discovery/toolarm.py`` imports no model library (that package's
own ``tests/test_upstream.py`` holds its sources AND its tests off
``langchain``): it reads a reply by attribute, writes a tool's result as a
plain role-keyed ``dict``, and binds tools written as plain function
``dict``s. Its own suite drives it with a scripted stand-in, which would
go on passing if the library stopped reading any of those three shapes.
This file is where they meet the library itself — in ``contrib/tests``
because it belongs to no package, as ``test_package_graph.py`` does.

It needs neither the MCP SDK nor a model: the arm's three seams with the
library are plain functions (``read_reply``, ``tool_message``, ``Pack``).
"""

from __future__ import annotations

import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.utils.function_calling import convert_to_openai_tool

from contrib.discovery.toolarm import DEFAULT_LIMITS, Pack, ToolCall, read_reply, tool_message
from contrib.discovery.tools_server import tool_specs

_STATS = {
    "name": "feature_stats",
    "args": {"feature": "close"},
    "id": "call_1",
    "type": "tool_call",
}
_UNPARSED = {"name": "backtest_train", "args": '{"rule": ', "id": "call_2", "error": "truncated"}
_RULE = json.dumps({"family": "breakout"})


# What the fake below was asked with, most recent last: the messages as the
# library converted them, and what was bound.
_ASKED: list[tuple[list, dict]] = []


class _Tooled(GenericFakeChatModel):
    """The library's own fake chat model, bound as its real ones are, keeping what it was asked."""

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _ASKED.append((messages, kwargs))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_a_reply_the_library_builds_is_read_by_attribute():
    message = AIMessage(
        content="I check where the close lies.",
        tool_calls=[_STATS],
        invalid_tool_calls=[_UNPARSED],
        usage_metadata={"input_tokens": 812, "output_tokens": 41, "total_tokens": 853},
    )
    reply = read_reply(message, "fake/model")
    assert reply.message is message
    assert reply.text == "I check where the close lies."
    assert reply.calls == (
        ToolCall(id="call_1", name="feature_stats", arguments={"feature": "close"}),
        # One the client could not parse: kept, with no arguments to send.
        ToolCall(id="call_2", name="backtest_train", arguments=None),
    )
    assert (reply.input_tokens, reply.output_tokens) == (812, 41)
    # The least a reply carries: text, no calls, no usage.
    plain = read_reply(AIMessage(content=_RULE), "fake/model")
    assert (plain.text, plain.calls, plain.input_tokens, plain.output_tokens) == (
        _RULE,
        (),
        None,
        None,
    )


def test_the_conversation_the_arm_builds_is_one_the_library_reads():
    reply = AIMessage(content="note", tool_calls=[_STATS, {**_STATS, "id": "call_3"}])
    conversation = [
        ("system", "SYSTEM"),
        ("human", "USER"),
        reply,
        tool_message("call_1", '{"bars": 3}', error=False),
        tool_message("call_3", "refused", error=True),
    ]
    system, human, said, answered, refused = convert_to_messages(conversation)
    assert isinstance(system, SystemMessage) and isinstance(human, HumanMessage)
    assert said is reply
    assert isinstance(answered, ToolMessage) and isinstance(refused, ToolMessage)
    assert (answered.content, answered.tool_call_id, answered.status) == (
        '{"bars": 3}',
        "call_1",
        "success",
    )
    assert (refused.content, refused.tool_call_id, refused.status) == ("refused", "call_3", "error")


def test_the_tools_as_bound_are_in_the_form_the_library_sends():
    pack = Pack.of(tool_specs(), DEFAULT_LIMITS)
    assert len(pack.tools) == 3
    for tool in pack.tools:
        # Already the function form: the library's own conversion changes nothing.
        assert convert_to_openai_tool(tool) == tool


def test_a_chat_model_of_the_library_s_takes_a_round_as_the_arm_runs_one():
    pack = Pack.of(tool_specs(), DEFAULT_LIMITS)
    _ASKED.clear()
    model = _Tooled(
        messages=iter([AIMessage(content="note", tool_calls=[_STATS]), AIMessage(content=_RULE)])
    )
    bound = model.bind_tools(list(pack.tools))

    opening = [("system", "SYSTEM"), ("human", "USER")]
    first = read_reply(bound.invoke(opening), "fake/model")
    (call,) = first.calls
    assert call == ToolCall(id="call_1", name="feature_stats", arguments={"feature": "close"})
    second = read_reply(
        bound.invoke([*opening, first.message, tool_message(call.id, '{"bars": 3}', error=False)]),
        "fake/model",
    )
    assert second.text == _RULE and second.calls == ()

    (_first_messages, first_kwargs), (messages, _kwargs) = _ASKED
    assert first_kwargs["tools"] == list(pack.tools)
    assert [type(message) for message in messages] == [
        SystemMessage,
        HumanMessage,
        AIMessage,
        ToolMessage,
    ]
    assert messages[2].tool_calls[0]["id"] == messages[3].tool_call_id == "call_1"
