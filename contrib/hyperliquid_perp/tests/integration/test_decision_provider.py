"""Tests for the engine decision provider."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.schema import PerpMarketContext
from contrib.hyperliquid_perp.integration import decision_provider as decision_provider_mod
from contrib.hyperliquid_perp.integration.decision_provider import (
    EngineDecisionProvider,
    _classify_engine_error,
)
from contrib.hyperliquid_perp.runtime.decision import DecisionInput

from ..conftest import doc_text, fake_completion_message

D = Decimal


def _perp_ctx(as_of: datetime, coin: str = "BTC") -> PerpMarketContext:
    return PerpMarketContext(
        coin=coin,
        as_of=as_of,
        candle_interval="4h",
        candle_count=200,
        mark_price=D(50000),
        oracle_price=D(50000),
        prev_day_price=D(50000),
        mid_price=D(50000),
        day_change_pct=0.0,  # prev == mark; a reference price exists, so the change is 0
        open_interest=D(0),
        day_ntl_volume=D(0),
        funding_rate=D("0.0001"),
        funding_premium=None,
        funding_zscore_30d=None,
        funding_window_days=30,
        funding_sample_count=0,
        # A healthy indicator set: build_input now applies the shared
        # context_guards.context_refusal guards, and a context without a usable atr_14
        # is (correctly) refused before it reaches the code under test.
        indicators={"rsi_14": 55.0, "ema_20": 50000.0, "ema_50": 49000.0, "atr_14": 1200.0},
    )


def _stub_provider(**attrs):
    """A provider built past ``__init__`` (which imports the engine), pre-fed.

    Skips ``__init__`` (which imports the engine) and presets what the
    build-through path reads: ``_decision`` for the format block, ``_max_pct``
    for the ceiling it advertises, and ``_pricing`` for the books' half of the
    position section. Guard-refusal cases never reach any of them — the guards
    fire first — but a stub that omits them fails with AttributeError instead
    of the refusal under test, which reads as a broken guard.

    Deliberately NOT a stand-in for ``__init__``'s own work: the values below
    are literals, so nothing here exercises how the real provider DERIVES the
    effective ceiling or maps it onto ``PositionPricing``. That derivation is
    pinned by ``test_the_provider_resolves_one_effective_ceiling_at_construction``,
    which constructs the real thing.

    The class-level ``_position_source = None`` keeps every stub book-free
    unless the case wires one.
    """
    from contrib.hyperliquid_perp.domains.perp.marginal_cost import PositionPricing
    from contrib.hyperliquid_perp.paper.config import PaperExecutionConfig

    execution = PaperExecutionConfig()
    provider = object.__new__(EngineDecisionProvider)
    provider._config = {}
    provider._on_blocking_read = None
    provider._decision = DecisionConfig()
    provider._max_pct = 60
    provider._pricing = PositionPricing(
        leverage=D(1),
        grid_min=0,
        grid_max=60,
        grid_step=1,
        taker_fee_rate=execution.taker_fee_rate,
        slippage_bps=execution.fill_model.slippage_bps,
    )
    for name, value in attrs.items():
        setattr(provider, name, value)
    return provider


def test_the_provider_resolves_one_effective_ceiling_at_construction(tmp_path, monkeypatch):
    # The only test that runs the real __init__ (the engine import is stubbed).
    # It exists because _stub_provider hands every other test a hardcoded
    # ceiling, so nothing else can see how the provider DERIVES one.
    #
    # The cap BINDS here, which is what makes it discriminating: a grid ceiling
    # of 40 under an allocation cap of 60 means min() and either input alone
    # give three different answers. Under the usual fixture (grid 100, cap 60)
    # they coincide at 60 and the derivation is unobservable.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.domains.perp.marginal_cost import PositionPricing
    from contrib.hyperliquid_perp.paper.config import PaperExecutionConfig

    monkeypatch.setattr(
        bridge_mod, "_build_engine_config", lambda config: ({"deep_think_llm": "m"}, [])
    )
    risk = RiskConfig(leverage=D(3), max_target_margin_pct=60)
    decision = DecisionConfig(ai_target_margin_max_pct=40, target_margin_step_pct=5)
    provider = EngineDecisionProvider(
        {},
        risk_cfg=risk,
        decision_cfg=decision,
        payload_dir=tmp_path,
        position_source=lambda: None,
    )
    assert provider._max_pct == 40  # not 60 (the cap) and not 100 (the grid default)
    # ...and the ONE ceiling reaches the cost table's rules as ``grid_max``,
    # with every other field mapped from the config it belongs to. Compared
    # whole: a swapped grid_min/grid_max, or a fee read off the wrong block,
    # is invisible to any per-field spot check that only looks at the ceiling.
    execution = PaperExecutionConfig()
    assert provider._pricing == PositionPricing(
        leverage=D(3),
        grid_min=0,
        grid_max=40,
        grid_step=5,
        taker_fee_rate=execution.taker_fee_rate,
        slippage_bps=execution.fill_model.slippage_bps,
    )
    # The books are required, not optional: a wiring that forgot them would
    # otherwise ship a silently position-blind prompt.
    with pytest.raises(TypeError, match="position_source"):
        EngineDecisionProvider({}, risk_cfg=risk, decision_cfg=decision, payload_dir=tmp_path)


def test_request_decision_drives_engine_with_cycle_as_of_not_now(monkeypatch):
    # DA3: the base engine's trade_date must track the cycle's as_of (a single
    # time base with the perp context), not wall-clock now — a late/recovery
    # cycle otherwise feeds today's news alongside the (older) market context.
    import contrib.hyperliquid_perp.integration.trading_graph as tg

    captured: dict = {}

    class _FakeGraph:
        def propagate(self, coin, trade_date, asset_type="crypto"):
            captured.update(coin=coin, trade_date=trade_date, asset_type=asset_type)
            return {"final_trade_decision": _DECISION_JSON}, "signal"

    monkeypatch.setattr(tg, "build_graph", lambda **_kw: _FakeGraph())

    # Bypass the heavy __init__ (engine-config build); request_decision only
    # reads the five attributes set below.
    provider = object.__new__(EngineDecisionProvider)
    provider._context_text = "ctx"
    provider._format_text = "fmt"
    provider._analysts = []
    provider._engine_config = {"deep_think_llm": "model-x"}
    provider._decision = DecisionConfig()

    as_of = datetime(2026, 3, 15, 2, 30, tzinfo=timezone.utc)
    provider.request_decision(DecisionInput(context=_perp_ctx(as_of)))

    assert captured["trade_date"] == "2026-03-15"  # the cycle's as_of date, not today
    assert captured["coin"] == "BTC"
    assert captured["asset_type"] == "crypto"


# --------------------------------------------------------------------------
# request_decision: the completion cap has a name and a measurement (#182)
# --------------------------------------------------------------------------

_DECISION_JSON = (
    '{"decision_mode": "set_target", "target_side": "long", '
    '"requested_target_margin_pct": 1, "confidence": 0.8, '
    '"rationale": "r", "key_risks": ["a risk"]}'
)


_USAGE_LOGGER = "contrib.hyperliquid_perp.integration.completion_usage"


def _completion(node, *, finish_reason, output_tokens, model="m"):
    return (node, finish_reason, output_tokens, model)


def _usage_provider(
    monkeypatch,
    *,
    decision_text=None,
    completions=(),
    cap=4096,
    raise_from_engine=None,
    final_state=None,
):
    """A provider whose stubbed engine feeds ``completions`` into the collector.

    The stub reaches the collector the way the real engine does — through the
    ``callbacks`` kwarg ``build_graph`` receives — and drives it with the
    handler API (start with the node's ``langgraph_node`` metadata, end with an
    ``LLMResult``), so the test exercises how the engine run reads the
    collector, not a hand-set attribute. The engine returns ``final_state``
    whole when given one (the reports-sidecar tests), else a state holding
    just ``decision_text``.
    """
    import uuid

    from langchain_core.outputs import ChatGeneration, LLMResult

    import contrib.hyperliquid_perp.integration.trading_graph as tg

    class _FakeGraph:
        def __init__(self, callbacks):
            (self.collector,) = callbacks

        def propagate(self, coin, trade_date, asset_type="crypto"):
            for node, finish_reason, output_tokens, model in completions:
                run_id = uuid.uuid4()
                metadata = {"langgraph_node": node} if node is not None else {}
                self.collector.on_chat_model_start({}, [[]], run_id=run_id, metadata=metadata)
                message = fake_completion_message(
                    finish_reason=finish_reason, output_tokens=output_tokens, model=model
                )
                self.collector.on_llm_end(
                    LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
                )
            if raise_from_engine is not None:
                raise raise_from_engine
            if final_state is not None:
                return final_state, "signal"
            return {"final_trade_decision": decision_text}, "signal"

    monkeypatch.setattr(tg, "build_graph", lambda **kw: _FakeGraph(kw["callbacks"]))

    provider = object.__new__(EngineDecisionProvider)
    provider._context_text = "ctx"
    provider._format_text = "fmt"
    provider._analysts = []
    provider._engine_config = {"deep_think_llm": "model-x", "max_tokens": cap}
    provider._decision = DecisionConfig()
    return provider


def _decision_input(as_of=None, **kw):
    return DecisionInput(context=_perp_ctx(as_of or datetime(2026, 3, 15, tzinfo=timezone.utc)), **kw)


def test_request_decision_names_the_cap_when_the_decision_completion_was_cut(monkeypatch, caplog):
    # The stop reason, not the parse, says what happened: the target JSON is
    # missing because the cap bound, so the audit tag is truncated_output and
    # the ERROR points at the config number.
    provider = _usage_provider(
        monkeypatch,
        decision_text="Rationale first, then the block:\n```json\n{\"decision_mo",
        completions=[
            _completion("Market Analyst", finish_reason="stop", output_tokens=900),
            _completion("Portfolio Manager", finish_reason="length", output_tokens=4096, model="deep"),
        ],
        cap=4096,
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.is_valid is False
    assert parsed.invalid_reason == "truncated_output"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "the decision completion was truncated" in msg
    # anchored on the word before the figure (issue #290)
    assert "truncated: 4096 output tokens against a cap of 4096 (model" in msg
    assert "model deep" in msg
    assert "fails closed as truncated_output" in msg
    assert "engine.max_completion_tokens" in msg
    # The decision lane owns its verdict: no second, analyst-style WARNING for it.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_request_decision_accepts_a_block_that_survived_the_cut_with_a_warning(monkeypatch, caplog):
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```\nAnd some commentary that was cu",
        completions=[
            _completion("Portfolio Manager", finish_reason="length", output_tokens=4096),
        ],
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.is_valid is True
    warnings_ = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings_) == 1
    assert "target JSON survived the cut" in warnings_[0]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_a_whole_block_that_fails_a_field_rule_is_not_blamed_on_the_cap(monkeypatch, caplog):
    # The completion was cut, but the JSON block came through whole and lost a
    # field: that verdict is the block's own. A WARNING says so; the ERROR that
    # names the cap as the fix must not fire for a failure the cap did not cause.
    provider = _usage_provider(
        monkeypatch,
        decision_text=(
            '```json\n{"decision_mode": "set_target", "target_side": "long", '
            '"requested_target_margin_pct": 1, "rationale": "r", "key_risks": ["a risk"]}\n```\n'
            "and the rationale was cu"
        ),
        completions=[_completion("Portfolio Manager", finish_reason="length", output_tokens=4096)],
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.invalid_reason == "missing_fields"
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "target JSON block was whole" in warning
    assert "the verdict missing_fields is the block's own" in warning


def test_the_verdict_reads_the_last_decision_completion_not_any_truncated_one(monkeypatch, caplog):
    # With structured_output on, the Portfolio Manager node makes two calls: a
    # structured attempt and the free-text fallback whose text is what gets
    # parsed. A cut ATTEMPT followed by a whole fallback with no JSON is a
    # contract failure of the fallback, not a cap failure.
    provider = _usage_provider(
        monkeypatch,
        decision_text="I decline to produce a JSON block.",
        completions=[
            _completion("Portfolio Manager", finish_reason="length", output_tokens=4096),
            _completion("Portfolio Manager", finish_reason="stop", output_tokens=300),
        ],
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())
    assert parsed.invalid_reason == "invalid_output"
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    # The cut attempt is still in the usage line: it was paid for.
    (info,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert info.endswith("truncated: Portfolio Manager")

    # And the reverse order — a whole attempt, then a cut fallback — IS the cap.
    caplog.clear()
    provider = _usage_provider(
        monkeypatch,
        decision_text="Rationale first, then the block:\n```json\n{\"decision_mo",
        completions=[
            _completion("Portfolio Manager", finish_reason="stop", output_tokens=300),
            _completion("Portfolio Manager", finish_reason="length", output_tokens=4096),
        ],
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())
    assert parsed.invalid_reason == "truncated_output"
    (error,) = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "truncated: 4096 output tokens against a cap of 4096 (model" in error


def test_a_run_that_fails_after_a_cut_decision_still_names_the_cap(monkeypatch, caplog):
    # The decision completion hit the cap, then the engine's trailing call
    # raised: no parse happens, so the post-parse verdict never fires. The cap
    # is still named outright, not left as a word in the INFO list.
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    provider = _usage_provider(
        monkeypatch,
        decision_text="",
        completions=[_completion("Portfolio Manager", finish_reason="length", output_tokens=4096)],
        raise_from_engine=TimeoutError("signal processing timed out"),
    )
    with (
        caplog.at_level(logging.INFO, logger=_USAGE_LOGGER),
        pytest.raises(RetryableDecisionError) as exc_info,
    ):
        provider.request_decision(_decision_input())
    (error,) = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "the engine run then failed before the answer could be parsed" in error
    assert "truncated (4096 output tokens against a cap of 4096, model" in error
    assert "the cap bound regardless" in error
    # The api_failed row this becomes says it too: error_message is the
    # RUNBOOK's free-text discriminator for that status, and the log alone
    # would leave the DB row reading as a plain timeout.
    assert exc_info.value.error_type == "timeout"
    assert exc_info.value.message == (
        "signal processing timed out"
        " (decision completion truncated: 4096 output tokens against cap 4096)"
    )


@pytest.mark.parametrize(
    ("returned", "type_name"),
    [
        ({"final_trade_decision": ""}, "dict"),  # a single dict, not a 2-tuple
        ((None, None), "tuple"),  # the pair, but its final_state is not a dict
    ],
)
def test_a_bad_engine_shape_after_a_cut_decision_still_names_the_cap(
    monkeypatch, caplog, returned, type_name
):
    # The other no-parse exit: propagate returned, but nothing the parse can
    # read. Both shapes file as one server_error, and the cap still bound on
    # the decision call and is still named.
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    provider = _usage_provider(
        monkeypatch,
        decision_text="",
        completions=[_completion("Portfolio Manager", finish_reason="length", output_tokens=4096)],
    )
    import contrib.hyperliquid_perp.integration.trading_graph as tg

    built = tg.build_graph  # the stub installed by _usage_provider

    class _OneValue:
        def __init__(self, inner):
            self._inner = inner

        def propagate(self, *a, **k):
            self._inner.propagate(*a, **k)  # drives the collector
            return returned

    monkeypatch.setattr(tg, "build_graph", lambda **kw: _OneValue(built(**kw)))
    with (
        caplog.at_level(logging.INFO, logger=_USAGE_LOGGER),
        pytest.raises(RetryableDecisionError) as exc_info,
    ):
        provider.request_decision(_decision_input())
    assert exc_info.value.error_type == "server_error"
    assert exc_info.value.message == (
        f"engine.propagate returned an unexpected shape ({type_name})"
        " (decision completion truncated: 4096 output tokens against cap 4096)"
    )
    (error,) = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "the engine run then failed before the answer could be parsed" in error


def test_request_decision_warns_per_truncated_analyst_and_keeps_the_decision(monkeypatch, caplog):
    # A cut report is a quality problem, not a contract problem: the cycle
    # continues, the verdict is untouched, and the WARNING names the node.
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[
            _completion("Market Analyst", finish_reason="length", output_tokens=4096, model="quick"),
            _completion("News Analyst", finish_reason="stop", output_tokens=700),
            _completion(None, finish_reason="length", output_tokens=4096),  # outside the graph
            _completion("Portfolio Manager", finish_reason="stop", output_tokens=1200),
        ],
        cap=4096,
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.is_valid is True
    assert parsed.invalid_reason is None
    warnings_ = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings_) == 2
    assert (
        "completion truncated in Market Analyst (model quick): 4096 output tokens against a cap of 4096"
        in warnings_[0]
    )
    assert "completion truncated in (outside the graph)" in warnings_[1]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_request_decision_logs_one_usage_line_per_run_and_nothing_else_when_nothing_was_cut(
    monkeypatch, caplog
):
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[
            _completion("Market Analyst", finish_reason="stop", output_tokens=800),
            _completion("Portfolio Manager", finish_reason="stop", output_tokens=500),
        ],
        cap=8192,
    )
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.is_valid is True
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == ["completion usage: 2 call(s), 1300 output tokens total, cap 8192; truncated: none"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_request_decision_writes_the_usage_sidecar_beside_the_payload(monkeypatch, tmp_path):
    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[
            _completion("Market Analyst", finish_reason="stop", output_tokens=800, model="quick"),
            _completion("Portfolio Manager", finish_reason="length", output_tokens=4096, model="deep"),
        ],
        cap=4096,
    )
    provider.request_decision(
        _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
    )

    sidecar = tmp_path / "BTC-20260315T000000_000000Z.usage.json"
    assert sidecar.exists()
    assert payload.read_bytes() == b"{}"  # the hash-locked payload is untouched
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record == {
        "schema": 1,
        "cap": 4096,
        "call_count": 2,
        "total_output_tokens": 4896,
        "truncated_nodes": ["Portfolio Manager"],
        "calls": [
            {
                "node": "Market Analyst",
                "model": "quick",
                "input_tokens": 10,
                "output_tokens": 800,
                "reasoning_tokens": None,
                "stop_reason": "stop",
                "truncated": False,
            },
            {
                "node": "Portfolio Manager",
                "model": "deep",
                "input_tokens": 10,
                "output_tokens": 4096,
                "reasoning_tokens": None,
                "stop_reason": "length",
                "truncated": True,
            },
        ],
    }
    # Without a payload path (the one-shot / test harnesses) there is nowhere
    # to put a sidecar, and nothing is written anywhere.
    before = sorted(tmp_path.iterdir())
    provider.request_decision(_decision_input())
    assert sorted(tmp_path.iterdir()) == before


def test_request_decision_writes_the_reports_sidecar_beside_the_payload(monkeypatch, tmp_path):
    # The replay plan's PR 0: the hook is wired in — what the agents wrote on
    # the way to the decision lands next to the payload (the record's shape is
    # pinned in tests/integration/test_decision_reports.py).
    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        final_state={
            "market_report": "market says up",
            "final_trade_decision": f"```json\n{_DECISION_JSON}\n```",
        },
    )

    parsed = provider.request_decision(
        _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
    )

    assert parsed.is_valid  # the decision itself is untouched by the recording
    sidecar = tmp_path / "BTC-20260315T000000_000000Z.reports.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["schema"] == 1
    assert record["selected_analysts"] == []  # the stub provider's analyst list, verbatim
    assert record["market_report"] == "market says up"
    assert record["final_trade_decision"] == f"```json\n{_DECISION_JSON}\n```"
    assert payload.read_bytes() == b"{}"  # the hash-locked payload is untouched


def test_the_reports_sidecar_is_kept_for_a_cycle_that_fails_closed(monkeypatch, tmp_path):
    # Written before the parse: a cycle whose target JSON is missing still
    # keeps the reports that led there — that cycle is the one worth replaying.
    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        final_state={"market_report": "market says up", "final_trade_decision": "no block here"},
    )

    parsed = provider.request_decision(
        _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
    )

    assert parsed.is_valid is False
    record = json.loads(
        (tmp_path / "BTC-20260315T000000_000000Z.reports.json").read_text(encoding="utf-8")
    )
    assert record["market_report"] == "market says up"


def test_both_sidecar_write_failures_are_logged_in_order_and_neither_costs_the_decision(
    monkeypatch, tmp_path, caplog
):
    from pathlib import Path

    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[_completion("Portfolio Manager", finish_reason="stop", output_tokens=500)],
    )

    def _refuse(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _refuse)
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(
            _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
        )

    assert parsed.is_valid is True
    # Both sidecars share the disk and the one writer: each failure is its own
    # ERROR (usage first — written on the engine's exit — then the reports),
    # and neither costs the decision.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "completion usage sidecar could not be written; the decision is unaffected",
        "decision reports sidecar could not be written; the decision is unaffected",
    ]
    assert all(r.exc_info is not None for r in errors)  # the tracebacks travel with them


def test_a_failure_in_the_usage_reporting_itself_is_the_wrappers_own_line(monkeypatch, caplog):
    # The sidecar write moved under common.sidecar's own never-raise, so the
    # wrapper's line now covers the rest of report_usage: the truncation scan
    # and the log formatting. It must still be there, still with a traceback.
    from contrib.hyperliquid_perp.integration.completion_usage import CompletionUsageCollector

    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[_completion("Portfolio Manager", finish_reason="stop", output_tokens=500)],
    )

    def _boom(self):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(CompletionUsageCollector, "truncated_calls", _boom)
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(_decision_input())

    assert parsed.is_valid is True
    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error.getMessage() == "completion usage could not be reported; the decision is unaffected"
    assert error.exc_info is not None


def test_an_engine_failure_leaves_the_usage_sidecar_and_no_reports_sidecar(monkeypatch, tmp_path):
    # The operator's pairing rule (RUNBOOK §5): usage without reports means
    # the engine failed. It rests on two placements in EngineRun.drive —
    # report_usage in its finally, write_decision_reports after the shape
    # guards — so pin the pairing on disk, not the placements.
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        decision_text="never reached",
        completions=[_completion("Market Analyst", finish_reason="stop", output_tokens=100)],
        raise_from_engine=RuntimeError("provider down"),
    )

    with pytest.raises(RetryableDecisionError):
        provider.request_decision(
            _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
        )

    assert (tmp_path / "BTC-20260315T000000_000000Z.usage.json").exists()
    assert not (tmp_path / "BTC-20260315T000000_000000Z.reports.json").exists()


def test_a_drifted_engine_shape_leaves_the_same_pairing(monkeypatch, tmp_path):
    # The other api_failed exit — propagate returned, but not the
    # (final_state, signal) pair — lands after drive's finally too: same pairing.
    import contrib.hyperliquid_perp.integration.trading_graph as tg
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(monkeypatch, decision_text="never reached", completions=[])
    built = tg.build_graph  # the stub installed by _usage_provider

    class _OneValue:
        def __init__(self, inner):
            self._inner = inner

        def propagate(self, *a, **k):
            self._inner.propagate(*a, **k)
            return {"final_trade_decision": ""}  # a single dict, not a 2-tuple

    monkeypatch.setattr(tg, "build_graph", lambda **kw: _OneValue(built(**kw)))

    with pytest.raises(RetryableDecisionError):
        provider.request_decision(
            _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
        )

    assert (tmp_path / "BTC-20260315T000000_000000Z.usage.json").exists()
    assert not (tmp_path / "BTC-20260315T000000_000000Z.reports.json").exists()


def test_a_logging_failure_in_report_usage_does_not_cost_the_usage_sidecar(
    monkeypatch, tmp_path, caplog
):
    # The sidecar write sits in report_usage's finally: break the logging
    # half (the per-call label) and the measurement still lands, beside the
    # wrapper's own ERROR line.
    import contrib.hyperliquid_perp.integration.completion_usage as cu

    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")
    provider = _usage_provider(
        monkeypatch,
        decision_text=f"```json\n{_DECISION_JSON}\n```",
        completions=[_completion("Portfolio Manager", finish_reason="length", output_tokens=4096)],
    )

    def _boom(call):
        raise RuntimeError("label failed")

    monkeypatch.setattr(cu, "_call_label", _boom)
    with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
        parsed = provider.request_decision(
            _decision_input(input_payload_path=str(payload), input_payload_hash="sha256:x")
        )

    assert parsed.is_valid is True
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "completion usage could not be reported; the decision is unaffected" in errors
    sidecar = tmp_path / "BTC-20260315T000000_000000Z.usage.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["call_count"] == 1


def test_usage_is_reported_even_when_the_engine_run_raises(monkeypatch, caplog):
    # Ten completions that end in a provider exception were still paid for:
    # the usage line is written on the raising exit too, before the retryable
    # classification the scheduler ladder relies on.
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    provider = _usage_provider(
        monkeypatch,
        decision_text="",
        completions=[_completion("Market Analyst", finish_reason="stop", output_tokens=800)],
        cap=8192,
        raise_from_engine=TimeoutError("upstream timed out"),
    )
    with (
        caplog.at_level(logging.INFO, logger=_USAGE_LOGGER),
        pytest.raises(RetryableDecisionError) as exc_info,
    ):
        provider.request_decision(_decision_input())

    assert exc_info.value.error_type == "timeout"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == ["completion usage: 1 call(s), 800 output tokens total, cap 8192; truncated: none"]


def test_the_runbook_names_the_truncation_tag_and_the_sidecar():
    # RUNBOOK §5 and the §7 table are where an operator meets these two
    # spellings; the code that emits them is the pin's other half.
    from contrib.hyperliquid_perp.common.sidecar import SIDECAR_SCHEMA
    from contrib.hyperliquid_perp.domains.perp.target_decision import TRUNCATED_OUTPUT

    runbook = doc_text("RUNBOOK.md")
    assert f"`risk_reason = {TRUNCATED_OUTPUT}`" in runbook
    assert "the decision completion was truncated" in runbook
    assert "completion truncated in <node>" in runbook
    assert "completion usage:" in runbook
    # The sidecar contract's four operator-facing spellings (§5): both log
    # templates and the stamp value, emitted by common.sidecar, and the
    # reports sidecar's file name.
    assert "sidecar could not be written; the decision is unaffected" in runbook
    assert "value JSON cannot carry was stored as its str" in runbook
    assert f"`schema: {SIDECAR_SCHEMA}`" in runbook
    assert "`<payload>.reports.json`" in runbook
    assert "`<payload>.usage.json`" in runbook
    # The no-parse exit's two spellings: the ERROR line and the error_message
    # suffix the api_failed row carries (the code side is pinned by
    # test_a_run_that_fails_after_a_cut_decision_still_names_the_cap).
    assert "and the engine run then failed before the answer could be parsed" in runbook
    assert "(decision completion truncated: N output tokens against cap C)" in runbook


def test_build_input_payload_write_failure_rides_retry_ladder(tmp_path, monkeypatch):
    # An environmental filesystem failure on the audit payload (disk full,
    # permissions) must not tear down the daemon (exit 2, SL/TP unwatched):
    # build_input classifies it into the §3.1 ladder like its sibling
    # environmental failures, so the worst case is an api_failed cycle whose
    # error_message names the cause.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.domains.perp import context_guards as guards_mod
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of)
    # **kw absorbs on_blocking_read: the live provider passes the kill-switch
    # refresh so _build_context's market reads do not form one unrefreshed chain.
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))
    monkeypatch.setattr(guards_mod, "warmup_threshold", lambda config: 1)

    # A FILE where the payload directory must go: mkdir(exist_ok=True) still
    # raises FileExistsError (an OSError) — the same landing zone as ENOSPC.
    blocked = tmp_path / "payloads"
    blocked.write_text("not a directory", encoding="utf-8")
    provider = _stub_provider(_payload_dir=blocked)

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=as_of)
    assert exc_info.value.error_type == "server_error"
    assert "payload write failed" in exc_info.value.message


def test_build_input_files_an_unreadable_answer_apart_from_a_disconnect(monkeypatch):
    """§6.2: ``error_type`` is the machine-readable half of the record.

    ``MalformedResponseError`` is a SUBCLASS of ``ExchangeError``, so the ORDER
    of build_input's two except clauses is the entire mechanism — reversing them
    silently restores the old collapsed behaviour. That is why the transport
    cases are pinned right beside the new one instead of left implicit: the
    claim under test is the SPLIT, and a one-sided test cannot see it close.

    All three still ride the §3.1 ladder identically (its delays index on
    attempt count, not on class), so what changes is only what the durable trail
    says about which fault happened.
    """
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
        ExchangeRequestError,
        ExchangeThrottledError,
        MalformedResponseError,
    )
    from contrib.hyperliquid_perp.persistence.repository import ERROR_TYPES
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    cases = [
        # Could not REACH the venue — self-healing, and retry is the whole
        # answer. Unpinned until now (issue #47's premise is that both halves
        # landed on one label), so both directions of the split are covered.
        (ExchangeRequestError("read timed out"), "connection"),
        (ExchangeThrottledError("429 slow down"), "connection"),
        # The venue ANSWERED and the answer was unusable. Not self-healing: a
        # feed that misroutes one read misroutes the next, so filing it as a
        # disconnect made per-class readings of decision_attempts count a
        # systematically broken feed as one transient blip.
        (MalformedResponseError("candleSnapshot payload not recognised: {}"), "malformed_response"),
    ]
    for raised, expected_class in cases:

        def _raise(config, coin, _exc=raised, **kw):
            raise _exc

        monkeypatch.setattr(bridge_mod, "_build_context", _raise)
        provider = _stub_provider()

        with pytest.raises(RetryableDecisionError) as exc_info:
            provider.build_input(coin="BTC", as_of=as_of)
        assert exc_info.value.error_type == expected_class, (
            f"{type(raised).__name__} was filed as {exc_info.value.error_type!r}"
        )
        # The label has to be one the write boundary accepts: a class the
        # vocabulary does not carry raises at insert time instead, turning a
        # recorded api_failed cycle into an unhandled crash.
        assert exc_info.value.error_type in ERROR_TYPES
        # The free-text half still carries the venue's own words either way.
        assert str(raised) in exc_info.value.message


@pytest.mark.parametrize(
    ("candle_count", "indicators", "expected_msg"),
    [
        # Under-warmed feed: candle_count 100 sits below the monkeypatched
        # threshold (150) but above the default indicator set's 50, so the
        # daemon's build_input must both ride the one-shot path's warm-up
        # guard and actually consult warmup_threshold(config) — a guard
        # falling back to a hardcoded/default threshold clears 100 and
        # reports a different refusal. Keys present with all-None values
        # (compute_indicators' real under-warm output): the shape also
        # satisfies the dead-set/regime guards, so a guard-order swap would
        # surface their messages instead and fail this case.
        (
            100,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "under-warmed",
        ),
        # Fully-dead known-indicator set (stockstats broken): must become an
        # api_failed cycle (no AI spend), not a prompt asserting a
        # fabricated-calm RANGING regime every 4h.
        (
            200,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "every technical indicator failed",
        ),
        # atr_14 absent (dropped from `indicators:` on a pre-upgrade config):
        # classify_regime would silently default to RANGING, hiding a volatile
        # market.
        (
            200,
            {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0},
            "atr_14 is unavailable",
        ),
        # A dead EMA is just as regime-critical: RANGING would also hide a
        # trending market.
        (
            200,
            {"rsi_14": 55.0, "ema_20": None, "ema_50": 59000.0, "atr_14": 250.0},
            "ema_20 is unavailable",
        ),
    ],
)
def test_build_input_refuses_untradeable_indicators(
    monkeypatch, candle_count, indicators, expected_msg
):
    # The daemon shares the one-shot path's pre-LLM context guards (see
    # context_guards.context_refusal) and rides them down the retry ladder.
    from types import SimpleNamespace

    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.domains.perp import context_guards as guards_mod
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    # Threshold monkeypatched to 150 — a value the default indicator set's 50
    # can't mimic: candle_count 200 clears the warm-up gate, 100 exercises it
    # only if the guard really reads warmup_threshold(config). Patched on the
    # DEFINING module: the guard looks the name up in context_guards' globals,
    # and engine_bridge deliberately keeps no second binding to patch.
    ctx = SimpleNamespace(candle_count=candle_count, indicators=indicators)
    # Both keyword-only parameters spelled out: the provider always passes
    # them, so a stand-in missing either raises TypeError before the guard
    # under test runs.
    monkeypatch.setattr(
        bridge_mod,
        "_build_context",
        lambda config, coin, on_blocking_read=None, position=None: (ctx, None),
    )
    monkeypatch.setattr(guards_mod, "warmup_threshold", lambda config: 150)

    # The guard still fires before the payload attributes are ever read; the
    # ceiling and the (absent) books are resolved ahead of it.
    provider = _stub_provider()

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc))
    assert exc_info.value.error_type == "server_error"
    assert expected_msg in exc_info.value.message


def test_build_input_refuses_a_stalled_candle_feed(monkeypatch):
    # A stalled feed clears the other three guards (every indicator computes,
    # the regime reads healthy), so without this one the daemon would spend a
    # paid cycle reasoning about a market 20h in the past — and drag the
    # analysts' research window back with it, since as_of becomes trade_date
    # (see test_request_decision_drives_engine_with_cycle_as_of_not_now).
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=20))  # 4h bars: past the 3 x 4h bound
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))

    provider = _stub_provider()

    with pytest.raises(RetryableDecisionError) as exc_info:
        provider.build_input(coin="BTC", as_of=as_of)
    # A §6.2 class, so it rides the §3.1 ladder to an api_failed cycle rather
    # than tearing the daemon down while SL/TP still need watching — and
    # specifically ``stale_market_data``, not the ``server_error`` that every
    # environmental failure shares: this one does not heal on its own, and the
    # acceptance validators count consecutive ones (issue #50).
    assert exc_info.value.error_type == "stale_market_data"
    assert "freshness limit" in exc_info.value.message
    assert "2026-03-14T12:00:00Z" in exc_info.value.message


def test_build_input_measures_freshness_against_the_cycle_clock(tmp_path, monkeypatch):
    # The discriminator for WHICH clock the guard reads: this candle is one
    # hour old relative to the cycle's own as_of, and months stale against the
    # real wall clock (the fixture date is fixed, so the gap only grows). A
    # guard calling datetime.now() itself would refuse it; the daemon's single
    # time base must not.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))

    provider = _stub_provider(
        _payload_dir=tmp_path / "payloads", _engine_config={"deep_think_llm": "model-x"}
    )

    decision_input = provider.build_input(coin="BTC", as_of=as_of)
    assert decision_input.candle_end == ctx.as_of  # built through, not refused


def test_build_input_carries_the_context_shape_beside_the_prompt_version(tmp_path, monkeypatch):
    # Issue #97: the prompt's structure rides on the DecisionInput (so the
    # ai_inputs row gets it) AND in the payload JSON (so the artifact is
    # self-describing), computed from the very context that was rendered.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.domains.perp.prompt_context import (
        context_shape,
        render_market_context,
    )

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))

    provider = _stub_provider(
        _payload_dir=tmp_path / "payloads", _engine_config={"deep_think_llm": "model-x"}
    )

    decision_input = provider.build_input(coin="BTC", as_of=as_of)
    expected = context_shape(ctx)
    # Spelled out so a silent change in what the shape covers is visible here.
    assert expected == "price|market|funding|indicators(rsi_14,ema_20,ema_50,atr_14)"
    assert decision_input.context_shape == expected
    with open(decision_input.input_payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["context_shape"] == expected
    assert payload["prompt_version"] == decision_input.prompt_version
    # The third key (issue #129): a digest of the format block AS RENDERED
    # for this provider — its config and its effective ceiling — so the row,
    # the payload and the text the model is shown all agree on one value.
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        decision_format_instructions,
        format_fingerprint,
    )

    expected_fingerprint = format_fingerprint(
        decision_format_instructions(DecisionConfig(), max_pct=60)
    )
    assert decision_input.format_fingerprint == expected_fingerprint
    assert payload["format_fingerprint"] == expected_fingerprint
    assert format_fingerprint(payload["format_instructions"]) == expected_fingerprint
    # The payload's own copy of the prompt text, which nothing else pins: the
    # stored artifact is what an audit reads months later, and the model is fed
    # a SEPARATE attribute (``self._context_text``), so a payload written empty
    # — or without the key — would keep every cycle trading correctly while the
    # §5 artifact and the hash over it described nothing.
    assert payload["context_text"] == render_market_context(ctx)


def _regime_log_lines(caplog) -> list[str]:
    """The ``prompt_regime:`` lines the provider logged, in order."""
    from contrib.hyperliquid_perp.common.prompt_regime import PROMPT_REGIME_PREFIX

    return [
        r.getMessage() for r in caplog.records if r.getMessage().startswith(PROMPT_REGIME_PREFIX)
    ]


def test_build_input_logs_the_prompt_regime_once_and_again_only_when_it_flips(
    tmp_path, monkeypatch, caplog
):
    # Issue #163: the daemon says which bucket its prompt lands in — at the
    # first cycle (the startup line a YAML edit + restart used to need
    # ``validate`` for) and then only when the triple flips. One site serves
    # both lanes: paper and live each build this provider. Rendered by the
    # function ``validate`` and ``--context-only`` print through, so the
    # three surfaces grep alike.
    import dataclasses
    import logging

    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.common.prompt_regime import (
        prompt_regime_line,
    )

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    contexts = [ctx]
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (contexts[-1], None))
    provider = _stub_provider(
        _payload_dir=tmp_path / "payloads", _engine_config={"deep_think_llm": "model-x"}
    )

    with caplog.at_level(logging.INFO, logger="contrib.hyperliquid_perp.integration.decision_provider"):
        first = provider.build_input(coin="BTC", as_of=as_of)
        provider.build_input(coin="BTC", as_of=as_of + timedelta(hours=4))
    expected = prompt_regime_line(first.prompt_version, first.context_shape, first.format_fingerprint)
    assert _regime_log_lines(caplog) == [expected]  # once, not per cycle

    # A section appears mid-run (here: an indicator joins the set) — the
    # bucket flips, and the log says so exactly once more.
    contexts.append(dataclasses.replace(ctx, indicators={**ctx.indicators, "macd": 1.0}))
    with caplog.at_level(logging.INFO, logger="contrib.hyperliquid_perp.integration.decision_provider"):
        flipped = provider.build_input(coin="BTC", as_of=as_of + timedelta(hours=8))
    assert flipped.context_shape != first.context_shape
    assert _regime_log_lines(caplog) == [
        expected,
        prompt_regime_line(flipped.prompt_version, flipped.context_shape, flipped.format_fingerprint),
    ]


def test_build_input_logs_the_regime_only_for_a_cycle_that_reached_its_payload(
    tmp_path, monkeypatch, caplog
):
    # The line is the LAST thing build_input does that can fail: a cycle that
    # dies writing its payload (disk full) leaves no ai_inputs row, so a line
    # logged for it would claim a bucket ``validate`` never counts — and,
    # having been "logged", would silence the first cycle that does reach
    # the store. RUNBOOK §5 promises the line on the first SUCCESSFUL cycle.
    import logging
    from pathlib import Path

    import contrib.hyperliquid_perp.engine_bridge as bridge_mod
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))
    provider = _stub_provider(
        _payload_dir=tmp_path / "payloads", _engine_config={"deep_think_llm": "model-x"}
    )
    real_write = Path.write_bytes
    writes: list[Path] = []

    def disk_full_once(self, data):
        writes.append(self)
        if len(writes) == 1:
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", disk_full_once)

    with caplog.at_level(logging.INFO, logger="contrib.hyperliquid_perp.integration.decision_provider"):
        with pytest.raises(RetryableDecisionError, match="payload write failed"):
            provider.build_input(coin="BTC", as_of=as_of)
        assert _regime_log_lines(caplog) == []  # nothing reached the store; nothing claimed
        provider.build_input(coin="BTC", as_of=as_of + timedelta(hours=4))
    assert len(_regime_log_lines(caplog)) == 1  # the first cycle that got through says so


def test_the_bookless_omission_is_worded_apart_from_the_pricers(caplog):
    # Issue #161: ``ctx.position is None`` has two live causes — the books do
    # not exist yet (this provider) and the pricer refusing non-positive
    # equity (``marginal_cost``) — that render the same prompt and the same
    # ``context_shape``. No store column tells them apart (a recorded
    # decision); the two WARNING lines are the only record, so the handle
    # that tells them apart is the ``reason=`` member on the shared template
    # (issue #197), not the English. The pricer's half is pinned in
    # test_marginal_cost.
    import logging

    from contrib.hyperliquid_perp.common.prompt_regime import position_section_omitted

    provider = _stub_provider(_position_source=lambda: None)
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.integration.decision_provider"):
        assert provider._read_books() is None
    [message] = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("position section omitted")
    ]
    assert message.startswith(position_section_omitted("no_books", ""))


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("request timed out after 60s"), "timeout"),
        (RuntimeError("HTTP 429 Too Many Requests"), "rate_limit"),
        (RuntimeError("Connection refused by host"), "connection"),
        (RuntimeError("something exploded"), "server_error"),
        # A bare "429" used to match here, so any larger number containing those
        # three digits — an oid, an epoch-ms timestamp, a price — filed as a rate
        # limit. The sibling classifier in sdk_client.py deleted the marker for
        # this reason; this copy kept it (2026-08-01 round-18 concept scan).
        (RuntimeError("run 1429 aborted at 14290 ms"), "server_error"),
        # And the class name alone still carries a real one, which is how the
        # SDKs actually surface it.
        (type("RateLimitError", (RuntimeError,), {})("slow down"), "rate_limit"),
        # The spaced phrase — the other half of the argument for deleting "429",
        # and the half nothing asserted (2026-08-01 round-19 mutation probe).
        (RuntimeError("provider rate limit exceeded"), "rate_limit"),
        # Order pinned in the two remaining collisions: rate_limit beats
        # connection, and timeout beats rate_limit.
        (RuntimeError("connection reset while rate limit backoff ran"), "rate_limit"),
        (RuntimeError("rate limit wait timed out"), "timeout"),
        # Order pinned: when a message matches both vocabularies, timeout wins.
        (RuntimeError("connection timeout while reading"), "timeout"),
    ],
)
def test_classify_engine_error(exc, expected):
    assert _classify_engine_error(exc) == expected


def test_the_prompt_version_is_pinned_to_the_block_it_versions():
    """The version stamp and the text it versions must move together.

    RUNBOOK §4's A/B exception deliberately lets one run straddle a
    prompt-only deploy and segments the before/after populations on
    ``ai_inputs.prompt_version`` — which makes this stamp the ONLY thing
    separating them. It lives in ``common/prompt_regime.py`` (issue #197)
    while the text it versions lives in ``domains/perp/target_decision.py``,
    and nothing makes the constant track the text — no assertion but this
    one relates them (the suite's other reference, in test_main, only echoes
    the value). So a prompt edit that forgot the bump would merge the two
    populations into one bucket and the merge would be invisible in the
    data: the query still returns a clean two-value split.

    The digest covers the block as rendered from ``DecisionConfig()``, so a
    changed config DEFAULT trips it too. That is the intended reading rather
    than a false positive: the deployed prompt text really did change, and the
    RUNBOOK already requires a code-default change to ship with a fresh run-id.

    If this fails because you changed the prompt on purpose: bump
    ``PROMPT_VERSION`` to a value that has never been used before (rollbacks
    included — see the RUNBOOK), then update the digest here.
    """
    from contrib.hyperliquid_perp.common import prompt_regime
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        DecisionConfig,
        decision_format_instructions,
        format_fingerprint,
    )

    # The same digest the daemon stamps on ai_inputs.format_fingerprint (issue
    # #129), so "the block changed" means one thing in the test and the data.
    block = decision_format_instructions(DecisionConfig())
    digest = format_fingerprint(block)
    # Compared as one tuple so a mismatch shows both halves at once — which one
    # drifted is the whole diagnosis.
    # v4 (2026-08-27) bumped the version for the CONTEXT's new Position:
    # section; the format block itself did not change, so v4's digest was
    # v3's. v5 (2026-09-01) changed the FORMAT block: the three gate
    # thresholds are no longer rendered as numbers (marginal-cost plan PR-B).
    # v6 (2026-09-22) bumped for the CONTEXT again — the Last fill: line's
    # age unit (issue #288) — so v6's digest is v5's.
    assert (prompt_regime.PROMPT_VERSION, digest) == ("phase2-target-v6", "947e85a9b7b750f1")
    # The daemon's spelling (``integration.decision_provider``) is the same
    # object, not a second declaration that would keep equal today and fork
    # the next time one side moves.
    assert decision_provider_mod.PROMPT_VERSION is prompt_regime.PROMPT_VERSION


def _book(**overrides):
    """The books as ``read_books`` returns them: an open long, one fill booked."""
    from contrib.hyperliquid_perp.persistence.models import AccountLedger, PositionState
    from contrib.hyperliquid_perp.runtime.position_facts import BookFacts

    base = {
        "ledger": AccountLedger(wallet_balance=D(1000)),
        "position": PositionState(coin="BTC", size=D("0.005"), entry_price=D(50000)),
        "last_fill_time": "2026-03-14T20:00:00+00:00",
    }
    return BookFacts(**{**base, **overrides})


def _provider_with_source(tmp_path, monkeypatch, ctx, source, handed=None):
    """A stub provider wired to ``source``, over a ``_build_context`` stand-in.

    ``handed`` (a dict) records the keyword arguments the stand-in was called
    with — the seam these tests are about since the position section moved
    into the builder (issue #134): the provider's job is to READ the books and
    hand them over, and what the builder then prices out of them is pinned in
    ``tests/domains/test_context_builder.py``.
    """
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    def _stand_in(config, coin, **kw):
        if handed is not None:
            handed.setdefault("order", []).append("fetch")
            handed.update(kw)
        return ctx, None

    monkeypatch.setattr(bridge_mod, "_build_context", _stand_in)
    return _stub_provider(
        _payload_dir=tmp_path / "payloads",
        _engine_config={"deep_think_llm": "model-x"},
        _position_source=source,
    )


def test_build_input_hands_the_books_and_the_effective_ceiling_to_the_builder(
    tmp_path, monkeypatch
):
    # Prompt v4: the provider reads the run's books through its position
    # source and hands them to the builder, which prices the Position: section
    # onto the very context it is assembling (issue #134). Pinned here: the
    # books arrive whole, and the grid they are priced against is the
    # EFFECTIVE ceiling (cap 60 under grid max 100) — the same value the
    # format block advertises, so the cost table and the ceiling cannot
    # disagree.
    from contrib.hyperliquid_perp.domains.perp.marginal_cost import PositionInputs
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        decision_format_instructions,
    )
    from contrib.hyperliquid_perp.paper.config import PaperExecutionConfig

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    handed = {}
    book = _book()
    provider = _provider_with_source(tmp_path, monkeypatch, ctx, lambda: book, handed)

    decision_input = provider.build_input(coin="BTC", as_of=as_of)
    position = handed["position"]
    assert isinstance(position, PositionInputs)
    # The builder gets the prompt-side view of the ONE read; the driver's
    # ai_inputs row gets the whole read, on the input (issue #134) — the same
    # object, so the two cannot describe different books.
    assert position.book == book.position_facts
    assert decision_input.books is book
    assert position.pricing.grid_max == 60
    assert position.pricing.leverage == D(1)
    assert position.pricing.taker_fee_rate == PaperExecutionConfig().taker_fee_rate
    assert position.pricing.slippage_bps == PaperExecutionConfig().fill_model.slippage_bps
    # ...and the ceiling the model is TOLD about is rendered from that SAME
    # number. Compared against the whole block rather than a "60" substring:
    # the block mentions other numbers, so a substring would pass on a
    # ceiling that drifted to any value whose text happens to contain 60.
    with open(decision_input.input_payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["format_instructions"] == decision_format_instructions(
        DecisionConfig(), max_pct=position.pricing.grid_max
    )


def test_build_input_without_a_position_source_stays_position_blind(tmp_path, monkeypatch):
    # A provider built through object.__new__ never wires a source (the
    # constructor requires one, so no production wiring can reach this): the
    # builder is told so explicitly rather than left to infer it.
    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    handed = {}
    provider = _provider_with_source(tmp_path, monkeypatch, ctx, None, handed)
    provider.build_input(coin="BTC", as_of=as_of)
    # The handoff is the whole assertion: the stand-in returns a fixed
    # position-free context whatever it is passed, so anything read off the
    # result would pass just as well on a fully-populated PositionInputs. What
    # the builder then does with a None is pinned in test_context_builder.
    assert handed["position"] is None


def test_build_input_omits_the_section_when_the_books_do_not_exist_yet(
    tmp_path, monkeypatch, caplog
):
    import logging

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    handed = {}
    provider = _provider_with_source(tmp_path, monkeypatch, ctx, lambda: None, handed)
    with caplog.at_level(logging.WARNING):
        provider.build_input(coin="BTC", as_of=as_of)
    # "No books yet" reaches the builder as the same position-blind None a
    # missing source does — one state, not two — and says so in the log, which
    # is the only place the two causes are told apart.
    assert handed["position"] is None
    assert "position section omitted (reason=no_books)" in caplog.text


def test_build_input_reads_the_books_once_before_the_fetch_even_if_the_cycle_is_refused(
    tmp_path, monkeypatch
):
    # The deliberate cost of moving the section into the builder (issue #134):
    # the books are read BEFORE the market fetch, so a context the guards go
    # on to refuse has already made its three local SQLite reads. They touch
    # no network and spend nothing. What must NOT drift is the count — one
    # read per build_input, never one per section-rendering site.
    from contrib.hyperliquid_perp.runtime.decision import RetryableDecisionError

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=20))  # stale: refused
    handed = {}

    def _source():
        handed.setdefault("order", []).append("read")
        return _book()

    provider = _provider_with_source(tmp_path, monkeypatch, ctx, _source, handed)
    with pytest.raises(RetryableDecisionError):
        provider.build_input(coin="BTC", as_of=as_of)
    # Both halves of the name measured: the read happened, it happened once,
    # and it happened before the fetch — the ordering is what lets the builder
    # price the section at the very snapshot that fetch returns.
    assert handed["order"] == ["read", "fetch"]
