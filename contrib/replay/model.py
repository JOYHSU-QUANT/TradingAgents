"""The variant's model, reached through the engine's own client factory (replay plan PR 2).

The replay wires no SDK of its own: ``tradingagents``' ``create_llm_client``
builds the client for the variant's provider, the factory the paper
trader's graph builds its LLMs with, so the keys, the base URLs and the
provider quirks are the ones the daemon runs under. The one knob translated
here is the completion cap's key, which Gemini names ``max_output_tokens``
(the graph's ``_get_provider_kwargs`` makes the same switch). The
temperature is forwarded only when the variant sets one, so an unset
temperature is each provider's own default, as in the graph.

The truncation verdict is read the way the engine run reads it: a
``CompletionUsageCollector`` rides the call as a callback and reports the
provider's stop reason. The text is normalised by the engine's own
``normalize_content`` (typed content blocks are joined to their text), so
the parse seam sees what it would have seen in the graph.
"""

from __future__ import annotations

from typing import Any

from .replay import Completion, Model
from .upstream import Engine, load_engine
from .variant import Variant

__all__ = ["engine_model"]


def engine_model(variant: Variant, *, engine: Engine | None = None) -> Model:
    """A :data:`~.replay.Model` that asks ``variant``'s provider and model.

    ``engine`` is the borrowed engine surface; ``None`` loads the real one
    (:func:`~.upstream.load_engine`), and the tests pass a fake. The client
    reads the provider's key from the environment the ``tradingagents``
    import loaded; a provider the factory does not know raises ``ValueError``.
    """
    engine = load_engine() if engine is None else engine
    kwargs: dict[str, Any] = {}
    cap_key = "max_output_tokens" if variant.provider.lower() == "google" else "max_tokens"
    kwargs[cap_key] = variant.max_tokens
    if variant.temperature is not None:
        kwargs["temperature"] = variant.temperature
    client = engine.create_llm_client(
        provider=variant.provider, model=variant.model, base_url=None, **kwargs
    )
    llm = client.get_llm()

    def ask(system: str, human: str) -> Completion:
        collector = engine.CompletionUsageCollector()
        response = llm.invoke(
            [engine.SystemMessage(content=system), engine.HumanMessage(content=human)],
            config={"callbacks": [collector]},
        )
        response = engine.normalize_content(response)
        calls = collector.calls
        call = calls[-1] if calls else None
        return Completion(
            text=response.content,
            truncated=call is not None and call.truncated,
            model=None if call is None else call.model,
            input_tokens=None if call is None else call.input_tokens,
            output_tokens=None if call is None else call.output_tokens,
        )

    return ask
