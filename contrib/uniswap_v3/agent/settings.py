"""Which judge the agent layer asks: the config's ``agent`` section, read into frozen values.

::

    agent:                      # optional, and so is each key in it
      llm_provider: openrouter
      deep_think_llm: "anthropic/claude-sonnet-5.5"
      quick_think_llm: "deepseek/deepseek-chat"
      selected_analysts: [market, social, news]
      max_tokens: 16384
      ask_within_seconds: 14400
      news_patience_seconds: 7200

The defaults are the models the Hyperliquid paper run is judged by, so
that the two scorecards compare. ``llm_provider`` is a provider the
engine's client registry knows (``openrouter``, ``anthropic``, ``openai``
and so on); its API key is read from the environment variable the engine
names for it (``OPENROUTER_API_KEY`` for OpenRouter), never from a config.
``selected_analysts`` are the analysts the graph runs before its debate,
any of ``market``, ``social`` and ``news``; the engine's fourth, the
fundamentals analyst, reads a company's statements and has no crypto
branch, so it is refused here, where every ticker is a crypto asset.
``max_tokens`` caps every completion: a gateway asked for no cap may
refuse every call, so a cap is always sent; it counts the thinking tokens
of a model that thinks (the default deep model does, and cannot switch it
off), and a cap that binds shows as answers with no rating.
``ask_within_seconds`` is how long after a bar's boundary the judge may still be asked about it: it
reads news and prices through the moment it is asked, so a late question
sees hours past the fill the run trades at, which the control run never
does. Past the window the tokens not yet judged are left unrated, while
verdicts already stored stand (the default, four hours, covers the
scheduled visit and its two retries; the least is 3600 seconds, which the
first visit's verdict, asked 10 to 35 minutes after the boundary, always
clears, so a typo cannot leave every bar unrated); a fake rating is not
bound by it. ``news_patience_seconds`` is how long after the boundary the
judge keeps saying "later" when the news input is unavailable (#408): the
``verdict`` command probes the news vendor before asking the graph and,
inside the patience, records nothing and spends nothing when the probe
finds it down; past the patience, the graph is asked anyway and its
answer is kept with ``news_missing`` set, so a bar whose news never came
back still gets a verdict on the last visit instead of none. Left unset
it is two hours, or an hour less than ``ask_within_seconds`` when that is
shorter, so the last hourly visit inside the window (02:10 UTC under the
defaults) is the last chance; given, it must be less than
``ask_within_seconds``, and zero means a news-less answer is kept at once.
The probe asks both news tools the graph has (the ticker's news and the
global news), so an outage of either is "later" before any model call; a
news failure that will not pass (a key not set, a vendor not configured)
is "for good" from the probe, as it was from the graph.

None of this enters a run's config snapshot: the verdicts a run reads are
data in the store, which record the model that gave each one, and which
judge is asked next changes no decision already made. The sidecar of each
verdict keeps the settings its judge ran under.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..domain.verdicts import require_count, require_text

__all__ = ["ANALYSTS", "AgentSettings"]

# The analysts the engine's graph can be set up with for a crypto asset, in its own order.
ANALYSTS: Final = ("market", "social", "news")
_STOCK_ONLY: Final = "fundamentals"


@dataclass(frozen=True)
class AgentSettings:
    """Provider, models, analysts and completion cap of the judge."""

    llm_provider: str = "openrouter"
    deep_think_llm: str = "anthropic/claude-sonnet-5.5"
    quick_think_llm: str = "deepseek/deepseek-chat"
    selected_analysts: tuple[str, ...] = ("market", "social", "news")
    max_tokens: int = 16384
    ask_within_seconds: int = 14_400
    news_patience_seconds: int | None = None

    def __post_init__(self) -> None:
        require_text(self.llm_provider, "llm_provider")
        require_text(self.deep_think_llm, "deep_think_llm")
        require_text(self.quick_think_llm, "quick_think_llm")
        analysts = self.selected_analysts
        if isinstance(analysts, list):
            analysts = tuple(analysts)
        if not isinstance(analysts, tuple) or not analysts:
            raise ValueError(
                f"selected_analysts must be a non-empty list of analyst names, got "
                f"{self.selected_analysts!r}"
            )
        for analyst in analysts:
            if analyst == _STOCK_ONLY:
                raise ValueError(
                    f"selected_analysts names {analyst!r}, and the engine's fundamentals "
                    f"analyst reads a company's statements, which a crypto asset has none of; "
                    f"the analysts are {list(ANALYSTS)}"
                )
            if analyst not in ANALYSTS:
                raise ValueError(
                    f"selected_analysts names {analyst!r}, and the analysts are {list(ANALYSTS)}"
                )
        if len(set(analysts)) != len(analysts):
            raise ValueError(f"selected_analysts names an analyst twice: {list(analysts)}")
        object.__setattr__(self, "selected_analysts", analysts)
        require_count(self.max_tokens, "max_tokens", at_least=1)
        require_count(self.ask_within_seconds, "ask_within_seconds", at_least=3600)
        patience = self.news_patience_seconds
        if patience is None:
            # Two hours, or an hour less than the window when the window is
            # shorter: the last hourly visit inside the window is lenient
            # either way, so a bar always has its last chance.
            patience = min(7_200, self.ask_within_seconds - 3_600)
            object.__setattr__(self, "news_patience_seconds", patience)
        require_count(patience, "news_patience_seconds", at_least=0)
        if patience >= self.ask_within_seconds:
            raise ValueError(
                f"news_patience_seconds must be less than ask_within_seconds "
                f"({self.ask_within_seconds}), got {patience}"
            )

    @property
    def patience_seconds(self) -> int:
        """``news_patience_seconds`` as resolved: an int once the settings are built."""
        assert self.news_patience_seconds is not None
        return self.news_patience_seconds
