"""Structured output on the Claude 5.5 generation (#338).

Those models reject a forced ``tool_choice`` with a 400. Through OpenRouter the
OpenAI-compatible client binds the schema as a tool without forcing it; the
native Anthropic client takes Claude's structured outputs instead. Which
models are concerned is the capability table's business (tests/test_capabilities.py);
these tests cover each client's dispatch on it.
"""

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from tradingagents.llm_clients import create_llm_client


class Schema(BaseModel):
    x: int


def _capture(monkeypatch, cls) -> dict:
    """The keyword arguments the client hands langchain's with_structured_output."""
    captured = {}
    monkeypatch.setattr(
        cls, "with_structured_output", lambda self, schema, **kw: captured.update(kw) or "BOUND"
    )
    return captured


@pytest.mark.unit
def test_openrouter_binds_the_schema_without_forcing_tool_choice(monkeypatch):
    captured = _capture(monkeypatch, ChatOpenAI)
    llm = create_llm_client(provider="openrouter", model="anthropic/claude-sonnet-5.5").get_llm()
    assert llm.with_structured_output(Schema) == "BOUND"
    assert captured["method"] == "function_calling"
    assert captured["tool_choice"] is None


@pytest.mark.unit
def test_openrouter_still_forces_tool_choice_for_an_earlier_claude(monkeypatch):
    captured = _capture(monkeypatch, ChatOpenAI)
    llm = create_llm_client(provider="openrouter", model="anthropic/claude-sonnet-4.6").get_llm()
    llm.with_structured_output(Schema)
    assert "tool_choice" not in captured  # langchain's own forced value stands


@pytest.mark.unit
def test_native_anthropic_takes_json_schema_for_the_5_5_generation(monkeypatch):
    captured = _capture(monkeypatch, ChatAnthropic)
    llm = create_llm_client(provider="anthropic", model="claude-sonnet-5-5").get_llm()
    assert llm.with_structured_output(Schema) == "BOUND"
    assert captured["method"] == "json_schema"


@pytest.mark.unit
def test_native_anthropic_keeps_function_calling_for_an_earlier_claude(monkeypatch):
    captured = _capture(monkeypatch, ChatAnthropic)
    llm = create_llm_client(provider="anthropic", model="claude-sonnet-4-6").get_llm()
    llm.with_structured_output(Schema)
    assert captured["method"] == "function_calling"


@pytest.mark.unit
def test_native_anthropic_sends_a_method_asked_for_by_name(monkeypatch):
    captured = _capture(monkeypatch, ChatAnthropic)
    llm = create_llm_client(provider="anthropic", model="claude-sonnet-5-5").get_llm()
    llm.with_structured_output(Schema, method="json_schema", include_raw=True)
    assert captured == {"method": "json_schema", "include_raw": True}
