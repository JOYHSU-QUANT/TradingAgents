"""The CLI's ``StatsCallbackHandler`` counts tokens through the shared reader.

Pinned so the stats panel and the perp lane's usage record cannot count the
same completion differently (issue #182 moved the extraction into
``tradingagents.llm_clients.completion_metadata``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from cli.stats_handler import StatsCallbackHandler


def _result(*, input_tokens=None, output_tokens=None) -> LLMResult:
    usage = None
    if input_tokens is not None or output_tokens is not None:
        usage = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "total_tokens": (input_tokens or 0) + (output_tokens or 0),
        }
    message = AIMessage(content="x", usage_metadata=usage)
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def test_on_llm_end_accumulates_both_directions_across_calls():
    handler = StatsCallbackHandler()
    handler.on_llm_end(_result(input_tokens=100, output_tokens=40), run_id=uuid.uuid4())
    handler.on_llm_end(_result(input_tokens=5, output_tokens=8192), run_id=uuid.uuid4())
    assert handler.get_stats()["tokens_in"] == 105
    assert handler.get_stats()["tokens_out"] == 8232


def test_a_response_without_usage_adds_nothing_and_does_not_raise():
    handler = StatsCallbackHandler()
    handler.on_llm_end(_result(), run_id=uuid.uuid4())
    handler.on_llm_end(LLMResult(generations=[]), run_id=uuid.uuid4())
    stats = handler.get_stats()
    assert (stats["tokens_in"], stats["tokens_out"]) == (0, 0)


def test_counts_go_through_the_shared_reader_not_a_local_get():
    # The discriminating input: the shared reader drops a bool count (bool is
    # an int subclass a provider never legitimately reports); the old inline
    # ``usage_metadata.get("output_tokens", 0)`` would have added 1. AIMessage
    # coerces the TypedDict, so the shape reaches the handler unvalidated the
    # way a third-party integration could hand it in.
    message = SimpleNamespace(
        response_metadata={},
        usage_metadata={"input_tokens": 7, "output_tokens": True, "total_tokens": 8},
    )
    generation = SimpleNamespace(message=message, generation_info=None)
    result = LLMResult.model_construct(generations=[[generation]], llm_output=None)
    handler = StatsCallbackHandler()
    handler.on_llm_end(result, run_id=uuid.uuid4())
    stats = handler.get_stats()
    assert (stats["tokens_in"], stats["tokens_out"]) == (7, 0)
