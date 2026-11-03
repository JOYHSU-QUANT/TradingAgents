"""The lower half of the paper trader's graph, re-run from a recorded cycle's reports (replay plan PR 3b).

Each paper cycle ran the whole engine graph: the analysts fetched their data
and wrote reports, the researchers debated, the trader planned, the risk
debate judged, and the portfolio manager decided. The ``.reports.json``
sidecar the daemon writes beside each input payload (plan PR 0) keeps the
upper half's output — the analysts' reports, the research debate and its
verdict — so the LOWER half can be run again offline from exactly what it
saw: ``Trader -> Aggressive Analyst -> (the risk-debate router) ->
Portfolio Manager``, the engine's own node factories on a graph this
module assembles (upstream ``graph/setup.py`` is not touched, for the
next sync's sake).

What differs from the recorded cycle is the one thing the backtest is
about: the ``Position:`` section of the perp context. The daemon rendered
the paper books; here it is re-rendered from the simulated account's own
book (:func:`position_section`, through the perp package's own renderer,
so it is spelled as the daemon spelled it) and put where the recorded
section stood (:func:`replace_position_section`). A payload whose
``context_shape`` has no position section is refused rather than asked
with the paper trader's position.

Two known differences from the daemon. The instrument identity line above
the perp context is rebuilt without the engine's network lookup
(``build_instrument_context`` with no identity), and the position section's
holding cost is priced at the research store's funding rate in force at the
question, the daemon's having been the venue's predicted rate at that
moment. The risk debate runs one round, the engine default the daemon ran
under (its ``engine`` block has no such knob). Structured output is turned
off, as the daemon turns it off, so the target JSON survives in the
decision text (memory of issue #338).

The engine is reached only through :func:`~.upstream.load_engine`; this
module imports nothing of it at module level, so a dry run and every test
with a fake engine pay nothing for it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from .model import build_llm
from .paper_store import REPORTS_SUFFIX
from .replay import Completion
from .upstream import (
    FINAL_TRADE_DECISION_KEY,
    REPORT_KEYS,
    BookPosition,
    CostModel,
    DecisionConfig,
    Engine,
    PositionInputs,
    PositionPricing,
    RiskConfig,
    build_position_context,
    effective_max_target_margin_pct,
    from_epoch_ms,
    inject_perp_context,
    load_engine,
    render_position_section,
    sidecar_path,
)
from .variant import GRAPH_MODE, Variant

__all__ = [
    "POSITION_HEADING",
    "POSITION_SHAPE",
    "REPORTS_SCHEMA",
    "RISK_ROUNDS",
    "GraphCompletion",
    "LowerGraph",
    "LowerGraphError",
    "Reports",
    "load_reports",
    "position_section",
    "replace_position_section",
]

# The sidecar's own schema stamp (``common/sidecar.py`` writes it); a later
# stamp is a shape this module has not read and is refused by name.
REPORTS_SCHEMA: Final = 1
# The risk debate's rounds: the engine default (``max_risk_discuss_rounds``),
# which the daemon never overrides.
RISK_ROUNDS: Final = 1
# Where the recorded context's position section begins: the renderer's own
# heading at the start of a line. The section runs to the end of the text.
POSITION_HEADING: Final = "\nPosition:"
# The ``context_shape`` token that says the payload carries that section.
POSITION_SHAPE: Final = "position"

# The four reports the lower half reads, the first four sidecar keys (the
# engine's ``AgentState`` names); a report the cycle did not run (an analyst
# not selected) is ``null`` there and an empty string here, as the engine
# leaves it. ``investment_plan`` is the research manager's verdict.
_REPORTS: Final = REPORT_KEYS[:4]
_PLAN: Final = "investment_plan"
_DEBATE_KEYS: Final = frozenset(
    {"bull_history", "bear_history", "history", "current_response", "judge_decision", "count"}
)
_FRESH_RISK_DEBATE: Final = {
    "aggressive_history": "",
    "conservative_history": "",
    "neutral_history": "",
    "history": "",
    "latest_speaker": "",
    "current_aggressive_response": "",
    "current_conservative_response": "",
    "current_neutral_response": "",
    "judge_decision": "",
    "count": 0,
}
_TRADER: Final = "Trader"
_RISK_NODES: Final = ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst")


class LowerGraphError(Exception):
    """A question the lower half cannot be asked; the sentence names the question and why."""


@dataclass(frozen=True)
class Reports:
    """What the upper half wrote in one recorded cycle: the lower half's starting state."""

    selected_analysts: tuple[str, ...]
    market_report: str
    sentiment_report: str
    news_report: str
    fundamentals_report: str
    investment_debate_state: Mapping[str, object]
    investment_plan: str

    def state(self) -> dict[str, object]:
        """The state keys the lower half starts from; the risk debate and the plans it re-runs are fresh."""
        return {
            **{key: getattr(self, key) for key in (*_REPORTS, _PLAN)},
            "investment_debate_state": dict(self.investment_debate_state),
            "trader_investment_plan": "",
            "risk_debate_state": dict(_FRESH_RISK_DEBATE),
            FINAL_TRADE_DECISION_KEY: "",
        }


def load_reports(payload_path: Path, *, input_id: str) -> Reports:
    """The ``.reports.json`` beside ``payload_path``, checked to be the daemon's shape.

    Refused by name when it is missing (an engine-failed cycle has none; the
    question cannot be re-run), stamped with a schema this module has not
    read, or not the shape the daemon writes.
    """
    path = sidecar_path(payload_path, REPORTS_SUFFIX)
    where = str(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LowerGraphError(
            f"{input_id}: no readable decision reports beside the payload ({where!r}: {exc}); "
            "the lower half of the graph cannot be run without the analysts' reports"
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise LowerGraphError(f"{input_id}: {where!r} is not JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise LowerGraphError(f"{input_id}: {where!r} does not hold a mapping")
    if document.get("schema") != REPORTS_SCHEMA:
        raise LowerGraphError(
            f"{input_id}: {where!r} is stamped reports schema {document.get('schema')!r}; this "
            f"build reads schema {REPORTS_SCHEMA}"
        )
    missing = [key for key in REPORT_KEYS if key not in document]
    if missing:
        raise LowerGraphError(f"{input_id}: {where!r} lacks {missing}; not the daemon's shape")
    analysts = document.get("selected_analysts")
    if not isinstance(analysts, list) or not all(isinstance(a, str) for a in analysts):
        raise LowerGraphError(f"{input_id}: {where!r} has no selected_analysts list")
    texts: dict[str, str] = {}
    for key in (*_REPORTS, _PLAN):
        value = document[key]
        if value is None and key != _PLAN:
            value = ""
        if not isinstance(value, str):
            raise LowerGraphError(f"{input_id}: {where!r} has a non-text {key}")
        texts[key] = value
    debate = document["investment_debate_state"]
    if not isinstance(debate, dict) or not set(debate) >= _DEBATE_KEYS:
        raise LowerGraphError(
            f"{input_id}: {where!r} has no complete investment_debate_state (needs "
            f"{sorted(_DEBATE_KEYS)})"
        )
    return Reports(selected_analysts=tuple(analysts), investment_debate_state=debate, **texts)


def replace_position_section(
    context_text: str, context_shape: str | None, lines: Sequence[str], *, input_id: str
) -> str:
    """``context_text`` with its position section replaced by ``lines``.

    The section is found by the LAST :data:`POSITION_HEADING` at a line
    start and runs to the end of the text (the renderer puts it last). A
    payload whose ``context_shape`` has no ``position`` token, or whose text
    has no such heading, is refused: asking it unchanged would show the
    model the paper trader's position, not the account's.
    """
    shapes = set(context_shape.split("|")) if context_shape else set()
    if POSITION_SHAPE not in shapes:
        raise LowerGraphError(
            f"{input_id}: the payload's context_shape {context_shape!r} names no position "
            "section, so the simulated account's position cannot take the recorded one's place; "
            "the question is not asked"
        )
    index = context_text.rfind(POSITION_HEADING)
    if index < 0:
        raise LowerGraphError(
            f"{input_id}: the payload names a position section but its context_text has no "
            f"{POSITION_HEADING.strip()!r} heading; the question is not asked"
        )
    trailing = "\n" if context_text.endswith("\n") else ""
    return context_text[:index] + "\n" + "\n".join(lines) + trailing


def position_section(
    *,
    size: Decimal,
    entry: Decimal | None,
    wallet: Decimal,
    last_fill_ms: int | None,
    mark: Decimal,
    funding_rate: Decimal,
    coin: str,
    as_of: datetime,
    risk: RiskConfig,
    decision: DecisionConfig,
    costs: CostModel,
) -> list[str]:
    """The ``Position:`` section for the simulated book, as the daemon would have rendered it.

    Priced the way the daemon prices its own (``decision_provider``): the
    configured leverage, the decision grid up to the effective ceiling, the
    run's taker fee and slippage. ``funding_rate`` is the hourly rate in
    force at the question; ``as_of`` the context's own as-of (the candle
    end), which the fill's age is measured from. The book must carry
    positive equity; the caller checks ruin before asking.
    """
    pricing = PositionPricing(
        leverage=risk.leverage,
        grid_min=decision.ai_target_margin_min_pct,
        grid_max=effective_max_target_margin_pct(risk, decision),
        grid_step=decision.target_margin_step_pct,
        taker_fee_rate=Decimal(str(costs.taker_fee_rate)),
        slippage_bps=Decimal(str(costs.slippage_bps)),
    )
    book = BookPosition(
        size=size,
        entry_price=entry,
        wallet_balance=wallet,
        last_fill_at=None if last_fill_ms is None else from_epoch_ms(last_fill_ms),
    )
    context = build_position_context(
        PositionInputs(book, pricing), mark=mark, funding_rate=funding_rate
    )
    if context is None:
        raise LowerGraphError("the account has no positive equity to render a position section for")
    return render_position_section(context, coin=coin, as_of=as_of)


@dataclass(frozen=True)
class GraphCompletion(Completion):
    """The portfolio manager's completion, with the whole graph's usage beside it.

    ``text``, ``truncated``, the model and the tokens are the portfolio
    manager's own call (the one whose text is parsed); ``graph_*`` sum every
    completion the graph made on the run that produced it (the trader, the
    three debators and the manager). What a run that failed before the
    manager answered cost is kept by :class:`LowerGraph` instead.
    """

    graph_input_tokens: int = 0
    graph_output_tokens: int = 0
    completions: int = 0


class _WithSystem:
    """An LLM that puts a fixed system message before whatever it is asked.

    The portfolio manager node builds a single human prompt and invokes the
    LLM with it; a graph variant that names a system prompt has it ride
    here, before that prompt, and nowhere else. Everything but ``invoke`` is
    the wrapped LLM's own.
    """

    def __init__(self, llm: Any, system: str, engine: Engine) -> None:
        self._llm = llm
        self._system = system
        self._engine = engine

    def invoke(self, prompt: Any, config: Any = None, **kwargs: Any) -> Any:
        if isinstance(prompt, str):
            messages: list[Any] = [self._engine.HumanMessage(content=prompt)]
        elif isinstance(prompt, list):
            messages = list(prompt)
        else:
            messages = [prompt]
        return self._llm.invoke(
            [self._engine.SystemMessage(content=self._system), *messages], config=config, **kwargs
        )

    # The other ways to run a chat model, which would skip the system message
    # without a word; the manager node calls ``invoke`` and nothing else.
    _BYPASSES = frozenset(
        {
            "ainvoke",
            "batch",
            "abatch",
            "stream",
            "astream",
            "astream_events",
            "bind",
            "bind_tools",
            "with_structured_output",
            "with_config",
            "with_retry",
            "with_fallbacks",
            "generate",
            "predict",
        }
    )

    def __getattr__(self, name: str) -> Any:
        if name in self._BYPASSES:
            raise LowerGraphError(
                f"the portfolio manager node called {name}(), which would run the model "
                "without the variant's system message; the lower graph only supports invoke()"
            )
        return getattr(self._llm, name)


class LowerGraph:
    """The lower half of the graph, built once for a graph variant and asked question by question.

    The portfolio manager runs on the variant's model (the daemon's deep
    model), the trader and the three debators on its quick model, or on the
    same model when the variant names none. Both clients are the engine's
    own (``create_llm_client``, the factory the daemon's graph uses, through
    :func:`~.model.build_llm`), with the variant's completion cap and, when
    set, its temperature; one usage collector rides both, so every
    completion's stop reason and tokens are read the way the daemon reads
    them. ``lost_input_tokens`` and ``lost_output_tokens`` sum what the
    graph runs that raised before the manager answered cost: paid for, and
    in no answer's figures.
    """

    def __init__(self, variant: Variant, *, engine: Engine | None = None) -> None:
        if variant.mode != GRAPH_MODE:
            raise LowerGraphError(
                f"variant {variant.name!r} is a {variant.mode} variant; the lower graph walks "
                f"{GRAPH_MODE} variants"
            )
        self.variant = variant
        self._engine = engine = load_engine() if engine is None else engine
        self.collector = engine.CompletionUsageCollector()
        self.lost_input_tokens = 0
        self.lost_output_tokens = 0
        # The daemon's own switch (engine_bridge): a structured completion
        # would drop the target JSON block the parse seam reads.
        engine.set_config({"structured_output": False})
        callbacks = [self.collector]
        deep = build_llm(engine, variant, variant.provider, variant.model, callbacks=callbacks)
        quick = (
            deep
            if variant.quick_model is None
            else build_llm(
                engine,
                variant,
                variant.quick_provider or "",
                variant.quick_model,
                callbacks=callbacks,
            )
        )
        manager_llm: Any = (
            _WithSystem(deep, variant.system_prompt, engine) if variant.has_prompt else deep
        )
        graph = engine.StateGraph(engine.AgentState)
        graph.add_node(_TRADER, engine.create_trader(quick))
        debators = (
            engine.create_aggressive_debator,
            engine.create_conservative_debator,
            engine.create_neutral_debator,
        )
        for name, factory in zip(_RISK_NODES, debators, strict=True):
            graph.add_node(name, factory(quick))
        graph.add_node(engine.PORTFOLIO_MANAGER_NODE, engine.create_portfolio_manager(manager_llm))
        graph.add_edge(engine.START, _TRADER)
        graph.add_edge(_TRADER, _RISK_NODES[0])
        router = engine.ConditionalLogic(
            max_risk_discuss_rounds=RISK_ROUNDS
        ).should_continue_risk_analysis
        for name in _RISK_NODES:
            graph.add_conditional_edges(name, router, engine.RISK_ANALYSIS_PATH_MAP)
        graph.add_edge(engine.PORTFOLIO_MANAGER_NODE, engine.END)
        self._graph = graph.compile()

    def _instrument_context(self, ticker: str, context_text: str, format_instructions: str) -> str:
        """The state's ``instrument_context``: the identity line, the perp context, the format block last."""
        context = context_text
        if self.variant.extra_context is not None:
            context = f"{context}\n\n{self.variant.extra_context}"
        base = self._engine.build_instrument_context(ticker, "crypto")
        return inject_perp_context(base, context, format_instructions)

    def ask(
        self,
        *,
        ticker: str,
        as_of: datetime,
        context_text: str,
        format_instructions: str,
        reports: Reports,
    ) -> GraphCompletion:
        """Run the lower half once and return the portfolio manager's decision text and usage.

        ``ticker`` is the word the daemon handed ``propagate`` for the
        recorded cycle — the payload's ``graph_ticker``, or the bare coin
        for a payload from before it was recorded (#392) — and goes into
        the trader's prompt and the identity line alike. ``context_text``
        already carries the simulated account's position section; ``as_of``
        is the context's own as-of, which dates the trade as the daemon
        dates it. A failure anywhere in the graph propagates after its
        completions are added to the lost tokens: the caller's call
        discipline (:func:`~.replay.attempt`) sorts it.
        """
        state: dict[str, Any] = {
            "messages": [],
            "company_of_interest": ticker,
            "asset_type": "crypto",
            "instrument_context": self._instrument_context(ticker, context_text, format_instructions),
            "trade_date": as_of.strftime("%Y-%m-%d"),
            "sender": "",
            "past_context": "",
            **reports.state(),
        }
        before = len(self.collector.calls)
        try:
            final = self._graph.invoke(state)
            if not isinstance(final, Mapping):
                raise LowerGraphError(
                    f"the graph returned a {type(final).__name__}, not the state mapping; "
                    "the engine's shape has changed and the decision text cannot be read"
                )
        except BaseException:
            # Whatever the run cost before it failed is paid for: kept here,
            # since no answer will carry it.
            spent = self.collector.calls[before:]
            self.lost_input_tokens += sum(c.input_tokens or 0 for c in spent)
            self.lost_output_tokens += sum(c.output_tokens or 0 for c in spent)
            raise
        calls = self.collector.calls[before:]
        manager = next(
            (c for c in reversed(calls) if c.node == self._engine.PORTFOLIO_MANAGER_NODE), None
        )
        return GraphCompletion(
            text=final.get(FINAL_TRADE_DECISION_KEY),
            truncated=manager is not None and manager.truncated,
            model=None if manager is None else manager.model,
            input_tokens=None if manager is None else manager.input_tokens,
            output_tokens=None if manager is None else manager.output_tokens,
            usage_reported=manager is not None,
            graph_input_tokens=sum(c.input_tokens or 0 for c in calls),
            graph_output_tokens=sum(c.output_tokens or 0 for c in calls),
            completions=len(calls),
        )
