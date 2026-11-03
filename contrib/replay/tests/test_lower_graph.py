"""The lower half of the graph: the reports it starts from, the position section it swaps in, the graph it builds.

The engine surface is a fake (:func:`conftest.fake_engine`), so nothing here
needs a key, a network or ``langgraph``: the fake graph records the nodes
and edges it is given and runs the nodes in the order the real router would.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from contrib.replay.lower_graph import (
    RISK_ROUNDS,
    LowerGraph,
    LowerGraphError,
    Reports,
    _WithSystem,
    load_reports,
    position_section,
    replace_position_section,
)
from contrib.replay.upstream import CostModel, DecisionConfig, RiskConfig, from_epoch_ms

from .conftest import GATE_DECISION, GATE_RISK, at_ms, fake_engine
from .papers import graph_variant, reports_record, variant as simple_variant

PM = "Portfolio Manager"
RISK_NODES = ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst")


# -- the reports sidecar --------------------------------------------------------


def _write_reports(tmp_path: Path, record: dict) -> Path:
    payload = tmp_path / "BTC-x.json"
    payload.write_text("{}", encoding="utf-8")
    (tmp_path / "BTC-x.reports.json").write_text(json.dumps(record), encoding="utf-8")
    return payload


def test_the_reports_sidecar_is_read_into_the_lower_halfs_starting_state(tmp_path):
    reports = load_reports(_write_reports(tmp_path, reports_record(3)), input_id="q")
    assert reports.selected_analysts == ("market", "social", "news")
    assert reports.fundamentals_report == ""  # null in the sidecar: the analyst was not selected
    state = reports.state()
    assert state["investment_plan"] == "plan 03"
    assert state["investment_debate_state"]["judge_decision"] == "judge 03"
    # What the lower half re-runs starts fresh, whatever the cycle recorded.
    assert state["trader_investment_plan"] == "" and state["final_trade_decision"] == ""
    assert state["risk_debate_state"]["count"] == 0
    assert state["risk_debate_state"]["latest_speaker"] == ""


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda r: r.pop("investment_plan"), r"lacks \['investment_plan'\]"),
        (lambda r: r.update(schema=2), "stamped reports schema 2; this build reads schema 1"),
        (lambda r: r.update(selected_analysts="market"), "no selected_analysts list"),
        (
            lambda r: r.update(investment_debate_state={"history": ""}),
            "no complete investment_debate_state",
        ),
        (lambda r: r.update(market_report=7), "non-text market_report"),
        (lambda r: r.update(investment_plan=None), "non-text investment_plan"),
    ],
    ids=["missing-key", "schema", "analysts", "debate", "report-type", "plan-null"],
)
def test_a_sidecar_that_is_not_the_daemons_shape_is_refused_by_name(tmp_path, change, message):
    record = reports_record(0)
    change(record)
    with pytest.raises(LowerGraphError, match=f"q: .*{message}"):
        load_reports(_write_reports(tmp_path, record), input_id="q")


def test_a_missing_or_unreadable_sidecar_is_refused_by_name(tmp_path):
    payload = tmp_path / "BTC-x.json"
    payload.write_text("{}", encoding="utf-8")
    with pytest.raises(LowerGraphError, match="q: no readable decision reports beside the payload"):
        load_reports(payload, input_id="q")
    (tmp_path / "BTC-x.reports.json").write_text("not json", encoding="utf-8")
    with pytest.raises(LowerGraphError, match="is not JSON"):
        load_reports(payload, input_id="q")
    (tmp_path / "BTC-x.reports.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(LowerGraphError, match="does not hold a mapping"):
        load_reports(payload, input_id="q")


# -- the position section ---------------------------------------------------------


def test_the_position_section_is_replaced_at_the_last_heading_and_the_text_before_it_kept():
    text = "Coin: BTC\n\nPrice:\n  Mark: 100\n\nPosition:\n  Side: long, size 1 BTC\n  Entry: 90\n"
    out = replace_position_section(text, "price|position", ["Position:", "  flat"], input_id="q")
    assert out == "Coin: BTC\n\nPrice:\n  Mark: 100\n\nPosition:\n  flat\n"
    # Without a trailing newline the result has none either.
    bare = replace_position_section(
        text.rstrip("\n"), "position", ["Position:", "  flat"], input_id="q"
    )
    assert bare.endswith("Mark: 100\n\nPosition:\n  flat")


def test_a_payload_without_a_position_section_is_refused_not_asked_with_papers_position():
    with pytest.raises(
        LowerGraphError, match="q: the payload's context_shape 'price' names no position"
    ):
        replace_position_section("Position:\n x", "price", ["Position:"], input_id="q")
    with pytest.raises(
        LowerGraphError, match="names a position section but its context_text has no"
    ):
        replace_position_section("Coin: BTC", "price|position", ["Position:"], input_id="q")
    with pytest.raises(LowerGraphError, match="context_shape None names no position"):
        replace_position_section("x", None, [], input_id="q")


def _section(**overrides) -> list[str]:
    args: dict[str, Any] = {
        "size": Decimal(0),
        "entry": None,
        "wallet": Decimal(1000),
        "last_fill_ms": None,
        "mark": Decimal(100),
        "funding_rate": Decimal("0.0001"),
        "coin": "BTC",
        "as_of": from_epoch_ms(at_ms(1)),
        "risk": RiskConfig.from_dict(GATE_RISK),
        "decision": DecisionConfig.from_dict(GATE_DECISION),
        "costs": CostModel(),
    }
    args.update(overrides)
    return position_section(**args)


def test_a_flat_book_renders_the_flat_line_with_the_accounts_equity():
    assert _section() == ["Position:", "  flat, no open position (account equity 1,000.00 USDC)"]


def test_an_open_book_renders_its_side_size_entry_and_margin_from_the_account():
    # 3 BTC bought at 90, marked at 100: equity 1000 + 30, notional 300, margin 300 / 1030.
    lines = _section(size=Decimal(3), entry=Decimal(90), last_fill_ms=at_ms(0))
    assert lines[0] == "Position:"
    assert lines[1] == "  Side: long, size 3 BTC, notional 300.00 USDC at mark"
    assert lines[2] == "  Entry: 90.00 (unrealized PnL +30.00 USDC)"
    assert lines[3].startswith("  Committed margin: 29.13% of account equity 1,030.00 USDC")
    assert lines[4].startswith("  Last fill: ") and lines[4].endswith(
        "before the as-of time above)"
    )
    assert lines[5].startswith("  Holding cost at the current funding rate: pays ")
    # The grid rows stop at the effective ceiling (the 60% cap under the fixture's gate).
    assert any(line.startswith("    -> 60%:") for line in lines)
    assert not any(line.startswith("    -> 65%:") for line in lines)


def test_a_book_with_no_equity_cannot_be_rendered():
    with pytest.raises(LowerGraphError, match="no positive equity"):
        _section(size=Decimal(3), entry=Decimal(90), wallet=Decimal(-100))


# -- the graph ----------------------------------------------------------------------


@dataclass
class _Call:
    node: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    truncated: bool


class _Collector:
    """The usage collector's shape: the calls it saw, and (in the fake) the node now running."""

    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.node: str | None = None


class _LLM:
    """A fake chat model: answers with one text and reports one call to every collector it carries."""

    def __init__(
        self,
        model: str,
        kwargs: dict,
        text: str,
        tokens: tuple[int, int] | None,
        truncated: bool = False,
    ) -> None:
        self.truncated = truncated
        self.model = model
        self.kwargs = kwargs
        self.text = text
        self.tokens = tokens
        self.seen: list[Any] = []

    def invoke(self, prompt: Any, config: Any = None, **kwargs: Any) -> Any:
        self.seen.append(prompt)
        if self.tokens is not None:
            for collector in self.kwargs.get("callbacks", []):
                collector.calls.append(
                    _Call(
                        collector.node, self.model, self.tokens[0], self.tokens[1], self.truncated
                    )
                )
        return SimpleNamespace(content=self.text)


class _Graph:
    """A fake StateGraph: records what it is given; compiled, it runs the nodes in the router's order."""

    def __init__(self, state_type: Any, collector: _Collector) -> None:
        self.state_type = state_type
        self.collector = collector
        self.nodes: dict[str, Any] = {}
        self.edges: list[tuple[str, str]] = []
        self.conditional: list[tuple[str, Any, Any]] = []

    def add_node(self, name: str, fn: Any) -> None:
        self.nodes[name] = fn

    def add_edge(self, a: str, b: str) -> None:
        self.edges.append((a, b))

    def add_conditional_edges(self, node: str, router: Any, path_map: Any) -> None:
        self.conditional.append((node, router, path_map))

    def compile(self) -> _Graph:
        return self

    def invoke(self, state: dict) -> dict:
        state = dict(state)
        for name in ("Trader", *RISK_NODES, PM):
            self.collector.node = name
            state.update(self.nodes[name](state))
        return state


class _Logic:
    def __init__(self, max_debate_rounds: int = 1, max_risk_discuss_rounds: int = 1) -> None:
        self.max_risk_discuss_rounds = max_risk_discuss_rounds

    def should_continue_risk_analysis(self, state: dict) -> str:
        return PM


class _Fake:
    """The engine surface the lower graph is built on, every borrowed name recorded."""

    PATH_MAP = {node: node for node in (*RISK_NODES, PM)}

    def __init__(
        self,
        *,
        deep_text: str = "Final decision",
        deep_tokens: tuple[int, int] | None = (300, 40),
        deep_truncated: bool = False,
    ) -> None:
        self.deep_truncated = deep_truncated
        self.clients: list[tuple[str, str, dict]] = []
        self.llms: dict[str, _LLM] = {}
        self.graphs: list[_Graph] = []
        self.config: list[dict] = []
        self.states: dict[str, dict] = {}
        self.deep_text = deep_text
        self.deep_tokens = deep_tokens
        self.collector = _Collector()

    def create_llm_client(self, *, provider: str, model: str, base_url: Any, **kwargs: Any) -> Any:
        self.clients.append((provider, model, kwargs))
        deep = len(self.clients) == 1
        llm = _LLM(
            model,
            kwargs,
            self.deep_text if deep else "the plan",
            self.deep_tokens if deep else (100, 20),
            deep and self.deep_truncated,
        )
        self.llms[model] = llm
        return SimpleNamespace(get_llm=lambda: llm)

    def state_graph(self, state_type: Any) -> _Graph:
        graph = _Graph(state_type, self.collector)
        self.graphs.append(graph)
        return graph

    def _node(self, name: str, llm: Any, prompt: Any, key: str) -> Any:
        def node(state: dict) -> dict:
            self.states[name] = dict(state)
            return {key: llm.invoke(prompt(state)).content}

        return node

    def engine(self):
        fake = self

        def trader(llm):
            return fake._node(
                "Trader",
                llm,
                lambda s: f"trader sees {s['investment_plan']} | {s['instrument_context']}",
                "trader_investment_plan",
            )

        def debator(name):
            return lambda llm: fake._node(
                name, llm, lambda s: f"{name} sees {s['trader_investment_plan']}", "sender"
            )

        def manager(llm):
            return fake._node(
                PM, llm, lambda s: f"pm sees {s['trader_investment_plan']}", "final_trade_decision"
            )

        return fake_engine(
            CompletionUsageCollector=lambda: fake.collector,
            HumanMessage=lambda content: ("human", content),
            SystemMessage=lambda content: ("system", content),
            END="__end__",
            START="__start__",
            StateGraph=fake.state_graph,
            create_aggressive_debator=debator(RISK_NODES[0]),
            create_conservative_debator=debator(RISK_NODES[1]),
            create_neutral_debator=debator(RISK_NODES[2]),
            create_portfolio_manager=manager,
            create_trader=trader,
            create_llm_client=fake.create_llm_client,
            AgentState=object(),
            build_instrument_context=lambda ticker, asset_type: (
                f"The {asset_type} to analyze is `{ticker}`."
            ),
            set_config=fake.config.append,
            ConditionalLogic=_Logic,
            RISK_ANALYSIS_PATH_MAP=fake.PATH_MAP,
            PORTFOLIO_MANAGER_NODE=PM,
        )


def _reports(slot: int) -> Reports:
    """The fixture question's reports, spelled by hand so the test does not lean on the loader."""
    return Reports(
        selected_analysts=("market", "social", "news"),
        market_report=f"market report {slot:02d}",
        sentiment_report=f"sentiment report {slot:02d}",
        news_report=f"news report {slot:02d}",
        fundamentals_report="",
        investment_debate_state=reports_record(slot)["investment_debate_state"],
        investment_plan=f"plan {slot:02d}",
    )


def _ask(graph: LowerGraph, **overrides):
    args: dict[str, Any] = {
        "ticker": "BTC-USD",
        "as_of": from_epoch_ms(at_ms(0)),
        "context_text": "Coin: BTC\n\nPosition:\n  flat",
        "format_instructions": "Answer in JSON.",
        "reports": _reports(0),
    }
    args.update(overrides)
    return graph.ask(**args)


def test_the_graph_is_the_lower_half_on_the_engines_router_and_path_map():
    fake = _Fake()
    LowerGraph(graph_variant(), engine=fake.engine())
    (graph,) = fake.graphs
    assert set(graph.nodes) == {"Trader", *RISK_NODES, PM}
    assert graph.edges == [("__start__", "Trader"), ("Trader", RISK_NODES[0]), (PM, "__end__")]
    assert [node for node, _, _ in graph.conditional] == list(RISK_NODES)
    routers = {router for _, router, _ in graph.conditional}
    assert len(routers) == 1  # one ConditionalLogic, one bound router for all three
    (router,) = routers
    assert router.__self__.max_risk_discuss_rounds == RISK_ROUNDS == 1
    assert all(path_map is fake.PATH_MAP for _, _, path_map in graph.conditional)
    # The daemon's own switch, set before the nodes are built.
    assert fake.config == [{"structured_output": False}]


def test_clients_are_built_for_the_deep_and_quick_models_with_the_cap_and_one_collector():
    fake = _Fake()
    variant = graph_variant(
        quick_provider="openrouter", quick_model="q/q", max_tokens=4096, temperature=0.1
    )
    graph = LowerGraph(variant, engine=fake.engine())
    assert [(p, m) for p, m, _ in fake.clients] == [("openrouter", "a/b"), ("openrouter", "q/q")]
    for _, _, kwargs in fake.clients:
        assert kwargs == {"callbacks": [graph.collector], "max_tokens": 4096, "temperature": 0.1}
    _ask(graph)
    # The manager on the deep model; the trader and the debators on the quick one.
    assert len(fake.llms["a/b"].seen) == 1 and len(fake.llms["q/q"].seen) == 4
    # Without a quick model, one client serves every node; a Google cap takes its own key.
    fake = _Fake()
    graph = LowerGraph(graph_variant(provider="google"), engine=fake.engine())
    _ask(graph)
    assert len(fake.clients) == 1 and "max_output_tokens" in fake.clients[0][2]
    assert len(fake.llms["a/b"].seen) == 5


# A quick model of its own, so the trader's and the manager's texts can be told apart.
_QUICK = {"quick_provider": "openrouter", "quick_model": "q/q"}


def test_a_variants_system_prompt_rides_as_the_portfolio_managers_system_message():
    fake = _Fake()
    _ask(LowerGraph(graph_variant(system_prompt="Decide.", **_QUICK), engine=fake.engine()))
    assert fake.llms["a/b"].seen[-1] == [("system", "Decide."), ("human", "pm sees the plan")]
    # Without one, the manager's prompt reaches the model as the engine built it.
    fake = _Fake()
    _ask(LowerGraph(graph_variant(**_QUICK), engine=fake.engine()))
    assert fake.llms["a/b"].seen[-1] == "pm sees the plan"


def test_ask_starts_from_the_reports_with_a_fresh_risk_debate_and_the_account_context():
    fake = _Fake()
    graph = LowerGraph(graph_variant(extra_context="A lesson.", **_QUICK), engine=fake.engine())
    completion = _ask(graph)
    trader_state = fake.states["Trader"]
    # The word the caller says the daemon handed the graph (#392), in the
    # prompt and the identity line alike; the perp context keeps the coin.
    assert (trader_state["company_of_interest"], trader_state["asset_type"]) == (
        "BTC-USD",
        "crypto",
    )
    assert trader_state["trade_date"] == from_epoch_ms(at_ms(0)).strftime("%Y-%m-%d")
    assert trader_state["instrument_context"] == (
        "The crypto to analyze is `BTC-USD`.\n\n## Perpetual market context\nCoin: BTC\n\n"
        "Position:\n"
        "  flat\n\nA lesson.\n\nAnswer in JSON."
    )
    assert trader_state["investment_plan"] == "plan 00"
    assert trader_state["market_report"] == "market report 00"
    assert trader_state["trader_investment_plan"] == ""
    assert trader_state["risk_debate_state"]["count"] == 0
    assert trader_state["messages"] == [] and trader_state["past_context"] == ""
    # The debate saw the trader's plan; the manager's text is the decision.
    assert fake.states[RISK_NODES[0]]["trader_investment_plan"] == "the plan"
    assert completion.text == "Final decision"
    assert (completion.input_tokens, completion.output_tokens, completion.model) == (300, 40, "a/b")
    assert (completion.graph_input_tokens, completion.graph_output_tokens) == (700, 120)
    assert completion.completions == 5 and completion.usage_reported


def test_usage_is_sliced_per_ask_and_a_manager_call_the_collector_missed_says_so():
    fake = _Fake()
    graph = LowerGraph(graph_variant(**_QUICK), engine=fake.engine())
    first = _ask(graph)
    second = _ask(graph)
    assert (second.graph_input_tokens, second.completions) == (first.graph_input_tokens, 5)
    fake = _Fake(deep_tokens=None)
    completion = _ask(LowerGraph(graph_variant(**_QUICK), engine=fake.engine()))
    assert completion.usage_reported is False and completion.truncated is False
    assert completion.input_tokens is None and completion.completions == 4


def test_a_simple_variant_cannot_build_the_lower_graph():
    with pytest.raises(
        LowerGraphError, match="'v' is a simple variant; the lower graph walks graph"
    ):
        LowerGraph(simple_variant(), engine=_Fake().engine())


def test_a_graph_that_returns_no_state_is_refused_and_its_cost_kept():
    class _Lost(_Graph):
        def invoke(self, state: dict) -> dict:
            super().invoke(state)
            return []  # type: ignore[return-value]

    fake = _Fake()

    def lost_graph(state_type: Any) -> _Graph:
        graph = _Lost(state_type, fake.collector)
        fake.graphs.append(graph)
        return graph

    fake.state_graph = lost_graph  # type: ignore[method-assign]
    graph = LowerGraph(graph_variant(**_QUICK), engine=fake.engine())
    with pytest.raises(LowerGraphError, match="returned a list, not the state mapping"):
        _ask(graph)
    assert (graph.lost_input_tokens, graph.lost_output_tokens) == (700, 120)


def test_the_system_message_wrapper_refuses_the_calls_that_would_skip_it():
    engine = _Fake().engine()
    inner = SimpleNamespace(
        invoke=lambda prompt, config=None: prompt, batch=lambda *a: None, model="m"
    )
    wrapped = _WithSystem(inner, "Decide.", engine)
    assert wrapped.invoke("p") == [("system", "Decide."), ("human", "p")]
    assert wrapped.model == "m"
    for name in ("batch", "with_structured_output", "bind_tools", "stream"):
        with pytest.raises(LowerGraphError, match=rf"called {name}\(\), which would run the model"):
            getattr(wrapped, name)


def test_the_managers_truncation_is_the_completions():
    fake = _Fake(deep_truncated=True)
    completion = _ask(LowerGraph(graph_variant(**_QUICK), engine=fake.engine()))
    assert completion.truncated is True and completion.usage_reported
