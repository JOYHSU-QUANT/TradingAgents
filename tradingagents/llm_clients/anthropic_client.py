import re
from typing import Any

from langchain_anthropic import ChatAnthropic

from .base_client import _COMMON_PASSTHROUGH_KWARGS, BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model

# Anthropic's extended-thinking ``effort`` parameter is accepted by Opus 4.5+,
# Sonnet 4.6+, and the Claude 5 family (Sonnet 5, Fable 5). Sonnet 4.5 and any
# Haiku version 400 with ``"This model does not support the effort parameter"``
# (#831). Versions may be dotted (``opus-4-8``) or single-number (``sonnet-5``,
# ``fable-5``); the per-family minimum below is forward-compatible.
_EFFORT_EXACT = {
    "claude-mythos-preview",  # non-standard preview name; effort-capable
    "claude-mythos-5",        # Fable 5 twin (Project Glasswing); effort-capable
}
_EFFORT_MODEL = re.compile(r"^claude-(opus|sonnet|fable)-(\d+)(?:-(\d+))?$")
_EFFORT_MIN_VERSION = {"opus": (4, 5), "sonnet": (4, 6), "fable": (5, 0)}


def _supports_effort(model: str) -> bool:
    """Whether Anthropic accepts the ``effort`` parameter for this model."""
    model_lc = model.lower()
    if model_lc in _EFFORT_EXACT:
        return True
    match = _EFFORT_MODEL.match(model_lc)
    if not match:
        return False
    family = match.group(1)
    major = int(match.group(2))
    minor = int(match.group(3)) if match.group(3) else 0
    return (major, minor) >= _EFFORT_MIN_VERSION[family]


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output and a structured-output method per model.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks. This normalizes to string for consistent
    downstream handling.

    ``with_structured_output`` keeps langchain's default, the schema bound as
    a tool the model is forced to call, for the models that take it. Forced
    tool use is gone from the Claude 5.5 generation on (#338), and langchain's
    function-calling path has no unforced form, so a model the capability
    table marks as not taking ``tool_choice`` gets Claude's own structured
    outputs (``method="json_schema"``, the ``output_config.format`` request
    field) instead.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method="function_calling", **kwargs):
        caps = get_capabilities(self.model)
        if method == "function_calling" and not caps.supports_tool_choice and caps.supports_json_schema:
            method = "json_schema"
        return super().with_structured_output(schema, method=method, **kwargs)


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    _passthrough_kwargs = _COMMON_PASSTHROUGH_KWARGS + (
        "timeout", "api_key", "http_client", "http_async_client", "effort",
    )

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        skip = () if _supports_effort(self.model) else ("effort",)
        llm_kwargs.update(self.forwarded_kwargs(skip=skip))

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)
