"""The judge: the TradingAgents graph asked about one ticker, and a fake that answers without it.

:class:`TradingAgentsJudge` builds the engine's graph over the config's
:class:`~.settings.AgentSettings` laid over the engine's own defaults, with
the spot context (:mod:`.context`) appended to the instrument context the
graph hands every agent, and reads the rating the graph itself extracts
from the portfolio manager's decision. The engine is imported here and
nowhere else in the package, and only when a question is first asked.

The engine writes as it runs: a JSON log of each run's state under its
``results_dir``, and a data cache. Both are pointed into ``home``, a
directory of this package's beside the store, so that nothing lands in the
user's ``~/.tradingagents``. Its memory log, which would carry one day's
decision into the next day's prompt, is left off: each verdict is the
graph's answer to that day alone, as the Hyperliquid paper run's are.

A judge says two things about itself beside its ``model``: whether it is a
``rehearsal``, answering without a model, whose verdicts must not mix
with real ones; and whether it is ``point_in_time``, its verdict on a bar
depending on nothing after the bar. The engine's graph is neither: it
reads news and prices through the day it is asked on, so only the latest
bar gets an honest verdict from it. :class:`FakeJudge` is both: it
answers every question with one rating and keeps no words, for
``verdict --fake-rating`` and for tests.
"""

from __future__ import annotations

import time as _time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from ..domain.verdicts import Rating
from .errors import AgentError, JudgeUnavailable
from .settings import AgentSettings

__all__ = [
    "FAKE_MODEL",
    "PROMPT_VERSION",
    "REPORT_KEYS",
    "Answer",
    "FakeJudge",
    "Judge",
    "TradingAgentsJudge",
    "engine_config",
]

#: Which contract the verdicts were asked under: the spot context's wording
#: and what is read back. Bump it when either changes.
PROMPT_VERSION: Final = "spot-context-v1"
#: What a verdict given by :class:`FakeJudge` records as its model: the
#: mark by which a stored verdict is known to be a rehearsal's.
FAKE_MODEL: Final = "fake"
#: The engine's ``final_state`` keys the sidecar keeps, in pipeline order:
#: the analysts' reports, the researchers' debate and its verdict, the
#: trader's plan, the risk debate, and the decision the rating is read from.
#: They spell the engine's ``AgentState`` fields; ``tests/test_upstream_names.py``
#: pins them to it.
REPORT_KEYS: Final = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_debate_state",
    "investment_plan",
    "trader_investment_plan",
    "risk_debate_state",
    "final_trade_decision",
)
_DECISION_KEY: Final = "final_trade_decision"
#: The engine's ``propagate`` argument that selects its crypto pipeline.
_ASSET_TYPE: Final = "crypto"
# Provider status codes that say "later": a timeout and a rate limit.
_LATER: Final = frozenset({408, 429})


@dataclass(frozen=True)
class Answer:
    """What a judge said: the decision text the rating was read from, the rating, and the reports.

    ``reports`` are the engine's reports by :data:`REPORT_KEYS`, kept in the
    sidecar; ``None`` for a judge that keeps no words. ``elapsed_seconds``
    is how long the judge took.
    """

    decision: str
    rating: Rating
    reports: Mapping[str, object] | None
    elapsed_seconds: float


class Judge(Protocol):
    """Whoever answers a question about one ticker on one date."""

    @property
    def model(self) -> str:
        """What the stored verdict names as its model."""

    @property
    def rehearsal(self) -> bool:
        """Whether the judge answers without a model, so its verdicts must not mix with real ones."""

    @property
    def point_in_time(self) -> bool:
        """Whether a verdict on a bar depends on nothing after the bar, so a past bar may be judged."""

    def ask(self, ticker: str, trade_date: str, context: str) -> Answer:
        """The verdict on ``ticker`` as of ``trade_date`` (``YYYY-MM-DD``), given ``context``."""


def engine_config(settings: AgentSettings, home: Path) -> dict[str, Any]:
    """The engine's config: its defaults, with the judge's settings and this package's directories over them.

    ``backend_url`` is left to the provider's own endpoint. The memory log
    is switched off by giving it no path.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "llm_provider": settings.llm_provider,
            "deep_think_llm": settings.deep_think_llm,
            "quick_think_llm": settings.quick_think_llm,
            "backend_url": None,
            "max_tokens": settings.max_tokens,
            "results_dir": str(home / "logs"),
            "data_cache_dir": str(home / "cache"),
            "memory_log_path": None,
        }
    )
    return config


def _spot_graph(base: type) -> type:
    """``base``, the engine's graph, with the spot context on the instance appended to its instrument context."""

    class SpotGraph(base):  # type: ignore[misc,valid-type]
        spot_context: str = ""

        def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
            context = super().resolve_instrument_context(ticker, asset_type)
            if self.spot_context:
                context = f"{context}\n\n## Spot market context\n{self.spot_context}"
            return context

    return SpotGraph


class TradingAgentsJudge:
    """The TradingAgents graph, built over ``settings``, asked about one ticker at a time.

    The engine is imported on the first question; an engine that cannot be
    imported is an :class:`AgentError`, and so is a graph the engine
    refuses to build (a provider whose key is not in the environment). A
    graph is built per question, with that question's spot context on it.
    ``graph_class`` stands in for the engine's graph in tests.
    """

    rehearsal = False
    point_in_time = False

    def __init__(
        self, settings: AgentSettings, home: Path, *, graph_class: type | None = None
    ) -> None:
        self._settings = settings
        self._home = home
        self._graph_class = None if graph_class is None else _spot_graph(graph_class)
        self._config: dict[str, Any] | None = None

    @property
    def model(self) -> str:
        return self._settings.deep_think_llm

    def _graph(self, context: str) -> Any:
        """A graph for one question, with ``context`` on it; the engine is imported on the first."""
        if self._graph_class is None:
            try:
                from tradingagents.graph.trading_graph import TradingAgentsGraph
            except ImportError as exc:
                raise AgentError(
                    f"the tradingagents engine cannot be imported ({exc}); is the package "
                    f"installed with its dependencies?"
                ) from exc
            self._graph_class = _spot_graph(TradingAgentsGraph)
        if self._config is None:
            self._config = engine_config(self._settings, self._home)
        try:
            graph = self._graph_class(
                selected_analysts=list(self._settings.selected_analysts),
                debug=False,
                config=self._config,
            )
        except Exception as exc:
            # A missing key or an unknown provider: the engine refuses before any call.
            raise AgentError(f"the judge cannot be built ({type(exc).__name__}: {exc})") from exc
        graph.spot_context = context
        return graph

    def ask(self, ticker: str, trade_date: str, context: str) -> Answer:
        started = _time.monotonic()
        graph = self._graph(context)
        try:
            propagated = graph.propagate(ticker, trade_date, asset_type=_ASSET_TYPE)
        except Exception as exc:
            if _refused_for_good(exc):
                raise AgentError(
                    f"the judge's provider refused the question on {ticker} for good "
                    f"({type(exc).__name__}: {exc}); asking again unchanged will not help"
                ) from exc
            raise JudgeUnavailable(
                f"the judge did not answer on {ticker} ({type(exc).__name__}: {exc})"
            ) from exc
        return _answer(
            propagated, ticker, self._settings.selected_analysts, _time.monotonic() - started
        )


def _refused_for_good(exc: BaseException) -> bool:
    """Whether ``exc``, or what caused it, is a provider's 4xx that a retry will meet again.

    A model the provider does not serve (404), a key it does not accept
    (401, 403) and a request it cannot read (400) come back the same every
    time; a rate limit (429) and a timeout (408) do not. The status is read
    off the exception chain, as the provider SDKs and langchain raise it,
    with ``status_code`` on the error.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return 400 <= status < 500 and status not in _LATER
        current = current.__cause__ or current.__context__
    return False


def _answer(
    propagated: object, ticker: str, selected_analysts: Sequence[str], elapsed: float
) -> Answer:
    """Read the engine's ``(final_state, signal)`` into an :class:`Answer`; another shape is refused."""
    if not isinstance(propagated, tuple | list) or len(propagated) != 2:
        raise AgentError(
            f"the judge answered on {ticker} in a shape that cannot be read "
            f"({type(propagated).__name__})"
        )
    final_state, signal = propagated
    if not isinstance(final_state, Mapping) or not isinstance(
        final_state.get(_DECISION_KEY), str
    ):
        raise AgentError(f"the judge's answer on {ticker} holds no {_DECISION_KEY} text")
    try:
        rating = Rating(signal)
    except ValueError:
        raise AgentError(
            f"the judge's signal on {ticker} is {signal!r}, and a rating is one of "
            f"{[rating.value for rating in Rating]}"
        ) from None
    reports: dict[str, object] = {"selected_analysts": list(selected_analysts)}
    reports.update({key: final_state.get(key) for key in REPORT_KEYS})
    return Answer(
        decision=final_state[_DECISION_KEY],
        rating=rating,
        reports=reports,
        elapsed_seconds=elapsed,
    )


class FakeJudge:
    """Answers every question with ``rating`` at once, keeping no words."""

    model = FAKE_MODEL
    rehearsal = True
    point_in_time = True

    def __init__(self, rating: Rating) -> None:
        if not isinstance(rating, Rating):
            raise ValueError(f"rating must be a Rating, got {rating!r}")
        self.rating = rating
        self.asked: list[tuple[str, str, str]] = []

    def ask(self, ticker: str, trade_date: str, context: str) -> Answer:
        self.asked.append((ticker, trade_date, context))
        return Answer(
            decision=f"Rating: {self.rating.value} (a fake verdict; no model was asked)",
            rating=self.rating,
            reports=None,
            elapsed_seconds=0.0,
        )
