"""``completion_metadata`` reads the stop reason each provider actually files.

The provider rows are exercised through the INSTALLED langchain converters
(``ChatOpenAI._create_chat_result``, ``ChatAnthropic._format_output``,
``langchain_google_genai._response_to_result``) fed canned API responses —
the real slot and spelling, no network. A hand-built ``LLMResult`` would only
prove the reader agrees with the test's own idea of where the key lives, which
is exactly the drift this module exists to absorb. Bedrock is table-only:
``langchain-aws`` is an optional extra not installed in this environment.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tradingagents.llm_clients.completion_metadata import (
    TRUNCATED_STOP_REASONS,
    completion_metadata_of,
    is_truncated,
    stop_reason_of,
)


def _as_llm_result(chat_result) -> LLMResult:
    """The shape ``on_llm_end`` receives, minus langchain_core's metadata merge.

    Deliberately WITHOUT the merge: the reader must find the key in whichever
    slot the provider filed it, not rely on core copying it into
    ``response_metadata`` first.
    """
    return LLMResult(generations=[chat_result.generations], llm_output=chat_result.llm_output)


def _openai_result(finish_reason: str, *, reasoning: int | None = None) -> LLMResult:
    from langchain_openai import ChatOpenAI

    usage = {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    response = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4.1-2025-04-14",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "cut"},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return _as_llm_result(ChatOpenAI(model="gpt-4.1", api_key="test")._create_chat_result(response))


def _openai_responses_result(*, complete: bool) -> LLMResult:
    """The native ``openai`` provider's branch: the Responses API converter."""
    from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api
    from openai.types.responses import Response

    response = Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "model": "gpt-5",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed" if complete else "incomplete",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "cut", "annotations": []}],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed" if complete else "incomplete",
            **({} if complete else {"incomplete_details": {"reason": "max_output_tokens"}}),
            "usage": {
                "input_tokens": 10,
                "output_tokens": 8192,
                "total_tokens": 8202,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 100},
            },
        }
    )
    return _as_llm_result(_construct_lc_result_from_responses_api(response))


def _anthropic_result(stop_reason: str) -> LLMResult:
    from anthropic.types import Message, TextBlock, Usage
    from langchain_anthropic import ChatAnthropic

    message = Message(
        id="msg_1",
        content=[TextBlock(type="text", text="cut")],
        model="claude-sonnet-4-6",
        role="assistant",
        stop_reason=stop_reason,
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=10, output_tokens=8192),
    )
    return _as_llm_result(ChatAnthropic(model="claude-sonnet-4-6", api_key="test")._format_output(message))


def _google_result(finish_reason: str) -> LLMResult:
    from google.genai import types as gt
    from langchain_google_genai.chat_models import _response_to_result

    response = gt.GenerateContentResponse(
        candidates=[
            gt.Candidate(
                content=gt.Content(role="model", parts=[gt.Part(text="cut")]),
                finish_reason=finish_reason,
            )
        ],
        usage_metadata=gt.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=8192, total_token_count=8202
        ),
        model_version="gemini-2.5-flash",
    )
    return _as_llm_result(_response_to_result(response))


def _bedrock_shaped_result(stop_reason: str) -> LLMResult:
    # ChatBedrockConverse files ``stop_reason`` in response_metadata (langchain-aws
    # is not installed here, so the shape is transcribed, not produced).
    message = AIMessage(
        content="cut",
        response_metadata={"stop_reason": stop_reason, "model_name": "anthropic.claude-x"},
        usage_metadata={"input_tokens": 10, "output_tokens": 8192, "total_tokens": 8202},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


# ---------------------------------------------------------------------------
# One row per provider: the real converter, the real slot, the real spelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "truncated_value", "natural_value", "model"),
    [
        (_openai_result, "length", "stop", "gpt-4.1-2025-04-14"),
        (_anthropic_result, "max_tokens", "end_turn", "claude-sonnet-4-6"),
        (_google_result, "MAX_TOKENS", "STOP", "gemini-2.5-flash"),
        (_bedrock_shaped_result, "max_tokens", "end_turn", "anthropic.claude-x"),
    ],
    ids=["openai", "anthropic", "google", "bedrock"],
)
def test_each_provider_row_reads_truncated_and_natural_stops(
    build, truncated_value, natural_value, model
):
    cut = completion_metadata_of(build(truncated_value))
    assert cut.stop_reason == truncated_value
    assert cut.truncated is True
    assert cut.output_tokens == 8192
    assert cut.model == model

    whole = completion_metadata_of(build(natural_value))
    assert whole.stop_reason == natural_value
    assert whole.truncated is False
    assert whole.output_tokens == 8192


def test_the_native_openai_responses_branch_files_no_finish_reason_and_is_still_read():
    # The shipped ``openai`` provider (native base URL) takes the Responses API,
    # whose converter files ``status`` + ``incomplete_details`` and no
    # finish_reason at all. The reader must not go blind on that path — this
    # is the busiest provider's default branch.
    cut = completion_metadata_of(_openai_responses_result(complete=False))
    assert cut.stop_reason == "max_output_tokens"
    assert cut.truncated is True
    assert (cut.input_tokens, cut.output_tokens, cut.reasoning_tokens) == (10, 8192, 100)
    assert cut.model == "gpt-5"

    whole = completion_metadata_of(_openai_responses_result(complete=True))
    assert whole.stop_reason is None  # a completed Responses call reports no reason
    assert whole.truncated is False
    assert whole.output_tokens == 8192


def test_openai_reasoning_tokens_are_read_as_a_sub_count_of_output():
    meta = completion_metadata_of(_openai_result("length", reasoning=1000))
    assert meta.input_tokens == 10
    assert meta.output_tokens == 8192  # the cap counts the whole completion
    assert meta.reasoning_tokens == 1000


def test_tool_call_stops_are_not_truncation():
    # An analyst asking for a tool stops with a tool reason on every provider;
    # that is a normal turn boundary, never a bound cap.
    assert completion_metadata_of(_openai_result("tool_calls")).truncated is False
    assert completion_metadata_of(_anthropic_result("tool_use")).truncated is False


# ---------------------------------------------------------------------------
# The reader's own contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("length", True),
        ("max_tokens", True),
        ("MAX_TOKENS", True),
        ("max_output_tokens", True),
        ("Length", True),
        ("stop", False),
        ("end_turn", False),
        ("STOP", False),
        ("content_filter", False),
        ("", False),
        (None, False),
        (1, False),
    ],
)
def test_is_truncated_folds_case_and_refuses_non_strings(reason, expected):
    assert is_truncated(reason) is expected


def test_the_truncated_vocabulary_is_exactly_the_spellings_the_table_names():
    # A new spelling belongs in the module docstring's table AND here; a
    # spelling removed here silently turns a bound cap back into a plain
    # invalid_output downstream.
    assert frozenset({"length", "max_tokens", "max_output_tokens"}) == TRUNCATED_STOP_REASONS


def test_response_metadata_wins_over_generation_info_and_llm_output():
    # Priority order matters when the three slots disagree (a stale llm_output
    # from a combined batch, say): the message's own metadata is the most
    # specific statement about THIS completion.
    message = AIMessage(content="x", response_metadata={"finish_reason": "stop"})
    generation = ChatGeneration(message=message, generation_info={"finish_reason": "length"})
    result = LLMResult(generations=[[generation]], llm_output={"stop_reason": "max_tokens"})
    assert stop_reason_of(result) == "stop"

    # Each lower slot is reached only when the higher ones carry no reason.
    generation = ChatGeneration(
        message=AIMessage(content="x"), generation_info={"finish_reason": "length"}
    )
    assert (
        stop_reason_of(LLMResult(generations=[[generation]], llm_output={"stop_reason": "x"}))
        == "length"
    )
    generation = ChatGeneration(message=AIMessage(content="x"))
    assert (
        stop_reason_of(
            LLMResult(generations=[[generation]], llm_output={"stop_reason": "max_tokens"})
        )
        == "max_tokens"
    )


def test_a_result_with_nothing_to_read_is_all_none_and_not_truncated():
    empty = completion_metadata_of(LLMResult(generations=[]))
    assert (
        empty.stop_reason,
        empty.input_tokens,
        empty.output_tokens,
        empty.reasoning_tokens,
        empty.model,
    ) == (None, None, None, None, None)
    assert empty.truncated is False

    bare = completion_metadata_of(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]])
    )
    assert bare.stop_reason is None
    assert bare.output_tokens is None


def test_boolean_token_counts_are_treated_as_absent():
    # bool is an int subclass; a provider never reports one, and True read as
    # "1 output token" would be a fabricated measurement. AIMessage itself
    # coerces the TypedDict (True -> 1), so the guard is reached through a
    # duck-typed message inside an unvalidated result — the shape a third-party
    # integration that bypasses pydantic could hand in.
    message = SimpleNamespace(
        response_metadata={},
        usage_metadata={"input_tokens": 1, "output_tokens": True, "total_tokens": 2},
    )
    generation = SimpleNamespace(message=message, generation_info=None)
    result = LLMResult.model_construct(generations=[[generation]], llm_output=None)
    meta = completion_metadata_of(result)
    assert meta.output_tokens is None
