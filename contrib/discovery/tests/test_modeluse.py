"""Tests for what a blind model arm spent (issue #433): the counter, the record, the lines.

The bench's side of it — a search writes the record, a reader holds it to
the verdicts, a directory searched before the record existed still reads —
is in ``test_bench.py``; this file is the module on its own.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from contrib.autoresearch.ports import HypothesistError
from contrib.discovery.modeluse import (
    MODEL_USE_FILENAME,
    MODEL_USE_VERSION,
    MeteredModel,
    ModelSearch,
    ModelUse,
    ModelUseError,
    model_use_lines,
)
from contrib.discovery.tooluse import Prices, RoundUse, SearchUse, usage_lines
from contrib.discovery.upstream import ChatHypothesist

DIGEST = "ab" * 4  # a manifest digest's eight hex digits
MODEL = "openrouter/some-model"
SPENT = ModelSearch(
    replicate=0,
    seam_failed=False,
    model_calls=30,
    unmetered_calls=0,
    input_tokens=1_000_000,
    output_tokens=200_000,
)
DEAD = ModelSearch(
    replicate=1,
    seam_failed=True,
    model_calls=0,
    unmetered_calls=0,
    input_tokens=0,
    output_tokens=0,
)


def _reply(text="{}", **usage):
    return SimpleNamespace(content=text, usage_metadata=usage or None)


class _Client:
    """A chat client that hands back what it was given to, in order, and keeps what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked = []

    def invoke(self, messages):
        self.asked.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# -- the counter ----------------------------------------------------------------------------------


def test_a_reply_that_is_text_is_counted_with_what_it_reported_and_handed_on_as_it_came():
    first = _reply(input_tokens=1200, output_tokens=80)
    second = _reply(input_tokens=1500, output_tokens=95)
    client = _Client([first, second])
    meter = MeteredModel(client)
    messages = [("system", "s"), ("human", "u")]
    assert meter.invoke(messages) is first  # the reply itself, not a copy
    assert meter.invoke(messages) is second
    assert client.asked == [messages, messages]  # and the client was asked what the adapter asked
    assert meter.spent(3, seam_failed=False) == ModelSearch(
        replicate=3,
        seam_failed=False,
        model_calls=2,
        unmetered_calls=0,
        input_tokens=2700,
        output_tokens=175,
    )


@pytest.mark.parametrize(
    ("usage", "kept"),
    [
        (None, (0, 0)),
        ("1200 in", (0, 0)),
        ({}, (0, 0)),
        ({"input_tokens": 1200}, (1200, 0)),
        ({"output_tokens": 80}, (0, 80)),
        ({"input_tokens": 1200, "output_tokens": None}, (1200, 0)),
        ({"input_tokens": True, "output_tokens": 80}, (0, 80)),
        ({"input_tokens": -5, "output_tokens": 80}, (0, 80)),
        ({"input_tokens": 12.0, "output_tokens": 80}, (0, 80)),
        ({"input_tokens": 10**13, "output_tokens": 80}, (0, 80)),
    ],
    ids=[
        "none",
        "not-a-mapping",
        "empty",
        "in-only",
        "out-only",
        "null",
        "bool",
        "negative",
        "float",
        "absurd",
    ],
)
def test_a_call_short_of_a_count_is_unmetered_and_what_it_did_report_is_kept(usage, kept):
    meter = MeteredModel(_Client([SimpleNamespace(content="{}", usage_metadata=usage)]))
    meter.invoke([])
    spent = meter.spent(0, seam_failed=False)
    assert (spent.model_calls, spent.unmetered_calls) == (1, 1)
    assert (spent.input_tokens, spent.output_tokens) == kept


def test_a_reply_with_no_usage_attribute_at_all_is_counted_and_unmetered():
    meter = MeteredModel(_Client([SimpleNamespace(content="{}")]))
    meter.invoke([])
    spent = meter.spent(0, seam_failed=False)
    assert (spent.model_calls, spent.unmetered_calls) == (1, 1)


@pytest.mark.parametrize(
    "content", [None, [{"type": "text", "text": "{}"}], 7], ids=["none", "blocks", "number"]
)
def test_a_reply_that_is_not_text_is_handed_on_and_counted_nowhere(content):
    # The adapter refuses it as the seam's failure; the tool arm's trace
    # has no model line for such a reply either, so the two floors are one
    # kind.
    reply = SimpleNamespace(
        content=content, usage_metadata={"input_tokens": 900, "output_tokens": 40}
    )
    meter = MeteredModel(_Client([reply]))
    assert meter.invoke([]) is reply
    assert meter.spent(0, seam_failed=True) == replace(DEAD, replicate=0)


def test_a_call_that_raises_is_let_through_and_counted_nowhere():
    meter = MeteredModel(_Client([RuntimeError("the provider is down"), _reply()]))
    with pytest.raises(RuntimeError, match="the provider is down"):
        meter.invoke([])
    meter.invoke([])
    assert meter.spent(0, seam_failed=False).model_calls == 1


def test_a_call_the_client_retried_is_counted_once():
    class _Retrying(_Client):
        """A client that, as the real ones do, retries inside one ``invoke``."""

        def invoke(self, messages):
            while True:
                try:
                    return super().invoke(messages)
                except TimeoutError:
                    continue

    client = _Retrying(
        [TimeoutError(), TimeoutError(), _reply(input_tokens=1200, output_tokens=80)]
    )
    meter = MeteredModel(client)
    meter.invoke([])
    assert len(client.asked) == 3  # the provider was asked three times, and paid for some of it
    spent = meter.spent(0, seam_failed=False)
    assert (spent.model_calls, spent.input_tokens, spent.output_tokens) == (1, 1200, 80)


def test_counts_that_cannot_be_read_leave_the_call_unmetered_and_never_fail_it():
    class _Usage(dict):
        def get(self, key, default=None):
            raise RuntimeError("usage is parsed lazily, and broke")

    class _Lazy:
        content = "{}"

        @property
        def usage_metadata(self):
            raise RuntimeError("usage is parsed lazily, and broke")

    for make in (lambda: SimpleNamespace(content="{}", usage_metadata=_Usage()), _Lazy):
        reply = make()
        meter = MeteredModel(_Client([reply]))
        assert meter.invoke([]) is reply
        spent = meter.spent(0, seam_failed=False)
        assert (spent.model_calls, spent.unmetered_calls) == (1, 1)
        assert (spent.input_tokens, spent.output_tokens) == (0, 0)
        # Raised from the counter it would have reached the adapter as the
        # provider not answering; the adapter has its answer.
        chat = ChatHypothesist(model=MeteredModel(_Client([make()])), label=MODEL)
        assert chat.propose("s", "u") == "{}"


def test_a_reply_whose_content_cannot_be_read_is_counted_nowhere_and_fails_as_it_did():
    class _Broken:
        @property
        def content(self):
            raise RuntimeError("content broke")

    reply = _Broken()
    meter = MeteredModel(_Client([reply]))
    assert meter.invoke([]) is reply
    assert meter.spent(0, seam_failed=True).model_calls == 0
    # With a counter and without one, the adapter meets the same error.
    for model in (_Client([_Broken()]), MeteredModel(_Client([_Broken()]))):
        with pytest.raises(RuntimeError, match="content broke"):
            ChatHypothesist(model=model, label=MODEL).propose("s", "u")


def test_the_adapter_answers_through_a_counter_as_it_does_without_one():
    # ``ChatHypothesist`` itself is not changed (the issue's condition):
    # the same text comes back, and the same refusal for what is not text.
    replies = ['{"family": "a"}', "not a rule"]

    def chat(counted: bool):
        client = _Client([_reply(text) for text in replies] + [_reply(text=None)])
        model = MeteredModel(client) if counted else client
        return ChatHypothesist(model=model, label=MODEL), client

    plain, plain_client = chat(counted=False)
    counted, counted_client = chat(counted=True)
    for _ in replies:
        assert counted.propose("s", "u") == plain.propose("s", "u")
    assert counted_client.asked == plain_client.asked
    for adapter in (plain, counted):
        with pytest.raises(HypothesistError, match=f"{MODEL} answered with NoneType, not text"):
            adapter.propose("s", "u")
    assert counted.model.spent(0, seam_failed=True).model_calls == 2


# -- the record -------------------------------------------------------------------------------------


def _use(*searches: ModelSearch) -> ModelUse:
    return ModelUse(
        world_id="w-0001", manifest_digest=DIGEST, model=MODEL, searches=searches or (SPENT, DEAD)
    )


def test_the_record_reads_back_as_it_was_written():
    use = _use()
    assert ModelUse.loads(use.dumps()) == use
    assert json.loads(use.dumps()) == {
        "model_use_version": MODEL_USE_VERSION,
        "world_id": "w-0001",
        "manifest_digest": DIGEST,
        "model": MODEL,
        "searches": [
            {
                "replicate": 0,
                "seam_failed": False,
                "model_calls": 30,
                "unmetered_calls": 0,
                "input_tokens": 1_000_000,
                "output_tokens": 200_000,
            },
            {
                "replicate": 1,
                "seam_failed": True,
                "model_calls": 0,
                "unmetered_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        ],
    }
    assert MODEL_USE_FILENAME == "model_use.json"


@pytest.mark.parametrize(
    ("changed", "sentence"),
    [
        ({"unmetered_calls": 31}, "search.unmetered_calls: 31 of 30 model call"),
        ({"model_calls": 0, "unmetered_calls": 0}, "ended on a failed seam, and this one is said"),
        ({"model_calls": -1}, "search.model_calls"),
        ({"input_tokens": -1}, "search.input_tokens"),
        ({"output_tokens": 1.5}, "search.output_tokens"),
        ({"seam_failed": 0}, "search.seam_failed"),
        ({"replicate": -1}, "search.replicate"),
    ],
)
def test_a_search_s_figures_are_held_to_one_another(changed, sentence):
    with pytest.raises(ModelUseError, match=sentence):
        replace(SPENT, **changed)


def test_tokens_with_no_call_that_came_back_are_refused():
    with pytest.raises(ModelUseError, match="tokens are counted, and no model call came back"):
        replace(DEAD, input_tokens=5)


def test_the_record_holds_each_replicate_once_and_at_least_one():
    with pytest.raises(ModelUseError, match=r"each replicate once, got \[0, 0\]"):
        _use(SPENT, SPENT)
    with pytest.raises(ModelUseError, match="at least one ModelSearch"):
        ModelUse(world_id="w-0001", manifest_digest=DIGEST, model=MODEL, searches=())
    with pytest.raises(ModelUseError, match="manifest_digest"):
        replace(_use(), manifest_digest="short")
    with pytest.raises(ModelUseError, match="model"):
        replace(_use(), model="")


def test_a_record_of_another_shape_is_refused_by_name():
    document = _use().to_dict()
    with pytest.raises(ModelUseError, match="model_use_version"):
        ModelUse.from_dict({**document, "model_use_version": MODEL_USE_VERSION + 1})
    with pytest.raises(ModelUseError, match="rounds"):
        ModelUse.from_dict({**document, "rounds": []})
    # Which of several searches it is, is said.
    with pytest.raises(ModelUseError, match=r"searches\[1\]: "):
        ModelUse.from_dict({**document, "searches": [SPENT.to_dict(), {"replicate": 1}]})
    with pytest.raises(ModelUseError):
        ModelUse.loads("not json")


# -- what a table says of it ----------------------------------------------------------------------


def test_the_row_s_tokens_are_said_in_the_tool_arm_s_own_sentence():
    # One rule of counting, one sentence: the same figures on a tool arm's
    # search print the same line, so the two rows can be set side by side.
    tooled = [
        SearchUse(
            replicate=search.replicate,
            rounds=tuple(RoundUse(0, 0, 0, False) for _ in range(search.model_calls)),
            seam_failed=search.seam_failed,
            model_calls=search.model_calls,
            unmetered_calls=search.unmetered_calls,
            input_tokens=search.input_tokens,
            output_tokens=search.output_tokens,
        )
        for search in (SPENT, DEAD)
    ]
    for prices in (None, Prices(input=3, output=15)):
        assert model_use_lines([SPENT, DEAD], 0, prices, model=MODEL) == [
            usage_lines(tooled, prices, model=MODEL)[1]
        ]
    (line,) = model_use_lines([SPENT, DEAD], 0, Prices(input=3, output=15), model=MODEL)
    assert line == (
        f"  tokens reported by {MODEL} over 2 search(es), failed seams included: 1,000,000 in, "
        "200,000 out, in 30 model call(s) that came back (a floor: a retried call counts once, "
        "a reply that was not read not at all); estimated cost US$6.00 on those counts, at "
        "US$3 in and US$15 out a million tokens with no cache discount"
    )


def test_searches_with_no_record_are_said_and_a_row_with_nothing_to_say_says_nothing():
    unrecorded = (
        "  tokens not recorded for 2 search(es) of this row: no model_use.json beside their "
        "verdicts (searched before that record existed, or their search did not record its "
        "counts)"
    )
    assert model_use_lines([], 2) == [unrecorded]
    # Prices do not conjure a cost for searches that left no counts.
    assert model_use_lines([], 2, Prices(input=3, output=15)) == [unrecorded]
    tokens, more = model_use_lines([SPENT], 2, model=MODEL)
    assert tokens.startswith(f"  tokens reported by {MODEL} over 1 search(es)")
    assert more == unrecorded.replace("for 2 search(es)", "for 2 more search(es)")
    # Every other arm's row: nothing recorded, nothing missing.
    assert model_use_lines([], 0) == []
    assert model_use_lines([], 0, Prices(input=3, output=15)) == []
