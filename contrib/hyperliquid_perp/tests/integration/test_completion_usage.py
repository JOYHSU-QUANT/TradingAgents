"""``CompletionUsageCollector`` attributes each completion to its graph node.

Driven through a real ``langgraph`` ``StateGraph`` with langchain's fake chat
model, because the property under test is the seam itself: a handler attached
to the MODEL's constructor (not passed per call) must still receive the node
name langgraph stamps on the run's metadata, and the merged response metadata
``on_llm_end`` sees. A hand-invoked handler would pass whatever the test put
in and prove nothing about the wiring the provider relies on.
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from contrib.hyperliquid_perp.integration.completion_usage import (
    CompletionCall,
    CompletionUsageCollector,
)

from ..conftest import fake_completion_message


class _State(TypedDict):
    text: str


def _ai(content: str, *, finish_reason: str, output_tokens: int, model: str = "fake-1") -> AIMessage:
    return fake_completion_message(
        content=content, finish_reason=finish_reason, output_tokens=output_tokens, model=model
    )


def _run_two_node_graph(collector, *, analyst_msg: AIMessage, decision_msg: AIMessage) -> None:
    """A Market Analyst node then a Portfolio Manager node, each one LLM call."""
    analyst_llm = GenericFakeChatModel(messages=iter([analyst_msg]), callbacks=[collector])
    decision_llm = GenericFakeChatModel(messages=iter([decision_msg]), callbacks=[collector])

    def analyst(state):
        return {"text": analyst_llm.invoke("analyse").content}  # no config passed, like the agents

    def manager(state):
        return {"text": decision_llm.invoke("decide").content}

    g = StateGraph(_State)
    g.add_node("Market Analyst", analyst)
    g.add_node("Portfolio Manager", manager)
    g.add_edge(START, "Market Analyst")
    g.add_edge("Market Analyst", "Portfolio Manager")
    g.add_edge("Portfolio Manager", END)
    g.compile().invoke({"text": ""})


def test_calls_are_attributed_to_the_langgraph_node_that_made_them():
    collector = CompletionUsageCollector()
    _run_two_node_graph(
        collector,
        analyst_msg=_ai("report", finish_reason="stop", output_tokens=1200, model="quick-1"),
        decision_msg=_ai("decision", finish_reason="length", output_tokens=8192, model="deep-1"),
    )
    assert collector.calls == (
        CompletionCall(
            node="Market Analyst",
            model="quick-1",
            input_tokens=10,
            output_tokens=1200,
            reasoning_tokens=None,
            stop_reason="stop",
        ),
        CompletionCall(
            node="Portfolio Manager",
            model="deep-1",
            input_tokens=10,
            output_tokens=8192,
            reasoning_tokens=None,
            stop_reason="length",
        ),
    )
    assert [c.truncated for c in collector.calls] == [False, True]
    assert collector.total_output_tokens() == 9392


def test_a_string_prompt_llm_is_attributed_through_on_llm_start():
    # Legacy (non-chat) LLM clients start through on_llm_start, not
    # on_chat_model_start; the node attribution must not depend on which hook
    # langchain picked.
    collector = CompletionUsageCollector()
    run_id = uuid.uuid4()
    collector.on_llm_start({}, ["prompt"], run_id=run_id, metadata={"langgraph_node": "Trader"})
    message = _ai("…", finish_reason="length", output_tokens=8192)
    collector.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)
    (call,) = collector.calls
    assert call.node == "Trader"
    assert call.truncated is True


def test_the_decision_lane_reads_its_own_last_call_not_the_analysts_cut():
    collector = CompletionUsageCollector()
    _run_two_node_graph(
        collector,
        analyst_msg=_ai("cut report", finish_reason="length", output_tokens=8192),
        decision_msg=_ai("whole decision", finish_reason="stop", output_tokens=900),
    )
    assert [c.node for c in collector.truncated_calls()] == ["Market Analyst"]
    # The decision lane must NOT see the analyst's cut as its own truncation:
    # that would relabel a whole decision's parse verdict.
    decision = collector.last_call("Portfolio Manager")
    assert decision is not None
    assert decision.truncated is False
    assert collector.last_call("Trader") is None  # a node that never completed


def test_last_call_is_the_last_completion_of_that_node():
    # With structured_output on, the Portfolio Manager makes a structured
    # attempt and then a free-text fallback; only the LAST one's text reaches
    # final_trade_decision, so that is the completion the verdict reads.
    collector = CompletionUsageCollector()
    for finish_reason, output_tokens in (("length", 8192), ("stop", 700)):
        run_id = uuid.uuid4()
        collector.on_chat_model_start(
            {}, [[]], run_id=run_id, metadata={"langgraph_node": "Portfolio Manager"}
        )
        message = _ai("…", finish_reason=finish_reason, output_tokens=output_tokens)
        collector.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
        )
    last = collector.last_call("Portfolio Manager")
    assert (last.stop_reason, last.output_tokens, last.truncated) == ("stop", 700, False)
    # Both are still in the record: the cut attempt was paid for.
    assert [c.truncated for c in collector.calls] == [True, False]


def test_a_call_outside_any_graph_node_is_recorded_with_no_node():
    # The base engine's process_signal / reflection calls run after or before
    # the graph: paid for, counted, but attributable to no node.
    collector = CompletionUsageCollector()
    llm = GenericFakeChatModel(
        messages=iter([_ai("signal", finish_reason="stop", output_tokens=5)]), callbacks=[collector]
    )
    llm.invoke("classify")
    (call,) = collector.calls
    assert call.node is None
    assert call.output_tokens == 5
    assert call.truncated is False


def test_to_record_is_the_sidecar_shape():
    collector = CompletionUsageCollector()
    _run_two_node_graph(
        collector,
        analyst_msg=_ai("report", finish_reason="stop", output_tokens=1000, model="quick-1"),
        decision_msg=_ai("decision", finish_reason="length", output_tokens=8192, model="deep-1"),
    )
    record = collector.to_record(cap=8192)
    assert record == {
        "cap": 8192,
        "call_count": 2,
        "total_output_tokens": 9192,
        "truncated_nodes": ["Portfolio Manager"],
        "calls": [
            {
                "node": "Market Analyst",
                "model": "quick-1",
                "input_tokens": 10,
                "output_tokens": 1000,
                "reasoning_tokens": None,
                "stop_reason": "stop",
                "truncated": False,
            },
            {
                "node": "Portfolio Manager",
                "model": "deep-1",
                "input_tokens": 10,
                "output_tokens": 8192,
                "reasoning_tokens": None,
                "stop_reason": "length",
                "truncated": True,
            },
        ],
    }


def test_an_errored_run_leaves_no_dangling_node_attribution():
    # on_llm_error must release the run's node so a later, unrelated end event
    # with a recycled run id cannot inherit it.
    collector = CompletionUsageCollector()
    run_id = uuid.uuid4()
    collector.on_chat_model_start({}, [[]], run_id=run_id, metadata={"langgraph_node": "Trader"})
    collector.on_llm_error(RuntimeError("boom"), run_id=run_id)
    assert collector.calls == ()
    assert collector._node_by_run == {}


def test_a_metadata_read_failure_drops_the_call_and_never_raises(caplog):
    # Measurement must not cost the decision: a response the reader cannot
    # make sense of is logged and skipped, and the handler returns normally.
    collector = CompletionUsageCollector()
    run_id = uuid.uuid4()
    collector.on_chat_model_start({}, [[]], run_id=run_id, metadata={"langgraph_node": "Trader"})

    class _Hostile:
        llm_output = None

        @property
        def generations(self):
            raise RuntimeError("no generations")

    with caplog.at_level("ERROR"):
        collector.on_llm_end(_Hostile(), run_id=run_id)  # type: ignore[arg-type]
    assert collector.calls == ()
    assert "completion metadata could not be read" in caplog.text


def test_calls_that_report_no_usage_add_zero_to_the_total():
    collector = CompletionUsageCollector()
    run_id = uuid.uuid4()
    collector.on_chat_model_start({}, [[]], run_id=run_id, metadata=None)
    collector.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]), run_id=run_id
    )
    (call,) = collector.calls
    assert call.output_tokens is None
    assert collector.total_output_tokens() == 0
    assert collector.to_record(cap=None)["total_output_tokens"] == 0


@pytest.mark.parametrize("bad_node", [None, 7, ["Trader"]])
def test_a_non_string_node_name_is_recorded_as_none(bad_node):
    collector = CompletionUsageCollector()
    run_id = uuid.uuid4()
    collector.on_chat_model_start({}, [[]], run_id=run_id, metadata={"langgraph_node": bad_node})
    collector.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]), run_id=run_id
    )
    assert collector.calls[0].node is None
