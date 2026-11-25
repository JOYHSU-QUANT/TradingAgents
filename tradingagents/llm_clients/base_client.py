import logging
import re
import warnings
from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Any

logger = logging.getLogger(__name__)

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


# A code point from U+D800 to U+DFFF standing in a ``str`` is half of a UTF-16
# pair with no partner. Every one in a ``str`` is such a half: a well-formed
# pair is a single code point above U+FFFF. Built from ``chr`` so that no
# editor or tool rewrites the escapes into the characters themselves.
_LONE_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_REPLACEMENT_CHARACTER = chr(0xFFFD)


def _without_lone_surrogates(value, found):
    """``value`` with each lone surrogate in any text it holds as U+FFFD.

    ``found`` is a one-item list that grows by how many were replaced. What
    holds none comes back as the same object, containers and messages
    included, so a clean input is passed on untouched. Read through: text,
    lists, tuples, dicts (keys too: a tool call's argument names are a
    model's words), and anything pydantic — a message, with its content,
    its tool calls, its other declared fields and the extra ones a provider
    put on it, or a prompt value with its messages. Told apart by shape and
    not by class, so this module imports no chat library. Every field is
    read, the ones a client sends and the ones it does not (a reply's
    metadata, its id, the raw copy of its tool calls a provider leaves
    beside the parsed one): the count is of the input a request is built
    from, not of the request's bytes, and it can be more than what was sent.

    Not read: what is none of those (a dataclass, a set). A container is
    followed as far as it goes: one that holds itself is a
    ``RecursionError``, with or without such a character in it; a chat
    input is a tree. And these hold only for something that had to change,
    which a chat input is not known to run into: a subclass of ``list``,
    ``tuple`` or ``dict`` comes back as the plain type, with nothing else of
    the container carried over; and two texts that differed only in such
    characters are the same text afterwards — two keys of one dict become
    one key, the later value kept, and two tool calls' ids one id.
    """
    if isinstance(value, str):
        replaced, count = _LONE_SURROGATE.subn(_REPLACEMENT_CHARACTER, value)
        found[0] += count
        return replaced if count else value
    if isinstance(value, dict):
        pairs = [
            (_without_lone_surrogates(key, found), _without_lone_surrogates(item, found))
            for key, item in value.items()
        ]
        same = all(
            new_key is key and new_item is item
            for (new_key, new_item), (key, item) in zip(pairs, value.items(), strict=True)
        )
        return value if same else dict(pairs)
    if isinstance(value, (list, tuple)):
        items = [_without_lone_surrogates(item, found) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items if isinstance(value, list) else tuple(items)
    fields = getattr(type(value), "model_fields", None)
    if fields is not None and hasattr(value, "model_copy"):
        update = {}
        # Declared fields, then what the model allows beside them: a message
        # keeps a provider's additions there, and a client may send those too.
        for name in (*fields, *(getattr(value, "model_extra", None) or ())):
            old = getattr(value, name, None)
            new = _without_lone_surrogates(old, found)
            if new is not old:
                update[name] = new
        return value.model_copy(update=update) if update else value
    return value


def sendable(input):
    """What a chat model is about to be asked, with nothing in it that cannot be sent.

    A model's reply can hold a lone surrogate (its provider's JSON carried
    half of an escaped pair and the client decoded it), and so can a tool's
    result or a vendor's text. A graph puts each into a later node's prompt,
    and a request is UTF-8, which has no encoding for one: with the
    OpenAI-compatible client, the one this was measured on, building the
    request raises ``UnicodeEncodeError`` before anything is sent, so the
    node fails on text it was only passing on, and whoever drives the graph
    retries or gives up (#441). Each such
    character becomes U+FFFD, one for one, in what is sent — the graph's own
    state is not touched, so what is stored from it still goes through the
    replacement its own writers make, and their counts — and a warning says
    how many, since U+FFFD in a prompt does not say
    whether its author or this function put it there. An input holding none
    is returned as the same object.

    Every client's ``invoke`` passes its input through here, beside
    :func:`normalize_content` on the way back: the two ends of the call
    every node of this repository makes, whichever way the node wraps the
    model (bound tools, structured output). ``invoke`` only: ``stream``,
    ``ainvoke`` and ``astream`` on these chat classes send their input as it
    is, and no caller here uses them. The count is of this request: a
    character a graph keeps in its state is counted again by each later
    request that quotes it.
    """
    found = [0]
    cleaned = _without_lone_surrogates(input, found)
    if found[0]:
        logger.warning(
            "%d lone surrogate(s) in a model request were replaced with U+FFFD before "
            "sending: none can be written as UTF-8 "
            "(text a graph keeps is counted by each request quoting it)",
            found[0],
        )
    return cleaned


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
        allowlist = self._passthrough_kwargs
        if isinstance(allowlist, str):
            # A bare string iterates its characters and forwards nothing, silently.
            raise TypeError(
                f"{type(self).__name__}._passthrough_kwargs must be a tuple of key names, not a str"
            )
        return {key: self.kwargs[key] for key in allowlist if key in self.kwargs and key not in skip}

    def warn_if_temperature_dropped(self, llm: Any) -> None:
        """Warn when a forwarded ``temperature`` did not survive the chat class.

        langchain-openai's ``validate_temperature`` nulls the knob on gpt-5
        reasoning models unless ``reasoning_effort`` is ``"none"``, so an
        operator's ``TRADINGAGENTS_TEMPERATURE`` would vanish with no signal —
        the #177 shape for another knob. Judged on the OUTCOME (the attribute
        after construction), not by re-deriving the library's rule, so it
        stays true when that rule moves (#212).
        """
        sent = self.kwargs.get("temperature")
        if sent is None or getattr(llm, "temperature", None) is not None:
            return
        warnings.warn(
            f"temperature={sent!r} was set for model '{self.model}' but the chat "
            "client dropped it: this model only takes a temperature with reasoning "
            "off. Set openai_reasoning_effort='none' "
            "(TRADINGAGENTS_OPENAI_REASONING_EFFORT) or unset the temperature.",
            RuntimeWarning,
            stacklevel=3,
        )

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
