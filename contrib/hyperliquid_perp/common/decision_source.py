"""The ``decision_source:`` block: WHICH ``ports.DecisionProvider`` a daemon runs.

Two providers (carry plan §2 D2):

- ``engine`` — the TradingAgents engine behind an LLM
  (``integration.decision_provider.EngineDecisionProvider``). The default,
  and what every run before this block existed was;
- ``file_target`` — the carry coordinator's handoff document, read by
  ``integration.file_target_provider`` and never shown to a model. The run
  needs no ``OPENROUTER_API_KEY``, and both CLI entry points read this block
  to know that before they demand one.

A top-level block, not keys inside ``decision:``, although the plan first
spelled it that way (§3.3): ``decision:`` is the DOMAIN's contract
(``domains.perp.target_decision.DecisionConfig`` — the margin grid, the
confidence bars — refuses unknown keys), and the domain must not know a file
or a coordinator exists (plan §1.2). Parsed here, at the bottom of the graph,
so ``config.py`` validates the block on every load without importing the
integration layer — its load-time import closure is pinned by
``tests/common/test_layering.py``, and this module is on that list by name.

Switching providers mid-run is config drift (``cli/_drift.py`` compares the
block parsed, so an absent block and an explicit ``provider: engine`` are the
same run) — warned on resume like any other behaviour change, never refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config_coercion import config_overrides, str_from_yaml
from .enum_guard import check_enum

__all__ = [
    "ENGINE_PROVIDER",
    "FILE_TARGET_PROVIDER",
    "PROVIDERS",
    "DecisionSourceConfig",
    "decision_source",
]

ENGINE_PROVIDER = "engine"
FILE_TARGET_PROVIDER = "file_target"
# The closed vocabulary ``provider`` is checked against; the refusal lists it.
PROVIDERS = (ENGINE_PROVIDER, FILE_TARGET_PROVIDER)


@dataclass(frozen=True)
class DecisionSourceConfig:
    """Typed view of the YAML ``decision_source:`` block.

    ``target_path`` is the handoff file the ``file_target`` provider reads —
    REQUIRED with that provider and REFUSED with the engine, where it would
    be dead config that reads as if the file mattered. Whether the file
    exists is not checked here: the coordinator writes it from its own
    schedule, and switching the provider on before its first run is the
    normal order; a missing file is a stale target at cycle time (a WARNING
    and a maintained position, plan §2 D5), not a load error. The provider
    does refuse a path whose DIRECTORY does not exist when it is built — a
    typo'd path would otherwise look exactly like a coordinator that never
    ran — but that is the daemon's startup, not the config load.
    """

    provider: str = ENGINE_PROVIDER
    target_path: str | None = None

    def __post_init__(self) -> None:
        check_enum(self.provider, PROVIDERS, name="decision_source.provider")
        if self.provider == FILE_TARGET_PROVIDER:
            path = self.target_path
            # Surrounding whitespace is refused rather than stripped, as the
            # autoresearch_signal path is: a path nobody will ever write to
            # has exactly the symptoms of a coordinator that never ran.
            if not path or path != path.strip():
                raise ValueError(
                    "decision_source.target_path must name the handoff file "
                    f"(no surrounding whitespace) when provider is {FILE_TARGET_PROVIDER!r}, "
                    f"got {path!r}"
                )
        elif self.target_path is not None:
            raise ValueError(
                f"decision_source.target_path is read only by the {FILE_TARGET_PROVIDER!r} "
                f"provider; drop it, or set provider: {FILE_TARGET_PROVIDER}"
            )

    @property
    def drives_engine(self) -> bool:
        """Whether a run under this block calls the LLM engine (and so needs its key)."""
        return self.provider == ENGINE_PROVIDER

    @classmethod
    def from_dict(cls, cfg: dict | None) -> DecisionSourceConfig:
        """Parse the raw YAML block; absent or null keys use the field defaults."""
        return cls(
            **config_overrides(cfg, {"provider": str_from_yaml, "target_path": str_from_yaml})
        )


def decision_source(config: Mapping[str, Any]) -> DecisionSourceConfig:
    """The block as a loaded config carries it; the engine default when absent."""
    return DecisionSourceConfig.from_dict(config.get("decision_source"))
