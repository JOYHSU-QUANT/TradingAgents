"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def bind_structured(
    llm: Any, schema: type[T], agent_name: str, *, config_gated: bool = True
) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.

    ``config_gated`` agents additionally honor the ``structured_output`` config
    key: when the config sets it to ``False``, the binding is skipped so every
    call takes the free-text path. That switch exists for callers that inject
    an output contract into the prompt and need it to survive in the agent's
    final text (e.g. the Hyperliquid perp target JSON): a schema render emits
    only the schema's own fields, so a *successful* structured call would
    silently drop the contract. Pass ``config_gated=False`` for agents whose
    rendered output carries no such contract (the Sentiment Analyst).

    The key is tri-state: ``None`` (like an absent key) counts as *unset* and
    keeps the default (enabled), matching the None-means-default convention of
    nullable config keys such as ``temperature``; ``False`` disables the
    binding. Any other non-bool value raises ``ValueError`` here, at agent
    construction: the gate picks between two silently-diverging output paths,
    so junk (e.g. a quoted ``"false"`` that reads as a truthy string) must not
    pick a side — the same contract the Hyperliquid config loader enforces
    with ``bool_from_yaml``.
    """
    if config_gated:
        from tradingagents.dataflows.config import get_config

        enabled = get_config().get("structured_output")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError(
                f"config key 'structured_output' must be a bool or None, got "
                f"{enabled!r} — a truthy non-bool (e.g. the string 'false') "
                "would silently keep structured output enabled and drop a "
                "prompt-injected output contract"
            )
        if enabled is False:
            logger.info(
                "%s: structured output disabled by config; using free-text generation",
                agent_name,
            )
            return None
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name,
            exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # A thinking model can answer in plain text instead of calling
                # the tool, leaving the parser with nothing to return. Treat it
                # as a structured miss and fall back, with a clear reason.
                raise ValueError("structured output returned no parsed result")
            return render(result)
        except Exception as exc:
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name,
                exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content
