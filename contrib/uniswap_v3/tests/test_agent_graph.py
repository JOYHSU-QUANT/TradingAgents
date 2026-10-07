"""The judge over the engine's graph: its config, how it reads an answer, and the fake."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.agent.errors import AgentError, JudgeUnavailable
from contrib.uniswap_v3.agent.graph import (
    FAKE_MODEL,
    REPORT_KEYS,
    Answer,
    FakeJudge,
    TradingAgentsJudge,
    _answer,
    engine_config,
)
from contrib.uniswap_v3.agent.settings import AgentSettings
from contrib.uniswap_v3.domain.verdicts import Rating


def test_engine_config_lays_the_settings_and_the_directories_over_the_engines_defaults(tmp_path):
    config = engine_config(AgentSettings(max_tokens=4096, selected_analysts=["market"]), tmp_path)
    assert config["llm_provider"] == "openrouter"
    assert config["deep_think_llm"] == "anthropic/claude-sonnet-5.5"
    assert config["quick_think_llm"] == "deepseek/deepseek-chat"
    assert config["backend_url"] is None
    assert config["max_tokens"] == 4096
    assert config["structured_output"] is False
    assert config["results_dir"] == str(tmp_path / "logs")
    assert config["data_cache_dir"] == str(tmp_path / "cache")
    assert config["memory_log_path"] is None
    # The engine's other defaults ride along.
    assert "max_debate_rounds" in config and "data_vendors" in config


def test_the_answer_is_read_from_the_final_state_and_the_signal():
    state = {"final_trade_decision": "**Rating**: Buy", "market_report": "m", "messages": [1]}
    answer = _answer((state, "Buy"), "ETH-USD", ("market",), 2.5)
    assert answer.rating is Rating.BUY
    assert answer.decision == "**Rating**: Buy"
    assert answer.elapsed_seconds == 2.5
    assert set(answer.reports) == {"selected_analysts", *REPORT_KEYS}
    assert answer.reports["selected_analysts"] == ["market"]
    assert answer.reports["market_report"] == "m"
    assert answer.reports["news_report"] is None
    assert answer.reports["final_trade_decision"] == "**Rating**: Buy"
    assert "messages" not in answer.reports
    review = _answer(({"final_trade_decision": "?"}, "REVIEW"), "x", (), 0)
    assert review.rating is Rating.REVIEW


@pytest.mark.parametrize(
    ("propagated", "match"),
    [
        ("Buy", r"shape that cannot be read \(str\)"),
        (({"final_trade_decision": "x"}, "Buy", 3), r"shape that cannot be read \(tuple\)"),
        (("state", "Buy"), "holds no final_trade_decision text"),
        (({"final_trade_decision": None}, "Buy"), "holds no final_trade_decision text"),
        (({"final_trade_decision": "x"}, "Strong Buy"), r"signal on ETH-USD is 'Strong Buy'"),
    ],
)
def test_an_answer_of_another_shape_is_refused(propagated, match):
    with pytest.raises(AgentError, match=match):
        _answer(propagated, "ETH-USD", (), 0)


class _StubGraph:
    """Stands in for the engine's graph: records how it was built and what context it saw."""

    built: list[_StubGraph] = []
    build_error: Exception | None = None
    answer_error: Exception | None = None

    def __init__(self, selected_analysts, debug, config):
        if _StubGraph.build_error is not None:
            raise _StubGraph.build_error
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config
        self.seen: str | None = None
        _StubGraph.built.append(self)

    def resolve_instrument_context(self, ticker, asset_type="stock"):
        return f"base context for {ticker} as {asset_type}"

    def propagate(self, ticker, trade_date, asset_type="stock"):
        self.seen = self.resolve_instrument_context(ticker, asset_type)
        if _StubGraph.answer_error is not None:
            raise _StubGraph.answer_error
        return {"final_trade_decision": f"**Rating**: Hold on {trade_date}"}, "Hold"


@pytest.fixture
def stub():
    _StubGraph.built, _StubGraph.build_error, _StubGraph.answer_error = [], None, None
    return _StubGraph


def test_the_judge_builds_a_graph_per_question_with_the_spot_context_on_it(tmp_path, stub):
    settings = AgentSettings(selected_analysts=["market", "news"], max_tokens=1234)
    judge = TradingAgentsJudge(settings, tmp_path, graph_class=stub)
    assert judge.model == "anthropic/claude-sonnet-5.5"
    assert judge.rehearsal is False and judge.point_in_time is False
    answer = judge.ask("ETH-USD", "2024-01-03", "the spot context")
    (graph,) = stub.built
    assert graph.selected_analysts == ["market", "news"] and graph.debug is False
    assert graph.config["max_tokens"] == 1234
    assert graph.config["results_dir"] == str(tmp_path / "logs")
    assert graph.seen == (
        "base context for ETH-USD as crypto\n\n## Spot market context\nthe spot context"
    )
    assert answer.rating is Rating.HOLD and answer.decision == "**Rating**: Hold on 2024-01-03"
    assert answer.elapsed_seconds >= 0
    # Each question gets its own graph with its own context.
    judge.ask("BTC-USD", "2024-01-03", "another context")
    assert len(stub.built) == 2
    assert stub.built[-1].seen.endswith("## Spot market context\nanother context")
    assert graph.spot_context == "the spot context"


def test_a_graph_without_a_spot_context_hands_on_the_base_context(stub):
    from contrib.uniswap_v3.agent.graph import _spot_graph

    graph = _spot_graph(stub)(selected_analysts=["market"], debug=False, config={})
    assert graph.resolve_instrument_context("ETH-USD", "crypto") == (
        "base context for ETH-USD as crypto"
    )


def test_a_graph_the_engine_refuses_to_build_is_an_agent_error(tmp_path, stub):
    stub.build_error = ValueError("Please set the OPENROUTER_API_KEY environment variable")
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(AgentError, match=r"cannot be built \(ValueError: Please set the"):
        judge.ask("ETH-USD", "2024-01-03", "context")


def test_a_graph_that_does_not_answer_is_judge_unavailable(tmp_path, stub):
    stub.answer_error = ConnectionError("the gateway is down")
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(
        JudgeUnavailable, match=r"did not answer on ETH-USD \(ConnectionError: the gateway"
    ):
        judge.ask("ETH-USD", "2024-01-03", "context")


class _ProviderError(Exception):
    def __init__(self, status_code, message="refused", *, spelled="status_code"):
        super().__init__(message)
        setattr(self, spelled, status_code)


class APIConnectionError(Exception):
    """Named as the provider SDKs name their transport error."""


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_provider_refusal_that_a_retry_would_meet_again_is_an_agent_error(
    tmp_path, stub, status
):
    stub.answer_error = _ProviderError(status, "No endpoints found for vendor/model")
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(AgentError, match=r"failed on ETH-USD for good .*No endpoints"):
        judge.ask("ETH-USD", "2024-01-03", "context")


@pytest.mark.parametrize("status", [402, 408, 429, 500, 502, 503])
def test_an_empty_balance_a_rate_limit_a_timeout_or_a_server_error_may_pass(
    tmp_path, stub, status
):
    stub.answer_error = _ProviderError(status)
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(JudgeUnavailable, match="did not answer on ETH-USD"):
        judge.ask("ETH-USD", "2024-01-03", "context")


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset"),
        TimeoutError("timed out"),
        APIConnectionError("the gateway is down"),
        _ProviderError(503, spelled="code"),
    ],
)
def test_a_network_error_or_a_status_spelled_code_may_pass(tmp_path, stub, error):
    stub.answer_error = error
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(JudgeUnavailable):
        judge.ask("ETH-USD", "2024-01-03", "context")


@pytest.mark.parametrize(
    "error",
    [KeyError("market_report"), TypeError("bad state"), _ProviderError(404, spelled="code")],
)
def test_an_error_of_the_engines_own_or_a_refusal_spelled_code_is_for_good(
    tmp_path, stub, error
):
    stub.answer_error = error
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(AgentError, match="for good"):
        judge.ask("ETH-USD", "2024-01-03", "context")


class _CodedError(Exception):
    """An error with an int ``code`` that is not an HTTP status."""

    def __init__(self, code):
        super().__init__("failed")
        self.code = code


# Named as curl_cffi names its errors, without shadowing the builtin in this module:
# every one of curl_cffi's derives from ``CurlError`` and carries a curl error number
# as ``code``; its timeout is ``Timeout``.
CurlError = type("CurlError", (_CodedError,), {})
_CurlConnectionError = type("ConnectionError", (CurlError,), {})
_CurlTimeout = type("Timeout", (CurlError,), {})
_CurlProxyError = type("ProxyError", (CurlError,), {})


class VendorRateLimitError(Exception):
    """Named as the engine's data-vendor throttling error."""


class _VendorThrottled(VendorRateLimitError):
    """A vendor's own subclass, as the engine derives them."""


class _WithResponse(Exception):
    def __init__(self, status):
        super().__init__("http error")
        self.response = type("Response", (), {"status_code": status})()


@pytest.mark.parametrize(
    "error",
    [
        _CurlConnectionError(7),
        _CurlTimeout(28),
        _CurlTimeout(100),
        _CurlProxyError(5),
        _VendorThrottled("throttled"),
        _WithResponse(503),
    ],
)
def test_a_curl_error_a_vendor_error_or_a_status_on_the_response_may_pass(tmp_path, stub, error):
    stub.answer_error = error
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(JudgeUnavailable):
        judge.ask("ETH-USD", "2024-01-03", "context")


def test_a_non_http_code_or_a_refusal_on_the_response_is_for_good(tmp_path, stub):
    for error in (_CodedError(7), _WithResponse(404)):
        stub.answer_error = error
        judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
        with pytest.raises(AgentError, match="for good"):
            judge.ask("ETH-USD", "2024-01-03", "context")


def test_a_suppressed_context_is_not_followed(tmp_path, stub):
    try:
        try:
            raise ConnectionResetError("reset")
        except ConnectionResetError:
            raise KeyError("market_report") from None
    except KeyError as permanent:
        stub.answer_error = permanent
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(AgentError, match="for good"):
        judge.ask("ETH-USD", "2024-01-03", "context")


def test_the_provider_status_is_read_through_the_exception_chain(tmp_path, stub):
    try:
        try:
            raise _ProviderError(429)
        except _ProviderError as inner:
            raise RuntimeError("the graph failed") from inner
    except RuntimeError as wrapped:
        stub.answer_error = wrapped
    judge = TradingAgentsJudge(AgentSettings(), tmp_path, graph_class=stub)
    with pytest.raises(JudgeUnavailable):
        judge.ask("ETH-USD", "2024-01-03", "context")
    # A status that is not a number, or a boolean, is no status, and the error is for good.
    odd = RuntimeError("odd")
    odd.status_code = True  # type: ignore[attr-defined]
    stub.answer_error = odd
    with pytest.raises(AgentError, match="for good"):
        judge.ask("ETH-USD", "2024-01-03", "context")


def test_an_engine_that_cannot_be_set_up_is_an_agent_error(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", None)
    judge = TradingAgentsJudge(AgentSettings(), tmp_path)
    with pytest.raises(AgentError, match=r"cannot be set up \(ModuleNotFoundError"):
        judge.ask("ETH-USD", "2024-01-03", "context")


def test_the_judge_tells_its_settings_and_an_answer_checks_its_fields(tmp_path, stub):
    judge = TradingAgentsJudge(AgentSettings(max_tokens=99), tmp_path, graph_class=stub)
    assert judge.settings["max_tokens"] == 99 and judge.settings["llm_provider"] == "openrouter"
    assert FakeJudge(Rating.BUY).settings == {}
    with pytest.raises(ValueError, match="decision must be a string"):
        Answer(decision=None, rating=Rating.BUY, reports=None, elapsed_seconds=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="elapsed_seconds must be a non-negative"):
        Answer(decision="x", rating=Rating.BUY, reports=None, elapsed_seconds=-1)
    with pytest.raises(ValueError, match="reports must be a mapping"):
        Answer(decision="x", rating=Rating.BUY, reports=[], elapsed_seconds=0)  # type: ignore[arg-type]


def test_the_fake_judge_answers_at_once_and_keeps_no_words():
    judge = FakeJudge(Rating.SELL)
    answer = judge.ask("ETH-USD", "2024-01-03", "context")
    assert judge.model == FAKE_MODEL == "fake"
    assert judge.rehearsal is True and judge.point_in_time is True
    assert answer.rating is Rating.SELL and answer.reports is None
    assert answer.decision.startswith("Rating: Sell")
    assert judge.asked == [("ETH-USD", "2024-01-03", "context")]
    with pytest.raises(ValueError, match="rating must be a Rating"):
        FakeJudge("Sell")  # type: ignore[arg-type]
