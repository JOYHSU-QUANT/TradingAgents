"""A variant: one brain the past papers are put to, described as data (replay plan §3-5).

A variant is a YAML file, not code::

    name: current-sonnet
    model:
      provider: openrouter
      id: anthropic/claude-sonnet-4-6
    system_prompt_path: current_system.md   # relative to this file
    temperature: 0.2                        # optional; absent = the provider's own default
    max_tokens: 8192                        # optional; the perp daemon's completion cap
    extra_context: |                        # optional; one more section after the market
      A lesson learned ...                  #   context, before the format block
    model_cutoff: 2025-03-31                # optional; the model's training cutoff (plan §6)

Unknown keys are refused rather than ignored: a typo'd ``temprature`` would
otherwise ask at the provider's default and record the variant as if it had
been asked at the value the author meant.

**Identity.** :attr:`Variant.sha` is a digest of what reaches the model:
the provider and model id, the system prompt's TEXT (not its path), the
temperature, the completion cap and the extra context. Change any of them
and it is a new variant, which needs a new name; it is stored beside the
old one, never over it. The
name is a label for people, held one-to-one with the sha by the store.
``model_cutoff`` is a fact about the model rather than something it is
shown, so it stays out of the digest: correcting it does not throw away
the answers already paid for.
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
    "Variant",
    "VariantError",
    "load_variant",
    "short_sha",
]

# The perp daemon's completion cap when neither its YAML nor the environment
# sets one (``engine_bridge._DEFAULT_MAX_COMPLETION_TOKENS``): a variant that
# names none is asked under the cap the paper trader was.
DEFAULT_MAX_TOKENS: Final = 8192

_KEYS: Final = frozenset(
    {"name", "model", "system_prompt_path", "temperature", "max_tokens", "extra_context", "model_cutoff"}
)
_REQUIRED: Final = frozenset({"name", "model", "system_prompt_path"})
_MODEL_KEYS: Final = frozenset({"provider", "id"})


class VariantError(ValueError):
    """A variant that cannot be used, and the sentence says which key."""


def short_sha(sha: str) -> str:
    """The first twelve hex digits of a ``sha256:<hex>`` digest: how a report names a variant."""
    return sha.removeprefix("sha256:")[:12]


@dataclass(frozen=True)
class Variant:
    """One variant, its file already read and checked."""

    name: str
    provider: str
    model: str
    system_prompt: str
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    extra_context: str | None = None
    model_cutoff: date | None = None

    def __post_init__(self) -> None:
        for field in ("name", "provider", "model", "system_prompt"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise VariantError(f"{field} must be a non-empty string, got {value!r}")
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

    @property
    def sha(self) -> str:
        """``sha256:<hex>`` over what reaches the model (module docstring), as canonical JSON."""
        identity = {
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_context": self.extra_context,
        }
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
        return (
            f"variant {self.name} ({self.short_sha}): {self.provider}/{self.model}, temperature "
            f"{shown}, max_tokens {self.max_tokens}"
            + (", with extra context" if self.extra_context is not None else "")
        )


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
    missing = _REQUIRED - set(document)
    if missing:
        raise VariantError(f"variant file {str(path)!r} lacks {sorted(missing)}")
    model = document["model"]
    if not isinstance(model, dict) or set(model) != _MODEL_KEYS:
        raise VariantError(f"model must be a mapping with exactly {sorted(_MODEL_KEYS)}, got {model!r}")
    prompt_ref = document["system_prompt_path"]
    if not isinstance(prompt_ref, str) or not prompt_ref:
        raise VariantError(f"system_prompt_path must be a path, got {prompt_ref!r}")
    prompt_path = path.parent / prompt_ref
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantError(f"system prompt {str(prompt_path)!r} cannot be read ({exc})") from exc
    return Variant(
        name=document["name"],
        provider=model["provider"],
        model=model["id"],
        system_prompt=system_prompt,
        temperature=document.get("temperature"),
        max_tokens=document.get("max_tokens", DEFAULT_MAX_TOKENS),
        extra_context=document.get("extra_context"),
        model_cutoff=document.get("model_cutoff"),
    )
