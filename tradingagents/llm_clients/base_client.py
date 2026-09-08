import warnings
from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Any

# Kwargs every provider client forwards from user config to its chat class: the
# cross-provider knobs (``temperature``, ``max_tokens``) plus the two
# transport-neutral extras every langchain chat class accepts. Each client
# declares its ``_passthrough_kwargs`` as this tuple plus its own
# provider-specific keys, so a new cross-provider knob is one edit here rather
# than one per client — the allowlist drops unrecognised keys silently, so a
# client left out of a five-file edit would simply never forward the knob
# (#184). The loop that applies the allowlist lives once too, in
# ``BaseLLMClient.forwarded_kwargs`` (#212).
_COMMON_PASSTHROUGH_KWARGS = ("temperature", "max_tokens", "max_retries", "callbacks")


def normalize_content(response):
    """Normalize LLM response content to a plain string.

    Multiple providers (OpenAI Responses API, Google Gemini 3) return content
    as a list of typed blocks, e.g. [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}].
    Downstream agents expect response.content to be a string. This extracts
    and joins the text blocks, discarding reasoning/metadata blocks.
    """
    content = response.content
    if isinstance(content, list):
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    return response


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    # The kwargs this client forwards from user config to its chat class.
    # Subclasses extend the common tuple with their provider-specific keys;
    # the loop that applies it is ``forwarded_kwargs`` below, so a change to
    # forwarding SEMANTICS (treating None as absent, logging what was
    # forwarded) is one edit, not one per client (#212).
    _passthrough_kwargs: tuple[str, ...] = _COMMON_PASSTHROUGH_KWARGS

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def forwarded_kwargs(self, *, skip: Collection[str] = ()) -> dict[str, Any]:
        """The subset of ``self.kwargs`` on this client's allowlist, minus ``skip``.

        Keys off the allowlist are dropped silently — the allowlist is the
        contract. ``skip`` is for a key the allowlist carries but THIS model
        rejects (Anthropic's ``effort`` below Opus 4.5, OpenAI's
        ``reasoning_effort`` off the reasoning families): the client decides
        per model, this helper only applies the decision.
        """
        return {
            key: self.kwargs[key]
            for key in self._passthrough_kwargs
            if key in self.kwargs and key not in skip
        }

    def get_provider_name(self) -> str:
        """Return the provider name used in warning messages."""
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        """Warn when the model is outside the known list for the provider."""
        if self.validate_model():
            return

        warnings.warn(
            (
                f"Model '{self.model}' is not in the known model list for "
                f"provider '{self.get_provider_name()}'. Continuing anyway."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """Return the configured LLM instance."""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """Validate that the model is supported by this client."""
        pass
