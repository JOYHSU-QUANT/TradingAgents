"""Declarative per-model capability table for the LLM clients.

This is the single place that knows which model IDs reject which API
parameters or require which structured-output method. The LLM client
subclasses consult ``get_capabilities(model_name)`` instead of hardcoding
model-name ``if`` ladders, so adding a new model (or a new provider quirk)
means editing this table — not the client code.

Pattern adapted from the per-model ``compat:`` flags DeepSeek themselves
publish in their integration guides (e.g. the Oh My Pi config schema
documents ``supportsToolChoice``, ``requiresReasoningContentForToolCalls``
as declarative per-model fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

StructuredMethod = Literal[
    "function_calling",  # uses tools; respects supports_tool_choice
    "json_mode",         # uses response_format={"type":"json_object"}
    "json_schema",       # uses response_format={"type":"json_schema",...}
    "none",              # no structured output available; caller falls back to free-text
]


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model accepts at the API level."""

    supports_tool_choice: bool
    supports_json_mode: bool
    supports_json_schema: bool
    preferred_structured_method: StructuredMethod
    # DeepSeek thinking-mode models 400 if reasoning_content from prior
    # assistant turns is not echoed back on the next request.
    requires_reasoning_content_roundtrip: bool = False
    # MiniMax M2.x reasoning models need ``reasoning_split=True`` so the
    # <think> block lands in ``reasoning_details`` instead of polluting
    # ``content``. The flag is rejected by non-reasoning MiniMax models
    # (Coding Plan, MiniMax-Text-01, etc.), so we only set it where the
    # model actually consumes it. (#826)
    requires_reasoning_split: bool = False


# DeepSeek's thinking models accept the ``tools`` array but reject the
# ``tool_choice`` parameter (official Oh My Pi integration guide and the
# 400 response in issue #678). Their official tool-calling examples
# (api-docs.deepseek.com/guides/tool_calls) pass ``tools=[...]`` without
# ``tool_choice`` — we mirror that pattern by setting supports_tool_choice
# to False and letting the client suppress the kwarg.
_DEEPSEEK_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_content_roundtrip=True,
)

_DEEPSEEK_CHAT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
)

# MiniMax M2.x reasoning models accept the tools array, but their
# tool_choice parameter is restricted to the enum {"none", "auto"}
# (platform.minimax.io/docs/api-reference/text-post). Langchain's
# function_calling path sends tool_choice as a function-spec dict, which
# MiniMax 400s — same shape as the DeepSeek bug. supports_tool_choice=False
# makes the dispatch in NormalizedChatOpenAI suppress the kwarg; the schema
# still ships as a tool. json_mode response_format is only for
# MiniMax-Text-01, not M2.x.
_MINIMAX_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=False,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_split=True,
)

# Anthropic retired forced tool use with the Claude 5.5 generation: Sonnet 5.5,
# Opus 5.5 and Fable 5.1 answer a ``tool_choice`` of type ``tool`` or ``any``
# with a 400, "not supported for this model" (#338); Mythos 5.1, Fable's twin,
# is taken to as well, not observed.
# The schema still binds as a tool with the choice left to the model; an
# answer in prose instead is a logged miss and a free-text retry
# (``agents/utils/structured.py``). Anthropic has no ``json_object`` mode; its
# structured outputs are what the native client takes for these models.
_CLAUDE_NO_FORCED_TOOL_CHOICE = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=False,
    supports_json_schema=True,
    preferred_structured_method="function_calling",
)

_DEFAULT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=True,
    preferred_structured_method="function_calling",
)

# ``claude-<family>-<major>[.-<minor>]``: the minor is at most two digits and
# may not be followed by a digit, so a dated release (``claude-sonnet-5-20260901``)
# reads as minor 0, not 20260901. Dotted versions are OpenRouter's spelling
# (``anthropic/claude-sonnet-5.5``).
_CLAUDE_MODEL = re.compile(r"^claude-(sonnet|opus|fable|mythos)-(\d+)(?:[.-](\d{1,2}))?(?!\d)")
# The first version of each family without forced tool use; later ones inherit.
_FORCED_TOOL_CHOICE_RETIRED = {"sonnet": (5, 5), "opus": (5, 5), "fable": (5, 1), "mythos": (5, 1)}
# Anthropic's own namespace in front of a Claude ID: OpenRouter's ``anthropic/``,
# Bedrock's ``anthropic.`` with or without a cross-region inference profile
# prefix (``us.``, ``global.``, ``us-gov.``). Another publisher's is left on.
_ANTHROPIC_NAMESPACE = re.compile(r"^(?:[a-z-]+\.)?anthropic[./]")


def _claude_rejects_forced_tool_choice(model_name: str) -> bool:
    """Whether Anthropic answers a forced ``tool_choice`` for this model with a 400 (#338).

    Takes the native ID (``claude-sonnet-5-5``) or one in Anthropic's own
    namespace (``_ANTHROPIC_NAMESPACE``: OpenRouter, Bedrock); other publishers'
    namespaces and other families (Haiku) are not known to, and answer ``False``.
    """
    match = _CLAUDE_MODEL.match(_ANTHROPIC_NAMESPACE.sub("", model_name.lower()))
    if not match:
        return False
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    return (major, minor) >= _FORCED_TOOL_CHOICE_RETIRED[family]


# Exact-ID matches take precedence over pattern matches.
_BY_ID: dict[str, ModelCapabilities] = {
    "deepseek-chat": _DEEPSEEK_CHAT,
    "deepseek-reasoner": _DEEPSEEK_THINKING,
    "deepseek-v4-flash": _DEEPSEEK_THINKING,
    "deepseek-v4-pro": _DEEPSEEK_THINKING,
    # MiniMax — full official model lineup per
    # platform.minimax.io/docs/api-reference/text-openai-api
    "MiniMax-M2.7": _MINIMAX_THINKING,
    "MiniMax-M2.7-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.5": _MINIMAX_THINKING,
    "MiniMax-M2.5-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.1": _MINIMAX_THINKING,
    "MiniMax-M2.1-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2": _MINIMAX_THINKING,
}

# Forward-compat patterns. New ``deepseek-v5-*`` / ``deepseek-reasoner-*``
# or ``MiniMax-M3*`` variants inherit the thinking-mode quirks automatically.
_BY_PATTERN: list[tuple[re.Pattern[str], ModelCapabilities]] = [
    (re.compile(r"^deepseek-v\d"), _DEEPSEEK_THINKING),
    (re.compile(r"^deepseek-reasoner"), _DEEPSEEK_THINKING),
    (re.compile(r"^MiniMax-M\d"), _MINIMAX_THINKING),
]


def get_capabilities(model_name: str) -> ModelCapabilities:
    """Resolve capabilities by exact ID, then pattern, then default."""
    # OpenRouter namespaces official DeepSeek models as ``deepseek/<id>``, so
    # strip that prefix to reuse the same quirks as the native provider — e.g.
    # ``deepseek/deepseek-v4-flash`` must suppress tool_choice like
    # ``deepseek-v4-flash`` does, not fall through to _DEFAULT (#1199). Only the
    # official namespace is stripped; third-party finetunes on other publishers
    # (e.g. ``tngtech/deepseek-...``) keep _DEFAULT, since their quirks are unknown.
    if model_name.startswith("deepseek/"):
        model_name = model_name.removeprefix("deepseek/")

    if model_name in _BY_ID:
        return _BY_ID[model_name]
    for pattern, caps in _BY_PATTERN:
        if pattern.match(model_name):
            return caps
    if _claude_rejects_forced_tool_choice(model_name):
        return _CLAUDE_NO_FORCED_TOOL_CHOICE
    return _DEFAULT
