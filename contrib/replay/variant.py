"""A variant: one brain the past papers are put to, described as data (replay plan §3-5).

A variant is a YAML file, not code::

    name: current-sonnet
    model:
      provider: openrouter
      id: anthropic/claude-sonnet-4-6
    system_prompt_path: current_system.md   # relative to this file
    temperature: 0.2                        # optional; absent = the provider's own default
    max_tokens: 8192                        # optional; absent = the perp daemon's cap (DEFAULT_MAX_TOKENS)
    extra_context: |                        # optional; one more section after the market
      A lesson learned ...                  #   context, before the format block
    model_cutoff: 2025-03-31                # optional; the model's training cutoff (plan §6)

Unknown keys are refused rather than ignored: a typo'd ``temprature`` would
otherwise ask at the provider's default and record the variant as if it had
been asked at the value the author meant.

**Two modes** (plan PR 3b). The default, ``mode: simple``, is the past
papers' one completion per question (``replay``). ``mode: graph`` is the
lower half of the paper trader's graph re-run from the recorded reports,
question by question from a simulated account's own book
(``backtest --ask``)::

    name: graph-current
    mode: graph
    model:                                  # the deep model: the portfolio manager
      provider: openrouter
      id: anthropic/claude-sonnet-4-6
    quick_model:                            # optional; the trader and the three risk
      provider: openrouter                  #   debators; absent = the deep model for all
      id: qwen/qwen3-235b-a22b-2507
    guardrail: recorded                     # recorded (default) | off | rule
    # system_prompt_path is OPTIONAL here: the portfolio manager's prompt is
    # the engine's own; a prompt given rides as that completion's system message.

``guardrail`` says what the gate reads from the trend guardrail at each
question: ``recorded`` is the side the input row recorded (``None`` with the
guardrail off, the gate the daemon ran); ``off`` is no guardrail; ``rule``
replays the research package's rule over the research store and reads its
side at each question (the ``guardrail`` report's own timeline). Both
graph-only keys are refused on a simple variant.

**Identity.** :attr:`Variant.sha` is a digest of what reaches the model:
the provider and model id, the system prompt's TEXT (not its path), the
temperature, the completion cap and the extra context; a graph variant's
digest also carries its mode, its quick model and its guardrail setting (a
simple variant's digest is the same as before these existed). Change any
of them and it is a new variant, which needs a new name; it is stored
beside the old one, never over it. The name is a label for people, held
one-to-one with the sha by the store. ``model_cutoff`` is a fact about the
model rather than something it is shown, so it stays out of the digest:
correcting it does not throw away the answers already paid for.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import yaml

from .upstream import epoch_ms

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "GRAPH_MODE",
    "GUARDRAIL_MODES",
    "GUARDRAIL_OFF",
    "GUARDRAIL_RECORDED",
    "GUARDRAIL_RULE",
    "MODES",
    "SIMPLE_MODE",
    "Variant",
    "VariantError",
    "load_variant",
    "short_sha",
]

# The perp daemon's completion cap when neither its YAML nor the environment
# sets one (``engine_bridge._DEFAULT_MAX_COMPLETION_TOKENS``): a variant that
# names none is asked under the cap the paper trader is. A variant that
# replays a run recorded under an older cap names that cap itself (the
# shipped ``current-sonnet`` does: 8192, the cap of paper-BTC-6 and -7).
DEFAULT_MAX_TOKENS: Final = 16384

SIMPLE_MODE: Final = "simple"
GRAPH_MODE: Final = "graph"
MODES: Final = (SIMPLE_MODE, GRAPH_MODE)

GUARDRAIL_RECORDED: Final = "recorded"
GUARDRAIL_OFF: Final = "off"
GUARDRAIL_RULE: Final = "rule"
GUARDRAIL_MODES: Final = (GUARDRAIL_RECORDED, GUARDRAIL_OFF, GUARDRAIL_RULE)

_KEYS: Final = frozenset(
    {
        "name",
        "mode",
        "model",
        "quick_model",
        "guardrail",
        "system_prompt_path",
        "temperature",
        "max_tokens",
        "extra_context",
        "model_cutoff",
    }
)
_REQUIRED: Final = frozenset({"name", "model"})
_GRAPH_ONLY: Final = frozenset({"quick_model", "guardrail"})
_MODEL_KEYS: Final = frozenset({"provider", "id"})


class VariantError(ValueError):
    """A variant that cannot be used, and the sentence says which key."""


def short_sha(sha: str) -> str:
    """The first twelve hex digits of a ``sha256:<hex>`` digest: how a report names a variant."""
    return sha.removeprefix("sha256:")[:12]


@dataclass(frozen=True)
class Variant:
    """One variant, its file already read and checked.

    ``system_prompt`` is empty only on a graph variant that names no prompt
    file: the portfolio manager then runs on the engine's own prompt alone.
    ``quick_provider`` and ``quick_model`` are set together or not at all,
    and only on a graph variant.
    """

    name: str
    provider: str
    model: str
    system_prompt: str
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    extra_context: str | None = None
    model_cutoff: date | None = None
    mode: str = SIMPLE_MODE
    quick_provider: str | None = None
    quick_model: str | None = None
    guardrail: str = GUARDRAIL_RECORDED

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise VariantError(f"mode must be one of {list(MODES)}, got {self.mode!r}")
        for field in ("name", "provider", "model"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise VariantError(f"{field} must be a non-empty string, got {value!r}")
        if not isinstance(self.system_prompt, str) or (
            not self.system_prompt.strip() and self.mode != GRAPH_MODE
        ):
            raise VariantError(
                f"system_prompt must be a non-empty string, got {self.system_prompt!r}"
            )
        if self.system_prompt and not self.system_prompt.strip():
            # Whitespace would reach the model as a system message and hash into
            # the sha while behaving like no prompt: one setting, two shas.
            raise VariantError("a graph variant's system_prompt is text, or empty for none")
        if self.temperature is not None:
            # The engine's own rule (``default_config``'s temperature check):
            # finite and non-negative, no ceiling, since the ceiling is the
            # provider's to enforce and differs between providers.
            if (
                isinstance(self.temperature, bool)
                or not isinstance(self.temperature, int | float)
                or not math.isfinite(self.temperature)
                or self.temperature < 0
            ):
                raise VariantError(
                    f"temperature must be a finite, non-negative number, got {self.temperature!r}"
                )
            object.__setattr__(self, "temperature", float(self.temperature))
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1:
            raise VariantError(f"max_tokens must be a positive integer, got {self.max_tokens!r}")
        if self.extra_context is not None and (
            not isinstance(self.extra_context, str) or not self.extra_context.strip()
        ):
            raise VariantError(
                f"extra_context must be non-empty text when present, got {self.extra_context!r}"
            )
        # ``datetime`` is a ``date`` subclass; a timestamp here would be a
        # cutoff at some hour, which the day rule below cannot honour.
        if self.model_cutoff is not None and (
            isinstance(self.model_cutoff, datetime) or not isinstance(self.model_cutoff, date)
        ):
            raise VariantError(f"model_cutoff must be a date (YYYY-MM-DD), got {self.model_cutoff!r}")
        if (self.quick_provider is None) != (self.quick_model is None):
            raise VariantError("quick_model names a provider and an id together, or neither")
        for field in ("quick_provider", "quick_model"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise VariantError(f"{field} must be a non-empty string, got {value!r}")
        if self.guardrail not in GUARDRAIL_MODES:
            raise VariantError(
                f"guardrail must be one of {list(GUARDRAIL_MODES)}, got {self.guardrail!r}"
            )
        if self.mode != GRAPH_MODE:
            if self.quick_model is not None:
                raise VariantError("quick_model is a graph variant's key (mode: graph)")
            if self.guardrail != GUARDRAIL_RECORDED:
                raise VariantError(
                    "guardrail is a graph variant's key (mode: graph); the simple replay gates "
                    "under the reading the input row recorded"
                )

    @property
    def has_prompt(self) -> bool:
        """Whether a system prompt reaches the model (a graph variant may name none)."""
        return bool(self.system_prompt.strip())

    @property
    def sha(self) -> str:
        """``sha256:<hex>`` over what reaches the model (module docstring), as canonical JSON."""
        identity: dict[str, object] = {
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_context": self.extra_context,
        }
        if self.mode == GRAPH_MODE:
            # Added only here, so every simple variant keeps the digest it
            # was stored under before graph variants existed.
            identity["mode"] = self.mode
            identity["quick_model"] = (
                None
                if self.quick_model is None
                else {"provider": self.quick_provider, "id": self.quick_model}
            )
            identity["guardrail"] = self.guardrail
        canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @property
    def short_sha(self) -> str:
        return short_sha(self.sha)

    @property
    def cutoff_ms(self) -> int | None:
        """The first instant after the cutoff day (the next day's UTC midnight), or ``None``.

        A question decided at or after it is past the cutoff (kept by default
        in ``score --replay-db``): the model's training data cannot have held
        the price that followed (plan §6).
        """
        if self.model_cutoff is None:
            return None
        start = datetime.combine(
            self.model_cutoff + timedelta(days=1), datetime.min.time(), timezone.utc
        )
        return epoch_ms(start, what="model_cutoff")

    def describe(self) -> str:
        """One line naming the variant: the header of every report about its answers."""
        shown = "provider default" if self.temperature is None else f"{self.temperature:g}"
        line = (
            f"variant {self.name} ({self.short_sha}): {self.provider}/{self.model}, temperature "
            f"{shown}, max_tokens {self.max_tokens}"
            + (", with extra context" if self.extra_context is not None else "")
        )
        if self.mode == GRAPH_MODE:
            quick = (
                "the same model"
                if self.quick_model is None
                else f"{self.quick_provider}/{self.quick_model}"
            )
            prompt = (
                "the file's prompt as the portfolio manager's system message"
                if self.has_prompt
                else "the engine's own portfolio manager prompt"
            )
            line += (
                f"; graph mode: trader and risk debators on {quick}, {prompt}, guardrail "
                f"{self.guardrail}"
            )
        return line


def _model_block(document: dict, key: str) -> tuple[str, str]:
    model = document[key]
    if not isinstance(model, dict) or set(model) != _MODEL_KEYS:
        raise VariantError(f"{key} must be a mapping with exactly {sorted(_MODEL_KEYS)}, got {model!r}")
    return model["provider"], model["id"]


def load_variant(path: Path) -> Variant:
    """Read and check the variant file at ``path``; the system prompt is read relative to it."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VariantError(f"variant file {str(path)!r} cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise VariantError(f"variant file {str(path)!r} is not YAML ({exc})") from exc
    if not isinstance(document, dict):
        raise VariantError(f"variant file {str(path)!r} must hold a mapping")
    unknown = set(document) - _KEYS
    if unknown:
        raise VariantError(f"unknown variant key(s) {sorted(unknown)}; allowed: {sorted(_KEYS)}")
    mode = document.get("mode", SIMPLE_MODE)
    if mode not in MODES:
        raise VariantError(f"mode must be one of {list(MODES)}, got {mode!r}")
    required = _REQUIRED if mode == GRAPH_MODE else _REQUIRED | {"system_prompt_path"}
    missing = required - set(document)
    if missing:
        raise VariantError(f"variant file {str(path)!r} lacks {sorted(missing)}")
    if mode != GRAPH_MODE:
        stray = sorted(_GRAPH_ONLY & set(document))
        if stray:
            raise VariantError(f"{', '.join(stray)} only apply to a graph variant (mode: graph)")
    provider, model = _model_block(document, "model")
    quick_provider = quick_model = None
    if "quick_model" in document:
        quick_provider, quick_model = _model_block(document, "quick_model")
    # YAML 1.1 reads a bare ``off`` as the boolean False (and ``on`` as True):
    # the former is the mode the author meant, the latter is no mode.
    guardrail = document.get("guardrail", GUARDRAIL_RECORDED)
    if guardrail is False:
        guardrail = GUARDRAIL_OFF
    elif guardrail is True:
        raise VariantError(
            f"guardrail: on is not a mode; use one of {list(GUARDRAIL_MODES)} (quote 'off' or "
            "write it bare: YAML reads a bare off as a boolean, which is taken as the off mode)"
        )
    system_prompt = ""
    if "system_prompt_path" in document:
        prompt_ref = document["system_prompt_path"]
        if not isinstance(prompt_ref, str) or not prompt_ref:
            raise VariantError(f"system_prompt_path must be a path, got {prompt_ref!r}")
        prompt_path = path.parent / prompt_ref
        try:
            system_prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VariantError(f"system prompt {str(prompt_path)!r} cannot be read ({exc})") from exc
        if not system_prompt.strip():
            # Said for both modes: a graph variant that wants the engine's own
            # prompt leaves the key out rather than pointing at an empty file,
            # else one setting could stand under two shas.
            raise VariantError(f"system prompt {str(prompt_path)!r} is empty")
    return Variant(
        name=document["name"],
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        temperature=document.get("temperature"),
        max_tokens=document.get("max_tokens", DEFAULT_MAX_TOKENS),
        extra_context=document.get("extra_context"),
        model_cutoff=document.get("model_cutoff"),
        mode=mode,
        quick_provider=quick_provider,
        quick_model=quick_model,
        guardrail=guardrail,
    )
