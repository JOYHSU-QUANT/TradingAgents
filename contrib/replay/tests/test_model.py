"""The engine-backed model: which client it builds, what it sends, what it reads back.

The engine surface is a fake (:class:`~contrib.replay.upstream.Engine` holds
any callables), so nothing here needs a key or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from contrib.replay.model import engine_model
from contrib.replay.upstream import Engine

from .papers import variant as make_variant


@dataclass
class _Call:
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    truncated: bool


@dataclass
class _Collector:
    calls: list[_Call] = field(default_factory=list)


@dataclass
class _Message:
    role: str
    content: str


class _Fake:
    """Records what the engine surface was asked, and answers with ``content`` and ``call``."""

    def __init__(self, content: Any = "the answer", call: _Call | None = None) -> None:
        self.content = content
        self.call = call
        self.client_kwargs: dict[str, Any] = {}
        self.invoked: list[tuple[list[_Message], dict]] = []

    def engine(self) -> Engine:
        fake = self

        def invoke(messages, config):
            fake.invoked.append((messages, config))
            (collector,) = config["callbacks"]
            if fake.call is not None:
                collector.calls.append(fake.call)
            return SimpleNamespace(content=fake.content)

        def create_llm_client(**kwargs):
            fake.client_kwargs = kwargs
            return SimpleNamespace(get_llm=lambda: SimpleNamespace(invoke=invoke))

        def normalize(response):
            if isinstance(response.content, list):
                response.content = "\n".join(response.content)
            return response

        return Engine(
            CompletionUsageCollector=_Collector,
            HumanMessage=lambda content: _Message("human", content),
            SystemMessage=lambda content: _Message("system", content),
            create_llm_client=create_llm_client,
            normalize_content=normalize,
        )


def test_the_client_is_built_for_the_variants_provider_and_cap():
    fake = _Fake()
    engine_model(make_variant(max_tokens=4096), engine=fake.engine())
    assert fake.client_kwargs == {
        "provider": "openrouter",
        "model": "a/b",
        "base_url": None,
        "max_tokens": 4096,
    }


def test_gemini_takes_its_cap_under_its_own_key_and_a_set_temperature_is_forwarded():
    fake = _Fake()
    engine_model(make_variant(provider="google", temperature=0.2), engine=fake.engine())
    assert fake.client_kwargs["max_output_tokens"] == 8192
    assert "max_tokens" not in fake.client_kwargs
    assert fake.client_kwargs["temperature"] == 0.2


def test_the_two_messages_are_sent_in_order_with_a_fresh_collector_each_call():
    fake = _Fake(call=_Call("m-1", 120, 30, False))
    ask = engine_model(make_variant(), engine=fake.engine())
    ask("the system", "the human")
    ask("the system", "the human")
    (first, config1), (_, config2) = fake.invoked
    assert [(m.role, m.content) for m in first] == [
        ("system", "the system"),
        ("human", "the human"),
    ]
    assert config1["callbacks"][0] is not config2["callbacks"][0]


def test_the_completion_carries_the_text_the_tokens_and_the_truncation_verdict():
    fake = _Fake(content=["part one", "part two"], call=_Call("m-1", 120, 30, True))
    completion = engine_model(make_variant(), engine=fake.engine())("s", "h")
    assert completion.text == "part one\npart two"
    assert (completion.truncated, completion.model) == (True, "m-1")
    assert (completion.input_tokens, completion.output_tokens) == (120, 30)


def test_a_call_the_collector_did_not_see_is_not_truncated_and_says_so():
    completion = engine_model(make_variant(), engine=_Fake().engine())("s", "h")
    assert (completion.truncated, completion.model, completion.input_tokens) == (False, None, None)
    assert completion.usage_reported is False
    seen = engine_model(make_variant(), engine=_Fake(call=_Call("m", 1, 1, False)).engine())
    assert seen("s", "h").usage_reported is True


def test_the_real_engine_surface_loads_and_its_factory_refuses_an_unknown_provider():
    pytest.importorskip("langchain_core")
    from contrib.replay.upstream import load_engine

    engine = load_engine()
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        engine.create_llm_client(provider="no-such-provider", model="x")
