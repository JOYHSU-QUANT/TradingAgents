"""The judge: the TradingAgents graph asked about one ticker, and a fake that answers without it.

:class:`TradingAgentsJudge` builds the engine's graph over the config's
:class:`~.settings.AgentSettings` laid over the engine's own defaults, with
the spot context (:mod:`.context`) appended to the instrument context the
graph hands every agent, and reads the rating off the ``Rating:`` line the
portfolio manager's decision ends with (:data:`_RATING_LINE`). The engine is
imported here and nowhere else in the package, and only when a question is
first asked.

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

import logging
import re
import time as _time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
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
PROMPT_VERSION: Final = "spot-context-v3"
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
#: The ``Rating: <rating>`` line the spot context asks the decision to end with,
#: as markdown may dress it (``**Rating**: Hold``, ``**Final rating: Hold**``,
#: ```Rating: Hold```, a fullwidth colon), with one word and nothing but
#: dressing after it (so neither ``Rating: Buy/Hold`` nor ``Rating: Buy (weak)``
#: is read as Buy); the last such line is the rating. The engine's own signal
#: is not read: its free-text reader falls back to the first rating word
#: anywhere in the text, which reads a decision cut short, or written
#: otherwise, as a direction.
_RATING_LINE: Final = re.compile(
    r"^[\s*_#>`-]*(?:final\s+)?rating[\s*_`]*[:\-：][\s*_`]*([A-Za-z]+)[\s*_`.]*$",
    re.IGNORECASE | re.MULTILINE,
)

logger = logging.getLogger(__name__)
#: The engine's ``propagate`` argument that selects its crypto pipeline.
_ASSET_TYPE: Final = "crypto"
# The engine's router degrades a news vendor's failure to a sentinel the
# analysts read on (news is an optional category since #393) and logs this
# line for it. The perp daemon wants exactly that; a verdict asked once per
# bar and never rewritten does not: a bar judged on a news-less graph would
# stand for good, where the raise the router used to make let a later visit
# ask again. So the judge watches the router's logger for the line while the
# graph runs and answers "later" if it fired — inside the caller's patience;
# past it the answer is kept with ``news_missing`` set, and the ``verdict``
# command probes the two news tools before any graph runs, so that "later"
# usually costs no model call (#408). The log line is the one trace both
# news paths leave: the news analyst's tool result lives in messages the
# graph clears between analysts, and the sentiment analyst reads the same
# getter directly, outside any tool node.
_ROUTER_LOGGER: Final = "tradingagents.dataflows.interface"
# The line's wording, kept for the test that drives the real router and
# reads the message back; the watch itself keys on the attribute below.
_NEWS_UNAVAILABLE: Final = "Optional news_data unavailable"
# The record attribute the router stamps the category on (#399,
# ``interface.UNAVAILABLE_CATEGORY_ATTR``): the watch keys on this field and
# the category's name, never on the line's words. A literal for the same
# reason as ``_ROUTER_LOGGER``; the test beside it pins both to the module.
_UNAVAILABLE_ATTR: Final = "unavailable_category"
_NEWS_CATEGORY: Final = "news_data"


class _NewsWatch(logging.Handler):
    """Collects the router's "news unavailable" lines emitted while attached.

    Each line is kept with the failure the router logged it for (the
    record's last argument, the vendor's exception), so the judge can tell a
    failure that may pass — an outage, a throttle — from one that will not:
    a library bug, a key a deployment did not set.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.seen: list[tuple[str, BaseException | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, _UNAVAILABLE_ATTR, None) != _NEWS_CATEGORY:
            return
        message = record.getMessage()
        args = record.args if isinstance(record.args, tuple) else ()
        error = args[-1] if args and isinstance(args[-1], BaseException) else None
        self.seen.append((message, error))


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
    #: The graph answered on a news-less input and the answer was kept
    #: anyway (the judge's patience was over, #408); a strategy reads the
    #: rating as any other, the sidecar and the store carry the flag, and
    #: the sidecar keeps the router's line that said so.
    news_missing: bool = False
    news_missing_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.news_missing, bool):
            raise ValueError(f"news_missing must be a bool, got {self.news_missing!r}")
        if self.news_missing_reason is not None and not isinstance(self.news_missing_reason, str):
            raise ValueError(
                f"news_missing_reason must be a string or None, got {self.news_missing_reason!r}"
            )
        if self.news_missing_reason is not None and not self.news_missing:
            raise ValueError("news_missing_reason is only given with news_missing")
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

    def ask(
        self, ticker: str, trade_date: str, context: str, *, accept_news_less: bool = False
    ) -> Answer:
        """The verdict on ``ticker`` as of ``trade_date`` (``YYYY-MM-DD``), given ``context``.

        ``accept_news_less`` says the caller's patience for the news input
        is over: an answer given without it is returned with
        ``news_missing`` set instead of "later" (#408).
        """


def engine_config(settings: AgentSettings, home: Path) -> dict[str, Any]:
    """The engine's config: its defaults, with the judge's settings and this package's directories over them.

    ``backend_url`` is left to the provider's own endpoint. The memory log
    is switched off by giving it no path. Structured output is off, as the
    perp engine has it: the rating is read off the decision's closing
    ``Rating:`` line, which the context asks for; the judge runs on the
    free-text path.
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
        self,
        settings: AgentSettings,
        home: Path,
        *,
        graph_class: type | None = None,
        probe_news: bool = False,
    ) -> None:
        self._settings = settings
        self._home = home
        self._graph_class = None if graph_class is None else _spot_graph(graph_class)
        self._config: dict[str, Any] | None = None
        # Whether ``ask`` probes the news tools through the engine's router
        # before building a graph (#408): the ``verdict`` command switches it
        # on; a question that would only be answered "later" then costs no
        # model call. Off by default so a judge built in tests, or by hand,
        # asks the way it always did.
        self._probing = probe_news

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
        except Exception as exc:
            # An engine that is not installed, or one that refuses an environment
            # override (TRADINGAGENTS_*) as it imports.
            raise AgentError(
                f"the tradingagents engine cannot be set up ({type(exc).__name__}: {exc})"
            ) from exc
        config = self._engine_config()
        try:
            graph = self._graph_class(
                selected_analysts=list(self._settings.selected_analysts),
                debug=False,
                config=config,
            )
        except Exception as exc:
            # A missing key or an unknown provider: the engine refuses before any call.
            raise AgentError(f"the judge cannot be built ({type(exc).__name__}: {exc})") from exc
        graph.spot_context = context
        return graph

    def _engine_config(self) -> dict[str, Any]:
        if self._config is None:
            try:
                self._config = engine_config(self._settings, self._home)
            except Exception as exc:
                raise AgentError(
                    f"the tradingagents engine cannot be set up ({type(exc).__name__}: {exc})"
                ) from exc
        return self._config

    def _probe_news(self, ticker: str, trade_date: str) -> None:
        """Ask the engine's two news tools for ``ticker`` now, without a model (#408).

        One ``get_news`` and one ``get_global_news`` call through the
        router, under the engine's config and the same watch ``ask`` keeps
        on the router's logger — the two tools the graph's news analyst
        has, so the probe's verdict is the one the graph's watch would
        give. The optional-lane line for ``news_data``, or a probe that
        raises, is classed as the graph's answer is: a failure that may
        pass is :class:`JudgeUnavailable` ("later", with the router's
        words), one that will not is :class:`AgentError` ("for good").
        Returns when both answered.
        """
        try:
            from datetime import date, timedelta

            from tradingagents.dataflows.config import set_config
            from tradingagents.dataflows.interface import route_to_vendor
        except Exception as exc:
            raise AgentError(
                f"the tradingagents engine cannot be set up ({type(exc).__name__}: {exc})"
            ) from exc
        set_config(self._engine_config())
        start = (date.fromisoformat(trade_date) - timedelta(days=7)).isoformat()
        watch = _NewsWatch()
        router_logger = logging.getLogger(_ROUTER_LOGGER)
        level_before = router_logger.level
        if not router_logger.isEnabledFor(logging.WARNING):
            router_logger.setLevel(logging.WARNING)
        router_logger.addHandler(watch)
        try:
            route_to_vendor("get_news", ticker, start, trade_date)
            if not watch.seen:
                route_to_vendor("get_global_news", trade_date, None, None)
        except Exception as exc:
            said = f"the news probe raised {type(exc).__name__}: {exc}"
            if _may_pass(exc):
                raise JudgeUnavailable(
                    f"the judge was not asked on {ticker} (news unavailable before asking: "
                    f"{said}; no model call was made)"
                ) from exc
            raise AgentError(
                f"the judge failed on {ticker} for good (news failed before asking: {said}); "
                f"asking again unchanged will not help"
            ) from exc
        finally:
            router_logger.removeHandler(watch)
            router_logger.setLevel(level_before)
        if watch.seen:
            message, error = watch.seen[0]
            if error is not None and not _may_pass(error):
                raise AgentError(
                    f"the judge failed on {ticker} for good (news failed before asking: "
                    f"{message}); asking again unchanged will not help"
                )
            raise JudgeUnavailable(
                f"the judge was not asked on {ticker} (news unavailable before asking: "
                f"{message}; no model call was made)"
            )

    def ask(
        self, ticker: str, trade_date: str, context: str, *, accept_news_less: bool = False
    ) -> Answer:
        started = _time.monotonic()
        if self._probing and not accept_news_less and "news" in self._settings.selected_analysts:
            # Inside the caller's patience: "later" (or "for good") before
            # any model call, since a news-less answer would not be kept. A
            # graph set up without the news analyst reads no news, so there
            # is nothing to probe for it.
            self._probe_news(ticker, trade_date)
        graph = self._graph(context)
        watch = _NewsWatch()
        router_logger = logging.getLogger(_ROUTER_LOGGER)
        # The line is a WARNING; a logging setup that silences the router's
        # warnings would silence the watch with them, so the level is held
        # at WARNING for the question and put back after.
        level_before = router_logger.level
        if not router_logger.isEnabledFor(logging.WARNING):
            router_logger.setLevel(logging.WARNING)
        router_logger.addHandler(watch)
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
        finally:
            router_logger.removeHandler(watch)
            router_logger.setLevel(level_before)
        answer = _answer(
            propagated, ticker, self._settings.selected_analysts, _time.monotonic() - started
        )
        if watch.seen:
            # The graph answered on a news-less input. The failure is classed
            # as the router's raise was before #393: one that will not pass
            # says "for good"; one that may pass says "later" — unless the
            # caller's patience is over, when the answer is kept with the
            # flag set (#408), since a bar whose news never came back is
            # better judged on the last visit than never.
            message, error = watch.seen[0]
            if error is not None and not _may_pass(error):
                raise AgentError(
                    f"the judge failed on {ticker} for good (news failed: {message}); asking "
                    f"again unchanged will not help"
                )
            if not accept_news_less:
                raise JudgeUnavailable(
                    f"the judge did not answer on {ticker} (news unavailable: {message})"
                )
            logger.warning(
                "the judge answered on %s without news and the answer is kept (news_missing): %s",
                ticker,
                message,
            )
            return replace(answer, news_missing=True, news_missing_reason=message)
        return answer


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


def _rating_of(decision: str, ticker: str) -> Rating:
    """The rating on the decision's last ``Rating:`` line; ``REVIEW``, and a warning, when there is none."""
    lines = _RATING_LINE.findall(decision)
    if not lines:
        logger.warning(
            "the judge's decision on %s has no `Rating:` line (cut short by agent.max_tokens, "
            "or not in the asked shape): recorded as REVIEW",
            ticker,
        )
        return Rating.REVIEW
    word = lines[-1].capitalize()
    try:
        return Rating(word)
    except ValueError:
        logger.warning(
            "the judge's decision on %s ends with `Rating: %s`, and a rating is one of %s: "
            "recorded as REVIEW",
            ticker,
            word,
            [rating.value for rating in Rating if not rating.is_review],
        )
        return Rating.REVIEW


def _answer(
    propagated: object, ticker: str, selected_analysts: Sequence[str], elapsed: float
) -> Answer:
    """Read the engine's ``(final_state, signal)`` into an :class:`Answer`; another shape is refused.

    The rating is read from the decision text's ``Rating:`` line by
    :func:`_rating_of`; the engine's ``signal`` is not read (see
    :data:`_RATING_LINE`).
    """
    if not isinstance(propagated, tuple | list) or len(propagated) != 2:
        raise AgentError(
            f"the judge answered on {ticker} in a shape that cannot be read "
            f"({type(propagated).__name__})"
        )
    final_state, _signal = propagated
    if not isinstance(final_state, Mapping) or not isinstance(
        final_state.get(_DECISION_KEY), str
    ):
        raise AgentError(f"the judge's answer on {ticker} holds no {_DECISION_KEY} text")
    rating = _rating_of(final_state[_DECISION_KEY], ticker)
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

    def ask(
        self, ticker: str, trade_date: str, context: str, *, accept_news_less: bool = False
    ) -> Answer:
        self.asked.append((ticker, trade_date, context))
        return Answer(
            decision=f"Rating: {self.rating.value} (a fake verdict; no model was asked)",
            rating=self.rating,
            reports=None,
            elapsed_seconds=0.0,
        )
