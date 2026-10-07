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

A failure while asking is sorted by whether a later visit may do better.
What the provider says is passing (a rate limit, a timeout, a server
error, an empty balance), what the network says (a connection or a
timeout error) and what a data vendor says (throttled, or down) is
:class:`~.errors.JudgeUnavailable`; everything else, from a model the
provider does not serve to any other error inside the engine, is an
:class:`~.errors.AgentError`, since asking again unchanged would meet it
again, and pay for the analysts' calls before it does.

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
from dataclasses import asdict, dataclass
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
PROMPT_VERSION: Final = "spot-context-v2"
#: What a verdict given by :class:`FakeJudge` records as its model: the
#: mark by which a stored verdict is known to be a rehearsal's.
FAKE_MODEL: Final = "fake"
#: The engine's ``final_state`` keys the sidecar keeps, in pipeline order:
#: the analysts' reports, the researchers' debate and its verdict, the
#: trader's plan, the risk debate, and the decision the rating is read from.
#: They spell the engine's ``AgentState`` fields; ``tests/test_upstream_names.py``
#: pins them to it. The fundamentals report is kept for the shape's sake and
#: is always ``None`` here (:mod:`.settings`).
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
# Provider statuses that say "later": an empty balance, a timeout, a rate limit.
_PASSING_STATUSES: Final = frozenset({402, 408, 429})
# Error classes that say "later", by name, matched anywhere in an error's class
# hierarchy: the network's and the provider SDKs' transport errors (the SDKs
# are not imported here, and the names are shared by openai, httpx, requests,
# urllib3 and curl_cffi, whose every error derives from ``CurlError``), and
# the engine's own "vendor throttled" and "vendor down" errors, which each
# vendor's throttle and outage errors derive from.
_PASSING_ERRORS: Final = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "CurlError",
        "MaxRetryError",
        "NewConnectionError",
        "PoolTimeout",
        "ProtocolError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "Timeout",
        "TimeoutError",
        "TimeoutException",
        "TransportError",
        "VendorRateLimitError",
        "VendorUnavailableError",
        "WriteError",
        "WriteTimeout",
    }
)


@dataclass(frozen=True)
class Answer:
    """What a judge said: the decision text the rating was read from, the rating, and the reports.

    ``reports`` are the engine's reports by :data:`REPORT_KEYS`, with
    ``selected_analysts`` beside them, kept in the sidecar; ``None`` for a
    judge that keeps no words. ``elapsed_seconds`` is how long the judge
    took.
    """

    decision: str
    rating: Rating
    reports: Mapping[str, object] | None
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.decision, str):
            raise ValueError(f"decision must be a string, got {self.decision!r}")
        if not isinstance(self.rating, Rating):
            raise ValueError(f"rating must be a Rating, got {self.rating!r}")
        if self.reports is not None and not isinstance(self.reports, Mapping):
            raise ValueError(f"reports must be a mapping or None, got {self.reports!r}")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, int | float)
            or self.elapsed_seconds < 0
        ):
            raise ValueError(
                f"elapsed_seconds must be a non-negative number, got {self.elapsed_seconds!r}"
            )


class Judge(Protocol):
    """Whoever answers a question about one ticker on one date."""

    @property
    def model(self) -> str:
        """What the stored verdict names as its model."""

    @property
    def settings(self) -> Mapping[str, object]:
        """What the sidecar keeps of how the judge was set up, beside its model; empty for none."""

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
    is switched off by giving it no path. Structured output is off, as the
    perp engine has it: the rating is read from the decision's text, which
    the free-text path gives as well, and the structured binding forces a
    tool choice that the Claude 5.5 models refuse (a 400 from each manager,
    then the same free-text fallback).
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
            "structured_output": False,
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
    imported or set up is an :class:`AgentError`, and so is a graph the
    engine refuses to build (a provider whose key is not in the
    environment). A graph is built per question, with that question's
    spot context on it. ``graph_class`` stands in for the engine's graph
    in tests.
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

    @property
    def settings(self) -> Mapping[str, object]:
        return asdict(self._settings)

    def _graph(self, context: str) -> Any:
        """A graph for one question, with ``context`` on it; the engine is imported on the first."""
        try:
            if self._graph_class is None:
                from tradingagents.graph.trading_graph import TradingAgentsGraph

                self._graph_class = _spot_graph(TradingAgentsGraph)
            if self._config is None:
                self._config = engine_config(self._settings, self._home)
        except Exception as exc:
            # An engine that is not installed, or one that refuses an environment
            # override (TRADINGAGENTS_*) as it imports.
            raise AgentError(
                f"the tradingagents engine cannot be set up ({type(exc).__name__}: {exc})"
            ) from exc
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
            if _may_pass(exc):
                raise JudgeUnavailable(
                    f"the judge did not answer on {ticker} ({type(exc).__name__}: {exc})"
                ) from exc
            raise AgentError(
                f"the judge failed on {ticker} for good ({type(exc).__name__}: {exc}); asking "
                f"again unchanged will not help"
            ) from exc
        return _answer(
            propagated, ticker, self._settings.selected_analysts, _time.monotonic() - started
        )


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status an error carries, when it carries one.

    The provider SDKs spell it ``status_code`` or ``code`` on the error;
    ``requests`` and ``httpx`` keep it on the error's ``response``. Only a
    number an error can carry, 400 to 599, is a status: curl's error
    numbers ride on a ``code`` too, and reach 100 and beyond.
    """
    for owner in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "code"):
            status = getattr(owner, name, None)
            if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
                return status
    return None


def _may_pass(exc: BaseException) -> bool:
    """Whether ``exc``, or what caused it, is a failure a later visit may not meet.

    A provider's status is read first: a server error, a rate limit, a
    timeout or an empty balance may pass, and any other 4xx (a model it
    does not serve, a key it does not accept, a request it cannot read)
    will not. Without a status, an error of the network's or the SDK's
    transport, or a data vendor's throttle or outage, known by a class
    name in its hierarchy, may pass; anything else is taken to be the
    engine's own, and permanent. The chain is followed through causes, and through the
    context of an error only where that context was not suppressed
    (``raise ... from None``).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = _status_of(current)
        if status is not None:
            return status >= 500 or status in _PASSING_STATUSES
        if any(cls.__name__ in _PASSING_ERRORS for cls in type(current).__mro__):
            return True
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
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
    settings: Mapping[str, object] = {}
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
