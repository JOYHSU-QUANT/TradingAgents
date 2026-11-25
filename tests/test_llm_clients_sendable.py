"""What a chat model is sent holds no lone surrogate (issue #441).

A ``str`` can hold a code point from U+D800 to U+DFFF on its own. A model's
reply gets one when its provider's JSON carries half of an escaped pair and
the client decodes it; a later node's prompt then holds that reply, and the
request for it cannot be built. ``sendable`` is what every client's
``invoke`` passes its input through.

The characters are built with ``chr`` here and never written out, as an
escape or as themselves: a tool that rewrites one into the other would turn
this file into one that cannot be saved as UTF-8.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompt_values import ChatPromptValue, StringPromptValue

from tradingagents.llm_clients import (
    anthropic_client,
    azure_client,
    base_client,
    bedrock_client,
    create_llm_client,
    google_client,
    openai_client,
)
from tradingagents.llm_clients.base_client import sendable

LONE = chr(0xD83D)
OTHER_HALF = chr(0xDE00)
REPLACED = chr(0xFFFD)
SAID = (
    "{} lone surrogate(s) in a model request were replaced with U+FFFD before "
    "sending: none can be written as UTF-8 (text a graph keeps is counted by each request quoting it)"
)


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == base_client.logger.name and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    "clean",
    [
        "a prompt",
        "an emoji that is a whole pair: " + chr(0x1F600),
        "the code points on either side of the range: " + chr(0xD7FF) + chr(0xE000),
        [SystemMessage("system"), HumanMessage("human"), AIMessage("ai")],
        [("system", "system"), ("human", "human")],
        [{"role": "user", "content": "hello"}],
        ChatPromptValue(messages=[HumanMessage("human")]),
        StringPromptValue(text="text"),
        [AIMessage("", tool_calls=[{"name": "tool", "args": {"key": ["value"]}, "id": "1"}])],
        [HumanMessage(content=[{"type": "text", "text": "a block"}])],
    ],
)
def test_an_input_holding_none_comes_back_as_the_same_object_and_nothing_is_said(clean, caplog):
    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        assert sendable(clean) is clean
    assert _warnings(caplog) == []


def test_each_one_in_a_text_becomes_one_replacement_character_and_the_count_is_said(caplog):
    text = f"a{LONE}b{OTHER_HALF}{LONE}c"
    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        assert sendable(text) == f"a{REPLACED}b{REPLACED}{REPLACED}c"
    assert _warnings(caplog) == [SAID.format(3)]


@pytest.mark.parametrize("code", [0xD800, 0xDBFF, 0xDC00, 0xDFFF])
def test_the_first_and_the_last_of_each_half_of_the_range_are_replaced(code):
    assert sendable(f"a{chr(code)}b") == f"a{REPLACED}b"


def test_every_place_a_message_carries_text_is_read_and_the_rest_is_left_as_it_was(caplog):
    untouched = SystemMessage("system")
    human = HumanMessage(f"human {LONE}")
    blocks = HumanMessage(content=[{"type": "text", "text": f"block {LONE}"}, {"type": "other"}])
    called = AIMessage(
        f"ai {LONE}",
        tool_calls=[{"name": "tool", "args": {f"key {LONE}": [f"value {LONE}", 3]}, "id": "1"}],
    )
    result = ToolMessage(f"result {LONE}", tool_call_id="1")
    system = SystemMessage(f"system {LONE}")
    given = [untouched, human, blocks, called, result, ("human", f"pair {LONE}"), system]

    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        sent = sendable(given)

    assert _warnings(caplog) == [SAID.format(8)]
    assert sent is not given
    assert [type(message) for message in sent] == [type(message) for message in given]
    # A message holding none is the same object: only what had to change did.
    assert sent[0] is untouched
    assert sent[1].content == f"human {REPLACED}"
    assert sent[2].content == [{"type": "text", "text": f"block {REPLACED}"}, {"type": "other"}]
    assert sent[3].content == f"ai {REPLACED}"
    assert sent[3].tool_calls == [
        {
            "name": "tool",
            "args": {f"key {REPLACED}": [f"value {REPLACED}", 3]},
            "id": "1",
            "type": "tool_call",
        }
    ]
    assert sent[4].content == f"result {REPLACED}"
    assert sent[4].tool_call_id == "1"
    assert sent[5] == ("human", f"pair {REPLACED}")
    assert sent[6].content == f"system {REPLACED}"
    # What the graph holds is not what was changed: its state still says what was written.
    assert human.content == f"human {LONE}"
    assert called.tool_calls[0]["args"] == {f"key {LONE}": [f"value {LONE}", 3]}
    assert result.content == f"result {LONE}"


def test_a_prompt_value_stays_a_prompt_value(caplog):
    chat = ChatPromptValue(messages=[SystemMessage("system"), HumanMessage(f"human {LONE}")])
    text = StringPromptValue(text=f"text {LONE}")
    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        sent_chat = sendable(chat)
        sent_text = sendable(text)
    assert type(sent_chat) is ChatPromptValue
    assert [message.content for message in sent_chat.to_messages()] == [
        "system",
        f"human {REPLACED}",
    ]
    assert type(sent_text) is StringPromptValue
    assert sent_text.to_string() == f"text {REPLACED}"
    assert _warnings(caplog) == [SAID.format(1), SAID.format(1)]


def test_what_a_provider_put_on_a_message_beside_its_fields_is_read_too(caplog):
    # A message takes attributes it does not declare, and a client can send
    # them (one of this repository's does, for a provider's reasoning text).
    message = AIMessage("clean", reasoning_note=f"note {LONE}")
    assert "reasoning_note" not in type(message).model_fields
    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        (sent,) = sendable([message])
    assert sent.reasoning_note == f"note {REPLACED}"
    assert sent.content == "clean"
    assert message.reasoning_note == f"note {LONE}"
    assert _warnings(caplog) == [SAID.format(1)]


def test_a_container_that_had_to_change_comes_back_plain_and_colliding_keys_become_one():
    # Stated in the docstring as what holds only for such a container; a
    # chat input is a plain list of messages, pairs or dicts.
    class Pair(tuple):
        pass

    assert type(sendable(Pair(("human", "clean")))) is Pair
    assert type(sendable(Pair(("human", LONE)))) is tuple
    assert sendable({f"a{LONE}": 1, f"a{OTHER_HALF}": 2}) == {f"a{REPLACED}": 2}


@pytest.mark.parametrize("other", [None, 3, 2.5, b"bytes", object()])
def test_what_is_no_text_and_holds_none_is_passed_on(other):
    assert sendable(other) is other
    assert sendable([other]) == [other]


def _over_a_wire(reply: str, form: str):
    """The repo's own OpenAI-compatible client over a transport that answers ``reply``.

    ``form`` is how the reply stands in the response body: ``"raw"`` carries
    the surrogate itself, ``"escaped"`` is an ASCII body whose JSON string
    holds its escape. The client decodes either into the same ``str``. Only
    the transport is replaced; what it received is returned beside the model.
    """
    received: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        payload = {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture/model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": reply},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if form == "raw":
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass")
        else:
            body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    llm = create_llm_client(
        provider="openrouter",
        model="fixture/model",
        http_client=httpx.Client(transport=httpx.MockTransport(answer)),
        api_key="not-a-key-for-a-fake-transport",
        max_retries=0,
    ).get_llm()
    assert type(llm) is openai_client.NormalizedChatOpenAI
    return llm, received


@pytest.mark.parametrize("form", ["raw", "escaped"])
def test_a_reply_holding_one_can_be_put_into_the_next_request(form, caplog):
    llm, received = _over_a_wire(f"Momentum {LONE} holds.", form)
    first = llm.invoke("Say something.")
    # The client decoded it: the earlier node's text, as a graph would keep it.
    assert first.content == f"Momentum {LONE} holds."

    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        llm.invoke(f"The earlier node said:\n{first.content}\nNow answer.")

    assert len(received) == 2
    sent = json.loads(received[1].decode("utf-8"))  # strict: the body is UTF-8
    assert sent["messages"][-1]["content"] == (
        f"The earlier node said:\nMomentum {REPLACED} holds.\nNow answer."
    )
    assert _warnings(caplog) == [SAID.format(1)]


def test_a_model_bound_to_tools_sends_through_the_same_replacement(caplog):
    # A node that binds tools asks through a binding around the model, not
    # the model itself: the binding's call has to reach ``invoke`` too.
    llm, received = _over_a_wire("answered", "raw")

    def get_price(ticker: str) -> str:
        """Look a price up."""
        return ticker

    with caplog.at_level(logging.WARNING, logger=base_client.logger.name):
        llm.bind_tools([get_price]).invoke(f"Price of {LONE}?")

    (body,) = received
    sent = json.loads(body.decode("utf-8"))  # strict: the body is UTF-8
    assert sent["messages"][-1]["content"] == f"Price of {REPLACED}?"
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["get_price"]
    assert _warnings(caplog) == [SAID.format(1)]


def test_without_the_replacement_that_request_is_never_built(monkeypatch):
    # Why the replacement exists: the day this stops raising, the SDK has
    # learned to send such text and ``sendable`` has nothing left to do.
    monkeypatch.setattr(openai_client, "sendable", lambda input: input)
    llm, received = _over_a_wire("unused", "raw")
    with pytest.raises(UnicodeEncodeError):
        llm.invoke(f"The earlier node said:\nMomentum {LONE} holds.\nNow answer.")
    assert received == []


def _bedrock():
    pytest.importorskip("langchain_aws")
    return bedrock_client._bedrock_class()


@pytest.mark.parametrize(
    ("module", "find"),
    [
        (openai_client, lambda: openai_client.NormalizedChatOpenAI),
        (anthropic_client, lambda: anthropic_client.NormalizedChatAnthropic),
        (google_client, lambda: google_client.NormalizedChatGoogleGenerativeAI),
        (azure_client, lambda: azure_client.NormalizedAzureChatOpenAI),
        (bedrock_client, _bedrock),
    ],
    ids=["openai", "anthropic", "google", "azure", "bedrock"],
)
def test_every_client_s_invoke_hands_its_chat_class_what_sendable_returned(
    module, find, monkeypatch
):
    # Only the OpenAI-compatible client is driven over a transport above; for
    # the others this is the whole of what is pinned: the input goes through
    # ``sendable`` before the provider's own ``invoke`` sees it.
    cls = find()
    (parent,) = cls.__bases__
    asked = []

    def invoke(self, input, config=None, **kwargs):
        asked.append((input, config, kwargs))
        return AIMessage("answered")

    monkeypatch.setattr(parent, "invoke", invoke)
    monkeypatch.setattr(module, "sendable", lambda input: ("made sendable", input))
    model = cls.__new__(cls)

    assert cls.invoke(model, "a prompt", {"tags": ["x"]}, stop=["."]).content == "answered"
    assert asked == [(("made sendable", "a prompt"), {"tags": ["x"]}, {"stop": ["."]})]
