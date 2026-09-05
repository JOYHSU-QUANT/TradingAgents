"""Provider-neutral reading of one completion's stop reason and token usage.

Every provider answers HTTP 200 when the completion cap binds; the only signal
is the stop reason, and each langchain integration spells it and files it
differently. Rows verified against the installed packages on 2026-09-05
(langchain-openai 1.3.3, langchain-anthropic 1.4.8, langchain-google-genai
4.2.6) EXCEPT Bedrock: ``langchain-aws`` is an optional extra not installed
here, so that row is transcribed from its documented Converse response shape
and pinned only by a hand-built result in the tests.

| provider (langchain package)                 | key                          | slot on the LLMResult                                   | truncated value       |
|----------------------------------------------|------------------------------|---------------------------------------------------------|-----------------------|
| openai Chat Completions (azure, openrouter,  | ``finish_reason``            | ``generation_info`` (merged into ``response_metadata``) | ``length``            |
| deepseek, openai_compatible, ollama)         |                              |                                                         |                       |
| openai Responses API (native ``openai``)     | ``status`` + ``incomplete_details.reason`` | ``response_metadata``; NO finish_reason at all | ``max_output_tokens`` |
| anthropic                                    | ``stop_reason``              | ``llm_output`` (merged into ``response_metadata``)      | ``max_tokens``        |
| google (Gemini)                              | ``finish_reason``            | ``generation_info``, the enum NAME                      | ``MAX_TOKENS``        |
| bedrock (Converse)                           | ``stop_reason``              | ``response_metadata``                                   | ``max_tokens``        |

The native ``openai`` provider takes the Responses branch
(``openai_client.py``: ``use_responses_api=True`` on the native base URL),
whose converter files no stop reason key — a bound cap is
``status: "incomplete"`` with ``incomplete_details: {"reason":
"max_output_tokens"}``. :func:`stop_reason_of` reads that pair as the stop
reason when no ``finish_reason``/``stop_reason`` is present, so the reader
does not go blind on the busiest provider's default path.

langchain_core merges ``generation_info`` and ``llm_output`` into the
message's ``response_metadata`` before ``on_llm_end`` fires, but a reader
handed a raw ``ChatResult`` (or a provider that skips the merge) must not
depend on it — :func:`stop_reason_of` reads all three slots, in that order.

Case-insensitive on purpose: Google reports the enum NAME (upper case), the
others lower case, and a stop reason is a closed vocabulary per provider, so
folding case cannot confuse two distinct reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.outputs import LLMResult

__all__ = [
    "TRUNCATED_STOP_REASONS",
    "CompletionMetadata",
    "completion_metadata_of",
    "is_truncated",
    "stop_reason_of",
]

# The spellings that mean "the completion cap bound" — see the table above.
TRUNCATED_STOP_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

_STOP_REASON_KEYS = ("finish_reason", "stop_reason")


def is_truncated(stop_reason: object) -> bool:
    """True when ``stop_reason`` says the completion hit its token cap."""
    return isinstance(stop_reason, str) and stop_reason.lower() in TRUNCATED_STOP_REASONS


def _first_generation(result: LLMResult) -> Any | None:
    try:
        return result.generations[0][0]
    except (IndexError, TypeError):
        return None


def _metadata_slots(result: LLMResult) -> list[dict[str, Any]]:
    """The three places a provider may file per-completion metadata, in priority order."""
    generation = _first_generation(result)
    slots: list[dict[str, Any]] = []
    if generation is not None:
        message = getattr(generation, "message", None)
        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            slots.append(response_metadata)
        generation_info = getattr(generation, "generation_info", None)
        if isinstance(generation_info, dict):
            slots.append(generation_info)
    if isinstance(result.llm_output, dict):
        slots.append(result.llm_output)
    return slots


def _first_str(slots: list[dict[str, Any]], keys: tuple[str, ...]) -> str | None:
    """The first non-empty string filed under any of ``keys``, slots in priority order."""
    for slot in slots:
        for key in keys:
            value = slot.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _incomplete_reason(slots: list[dict[str, Any]]) -> str | None:
    """The Responses API's spelling: ``status: incomplete`` + ``incomplete_details.reason``."""
    for slot in slots:
        if slot.get("status") != "incomplete":
            continue
        details = slot.get("incomplete_details")
        # A dict after the converter's ``model_dump``; the SDK object if a
        # caller hands the raw response through un-dumped.
        reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
        if isinstance(reason, str) and reason:
            return reason
    return None


def _stop_reason(slots: list[dict[str, Any]]) -> str | None:
    return _first_str(slots, _STOP_REASON_KEYS) or _incomplete_reason(slots)


def stop_reason_of(result: LLMResult) -> str | None:
    """The provider's stop reason for the first generation, or ``None`` if absent.

    A ``finish_reason``/``stop_reason`` key wins; failing that, the Responses
    API's ``incomplete_details.reason`` (only ever present on an incomplete
    completion) is the stop reason — a completed Responses call reports none.
    """
    return _stop_reason(_metadata_slots(result))


def _int_or_none(value: object) -> int | None:
    # bool is an int subclass; a provider never reports a boolean count, so
    # treat one as absent rather than as 0/1 tokens.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class CompletionMetadata:
    """What one completion reported about itself: stop reason, size, model."""

    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    model: str | None

    @property
    def truncated(self) -> bool:
        return is_truncated(self.stop_reason)


def completion_metadata_of(result: LLMResult) -> CompletionMetadata:
    """Read stop reason, token counts and model name off ``result``.

    Token counts come from the message's ``usage_metadata`` (the langchain
    standard shape every client here populates); ``reasoning_tokens`` is the
    ``output_token_details.reasoning`` sub-count when the provider reports
    one, and is INCLUDED in ``output_tokens`` — the cap counts both.
    """
    generation = _first_generation(result)
    message = getattr(generation, "message", None)
    usage = getattr(message, "usage_metadata", None)
    input_tokens = output_tokens = reasoning_tokens = None
    if isinstance(usage, dict):
        input_tokens = _int_or_none(usage.get("input_tokens"))
        output_tokens = _int_or_none(usage.get("output_tokens"))
        details = usage.get("output_token_details")
        if isinstance(details, dict):
            reasoning_tokens = _int_or_none(details.get("reasoning"))
    slots = _metadata_slots(result)
    return CompletionMetadata(
        stop_reason=_stop_reason(slots),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        model=_first_str(slots, ("model_name", "model")),
    )
