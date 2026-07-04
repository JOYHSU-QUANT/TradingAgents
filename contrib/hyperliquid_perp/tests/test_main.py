"""Tests for the orchestration glue in ``main.py``.

Covers the two deterministic seams that do not need a live engine or network:
``_build_engine_config`` (config overlay onto the engine DEFAULT_CONFIG) and
``_load_position`` (wallet/​error/​success branches). The full ``run_engine``
path needs a key + network and is left to integration testing.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp import main as main_mod
from contrib.hyperliquid_perp.domains.perp.schema import AccountSnapshot, PerpPosition
from contrib.hyperliquid_perp.exchanges.hyperliquid.account import HyperliquidAccount
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    MalformedResponseError,
)

# --------------------------------------------------------------------------
# _build_engine_config — overlay the perp ``engine`` block onto DEFAULT_CONFIG
# --------------------------------------------------------------------------


def test_indicator_names_handles_null_and_empty():
    # A bare `indicators:` in YAML parses to None — it must fall back to the
    # defaults, not crash the downstream iteration; an explicit [] is honoured.
    assert main_mod._indicator_names({}) == main_mod._DEFAULT_INDICATORS
    assert main_mod._indicator_names({"indicators": None}) == main_mod._DEFAULT_INDICATORS
    assert main_mod._indicator_names({"indicators": []}) == []
    assert main_mod._indicator_names({"indicators": ["rsi_14"]}) == ["rsi_14"]


def test_build_engine_config_defaults():
    engine_config, selected = main_mod._build_engine_config({})

    assert engine_config["llm_provider"] == "openrouter"
    # backend_url is forced to None so the OpenRouter client uses its own default.
    assert engine_config["backend_url"] is None
    # deep/quick fall back to the engine DEFAULT_CONFIG values, untouched.
    from tradingagents.default_config import DEFAULT_CONFIG

    assert engine_config["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert engine_config["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert selected == ["market", "social", "news"]


def test_build_engine_config_overrides():
    config = {
        "engine": {
            "llm_provider": "custom",
            "deep_think_llm": "deep-x",
            "quick_think_llm": "quick-y",
            "selected_analysts": ["market"],
        }
    }
    engine_config, selected = main_mod._build_engine_config(config)

    assert engine_config["llm_provider"] == "custom"
    assert engine_config["deep_think_llm"] == "deep-x"
    assert engine_config["quick_think_llm"] == "quick-y"
    assert engine_config["backend_url"] is None
    assert selected == ["market"]


def test_build_engine_config_does_not_mutate_default_config():
    from tradingagents.default_config import DEFAULT_CONFIG

    before = DEFAULT_CONFIG["llm_provider"]
    main_mod._build_engine_config({"engine": {"llm_provider": "openrouter"}})
    assert DEFAULT_CONFIG["llm_provider"] == before  # overlay is on a copy


def test_build_engine_config_null_values_fall_back_to_defaults():
    # A present-but-null YAML value (key left blank) must fall back to the default,
    # not pass None into the LLM client where it fails deep in the engine.
    from tradingagents.default_config import DEFAULT_CONFIG

    config = {
        "engine": {
            "llm_provider": None,
            "deep_think_llm": None,
            "quick_think_llm": None,
            "selected_analysts": None,
        }
    }
    engine_config, selected = main_mod._build_engine_config(config)
    assert engine_config["llm_provider"] == "openrouter"
    assert engine_config["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert engine_config["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert selected == ["market", "social", "news"]


def test_build_engine_config_preserves_explicit_empty_analysts():
    # An explicit empty list is a deliberate "no analysts" choice — it must be
    # preserved, not silently replaced by the default suite (None still falls back).
    _engine_config, selected = main_mod._build_engine_config({"engine": {"selected_analysts": []}})
    assert selected == []


# --------------------------------------------------------------------------
# _load_position — wallet / error / success branches
# --------------------------------------------------------------------------


class _FakeAccount:
    """Stand-in for HyperliquidAccount; ``snapshot`` is returned or raised."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def __call__(self, _client):  # HyperliquidAccount(client)
        return self

    def get_account_snapshot(self, _addr):
        if isinstance(self._snapshot, Exception):
            raise self._snapshot
        return self._snapshot


def test_load_position_empty_addr_is_flat_and_ok():
    # No wallet address -> cleanly flat (ok=True), no account lookup attempted.
    position, account_value, ok = main_mod._load_position(client=None, addr=None, coin="BTC")
    assert position is None
    assert account_value == Decimal(0)
    assert ok is True


def test_load_position_exchange_error_returns_not_ok(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "HyperliquidAccount", _FakeAccount(ExchangeError("boom")))
    position, account_value, ok = main_mod._load_position(
        client=object(), addr="0xReadOnlyAddress", coin="BTC"
    )
    assert position is None
    assert account_value == Decimal(0)
    assert ok is False  # a failed lookup must be distinguishable from a real flat
    assert "account lookup skipped" in capsys.readouterr().err


def test_load_position_schema_value_error_returns_not_ok(monkeypatch, capsys):
    # A structurally-unusable snapshot (e.g. a zero accountValue the schema rejects at
    # construction) raises ValueError, not ExchangeError. It must still be reported as a
    # clean failed lookup (ok=False -> exit 1), not escape to main's last-resort handler
    # and surface as exit 2 "unexpected error".
    monkeypatch.setattr(
        main_mod,
        "HyperliquidAccount",
        _FakeAccount(ValueError("AccountSnapshot.account_value must be > 0, got 0")),
    )
    position, account_value, ok = main_mod._load_position(
        client=object(), addr="0xReadOnlyAddress", coin="BTC"
    )
    assert position is None
    assert account_value == Decimal(0)
    assert ok is False
    assert "account lookup skipped" in capsys.readouterr().err


class _FakeInfo:
    """Minimal SDK ``Info`` stand-in: ``user_state`` returns a canned payload."""

    def __init__(self, state):
        self._state = state

    def user_state(self, _addr):
        return self._state


class _FakeClient:
    def __init__(self, state):
        self.info = _FakeInfo(state)


def test_get_account_snapshot_zero_value_raises_bare_value_error():
    # End-to-end guarantee behind test_load_position_schema_value_error_*: a zero
    # accountValue must propagate from AccountSnapshot.__post_init__ through the real
    # HyperliquidAccount.get_account_snapshot as a *bare* ValueError — NOT re-wrapped
    # as the exchange layer's MalformedResponseError (which is an ExchangeError, not a
    # ValueError). _load_position's `except (ExchangeError, ValueError)` relies on this:
    # if the chain ever re-wrapped it as some non-caught type the zero-balance path
    # would regress to exit 2. The mock-based test above only exercises the handler.
    state = {
        "marginSummary": {"accountValue": "0", "totalMarginUsed": "0"},
        "withdrawable": "0",
        "assetPositions": [],
    }
    account = HyperliquidAccount(_FakeClient(state))
    with pytest.raises(ValueError) as exc_info:
        account.get_account_snapshot("0xReadOnlyAddress")
    assert not isinstance(exc_info.value, MalformedResponseError)
    assert "account_value must be > 0" in str(exc_info.value)


def test_load_position_success_returns_position_and_value(monkeypatch):
    pos = PerpPosition(
        coin="BTC",
        size=Decimal("0.1"),
        entry_price=Decimal("60000"),
        unrealized_pnl=Decimal("50"),
        position_value=Decimal("6000"),
    )
    snapshot = AccountSnapshot(
        account_value=Decimal("10000"),
        withdrawable=Decimal("4000"),
        total_margin_used=Decimal("6000"),
        positions=(pos,),
    )
    monkeypatch.setattr(main_mod, "HyperliquidAccount", _FakeAccount(snapshot))

    position, account_value, ok = main_mod._load_position(
        client=object(), addr="0xReadOnlyAddress", coin="BTC"
    )
    assert position is pos
    assert account_value == Decimal("10000")
    assert ok is True


def test_load_position_success_flat_when_coin_absent(monkeypatch):
    snapshot = AccountSnapshot(
        account_value=Decimal("10000"),
        withdrawable=Decimal("10000"),
        total_margin_used=Decimal("0"),
        positions=(),
    )
    monkeypatch.setattr(main_mod, "HyperliquidAccount", _FakeAccount(snapshot))

    position, account_value, ok = main_mod._load_position(
        client=object(), addr="0xReadOnlyAddress", coin="BTC"
    )
    assert position is None  # position_for("BTC") finds nothing -> flat
    assert account_value == Decimal("10000")  # but account value is still reported
    assert ok is True  # genuine flat, not a failed lookup


# --------------------------------------------------------------------------
# run_engine — error-path behaviour (mocked engine; no network / LLM)
# --------------------------------------------------------------------------


# A well-formed Phase 2 structured target the stubbed engine returns by default;
# the real parse + RiskGate seams then run un-stubbed, exactly as in production.
_VALID_DECISION_TEXT = """After weighing the debate, here is the final decision.

```json
{
  "decision_mode": "set_target",
  "target_side": "long",
  "requested_target_margin_pct": 35,
  "confidence": 0.78,
  "rationale": "Trend and funding support a long.",
  "key_risks": ["Funding is rising"]
}
```
"""


def _stub_engine(
    monkeypatch,
    *,
    position_ok=True,
    account_value=Decimal("10000"),
    final_state=None,
    audit_error=None,
):
    """Stub out run_engine's heavy collaborators; return capture hooks.

    ``account_value`` defaults to a funded account (a configured wallet with net
    value) so the happy path reaches logging; pass ``Decimal(0)`` to exercise the
    no-equity pre-engine abort (no funded wallet configured).
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class _Ctx:
        candle_count = 200  # comfortably above any indicator warm-up need
        indicators = {"rsi_14": 55.0, "ema_20": 100.0}  # at least one live signal
        mark_price = Decimal("60000")  # current_position_state values at mark

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_Ctx(), object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda ctx: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "0xReadOnly")
    monkeypatch.setattr(
        main_mod,
        "_load_position",
        lambda *a, **k: (None, account_value, position_ok),
    )

    class _Graph:
        def propagate(self, *a, **k):
            return final_state or {"final_trade_decision": _VALID_DECISION_TEXT}, None

    monkeypatch.setattr(main_mod, "build_graph", lambda **k: _Graph())

    if audit_error is not None:

        def _raise(**_kw):
            raise audit_error

        monkeypatch.setattr(main_mod, "log_target_decision", _raise)


def test_run_engine_reports_bad_config_as_config_error(monkeypatch, capsys):
    # A malformed risk:/decision: block is an operator typo — exit 1 with the
    # offending key named (like the API-key / warm-up checks), before any
    # engine build or LLM spend, not exit-2 "unexpected error".
    calls = []
    _stub_engine(monkeypatch)
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({"risk": {"max_target_margin_pct": 150}}, "BTC")
    assert rc == 1
    assert calls == []  # aborted before the engine was built
    err = capsys.readouterr().err
    assert "invalid risk:/decision: config" in err
    assert "max_target_margin_pct" in err


def test_run_engine_aborts_when_position_lookup_fails(monkeypatch, capsys):
    # A failed wallet lookup must abort before the engine runs (no LLM spend) and
    # exit non-zero — never trade against guessed-flat state.
    calls = []
    _stub_engine(monkeypatch, position_ok=False)
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built
    assert "refusing to run the engine" in capsys.readouterr().err


def test_run_engine_aborts_on_insufficient_candles(monkeypatch, capsys):
    # Fewer candles than the indicators need -> abort before the engine runs, so we
    # never reason over a context where every indicator is None.
    _stub_engine(monkeypatch)

    class _ThinCtx:
        candle_count = 5  # below the 50 that ema_50 needs

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_ThinCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built
    assert "under-warmed" in capsys.readouterr().err


def test_run_context_only_warns_on_under_warmed_candles(monkeypatch, capsys):
    # --context-only renders rather than aborts, but an under-warmed context (every
    # indicator None, default "ranging" regime) must be flagged so it is not mistaken
    # for live signal — the symmetric warning to run_engine's hard abort.
    class _ThinCtx:
        candle_count = 5  # below the 50 that ema_50 needs

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_ThinCtx(), object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda ctx: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "")  # skip position block
    rc = main_mod.run_context_only({}, "BTC")
    assert rc == 0  # diagnostic tool renders, does not abort
    assert "under-warmed" in capsys.readouterr().err


def test_run_engine_prints_decision_then_reports_audit_failure(monkeypatch, capsys):
    # The Critical case: the decision is generated, then the audit write fails. The
    # decision must still reach stdout and the failure must be loud + non-zero.
    _stub_engine(monkeypatch, audit_error=OSError("disk full"))
    rc = main_mod.run_engine({}, "BTC")
    captured = capsys.readouterr()
    assert rc == 1
    assert '"decision_mode": "set_target"' in captured.out  # decision printed, not lost
    assert "audit log write failed" in captured.err


def test_run_engine_reports_audit_failure_on_unicode_error(monkeypatch, capsys):
    # A lone surrogate in the engine response can make the JSON write raise
    # UnicodeEncodeError; it must be caught like any other audit failure (decision
    # still printed, loud error, rc==1), not escape to the generic "fatal" handler.
    _stub_engine(monkeypatch, audit_error=UnicodeEncodeError("utf-8", "\ud800", 0, 1, "bad"))
    rc = main_mod.run_engine({}, "BTC")
    captured = capsys.readouterr()
    assert rc == 1
    assert '"decision_mode": "set_target"' in captured.out  # decision printed, not lost
    assert "audit log write failed" in captured.err


def test_run_engine_audit_failure_takes_precedence_over_contract_exit_3(monkeypatch, capsys):
    # Both alarms in one round: the model broke the contract AND the audit write
    # failed. Exit 1 wins (infrastructure failure is the louder alarm), so a
    # scheduler alerting specifically on exit 3 must also cover exit 1.
    _stub_engine(
        monkeypatch,
        final_state={"final_trade_decision": ""},
        audit_error=OSError("disk full"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert "audit log write failed" in capsys.readouterr().err


def test_run_engine_aborts_when_all_indicators_fail(monkeypatch, capsys):
    # Enough candles to clear the warm-up gate, but every known indicator is None:
    # stockstats failed on every column. This is indistinguishable from a warm-up
    # dict downstream (regime -> RANGING), so run_engine must abort before the LLM
    # call rather than trade on a fully-dead indicator set.
    _stub_engine(monkeypatch)

    class _DeadCtx:
        candle_count = 200  # past the warm-up gate -> not under-warm
        indicators = {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None}

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_DeadCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert "every technical indicator failed" in capsys.readouterr().err


def test_run_engine_aborts_when_only_atr_fails(monkeypatch, capsys):
    # Decision B: a *single* dead indicator (atr_14) slips past the all-dead guard, but
    # atr_14 is load-bearing — classify_regime would silently default to RANGING,
    # hiding a volatile market. run_engine must abort before the LLM call rather
    # than trade on a fabricated-calm regime.
    _stub_engine(monkeypatch)

    class _AtrDeadCtx:
        candle_count = 200  # past the warm-up gate
        indicators = {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0, "atr_14": None}

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_AtrDeadCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert "atr_14 failed to compute" in capsys.readouterr().err


def test_run_engine_aborts_on_malformed_propagate_shape(monkeypatch, capsys):
    # An engine version drift can change propagate's return contract. A non-(2+)-tuple
    # would otherwise blow up as an opaque unpack ValueError in the last-resort handler;
    # run_engine must fail clean (exit 1) and name the seam.
    _stub_engine(monkeypatch)

    class _BadShapeGraph:
        def propagate(self, *a, **k):
            return {"final_trade_decision": "**Rating**: Buy"}  # single dict, not a 2-tuple

    monkeypatch.setattr(main_mod, "build_graph", lambda **k: _BadShapeGraph())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert "unexpected shape" in capsys.readouterr().err


def test_run_engine_reports_engine_failure_when_propagate_raises(monkeypatch, capsys):
    # An engine-side exception (LLM rate-limit/timeout, a LangGraph crash) must be
    # classified as an engine-run failure: exit 1 with an actionable message, not fall
    # through to main's last-resort handler as an opaque exit-2 "unexpected error".
    _stub_engine(monkeypatch)

    class _RaisingGraph:
        def propagate(self, *a, **k):
            raise RuntimeError("provider rate-limited (429)")

    monkeypatch.setattr(main_mod, "build_graph", lambda **k: _RaisingGraph())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    err = capsys.readouterr().err
    assert "engine run failed" in err
    assert "429" in err  # the original cause is surfaced, not swallowed


def test_run_engine_aborts_on_non_dict_final_state(monkeypatch, capsys):
    # A crashed engine can return a non-dict final_state (e.g. None); to_perp_decision
    # indexes it as a dict, so run_engine must fail clean (exit 1) with a clear message
    # rather than crash with an opaque AttributeError under the last-resort handler.
    _stub_engine(monkeypatch)

    class _BadGraph:
        def propagate(self, *a, **k):
            return None, None

    monkeypatch.setattr(main_mod, "build_graph", lambda **k: _BadGraph())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert "non-dict final_state" in capsys.readouterr().err


def test_run_engine_success_writes_log_and_returns_zero(monkeypatch, capsys):
    written = {}
    _stub_engine(monkeypatch)
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 0
    assert written["coin"] == "BTC"
    assert written["parsed"].is_valid
    assert written["risk_result"].risk_action.value == "approved"
    # Decision-time sizing inputs must reach the audit record (values from _stub_engine).
    assert written["mark_price"] == Decimal("60000")
    assert written["account_equity"] == Decimal("10000")
    # prompt_hash must cover everything the adapter injected: the market
    # context AND the output-format contract (whose grid/min_confidence come
    # from the live config), so config changes always change the hash.
    assert "## Perpetual market context\nctx text" in written["prompt"]
    assert "## Required final decision output format" in written["prompt"]
    assert "decision log written to" in capsys.readouterr().err


def test_run_engine_fails_closed_on_empty_engine_output(monkeypatch, capsys):
    # An empty final_trade_decision carries no structured target: the Phase 2
    # contract fails closed to maintain_current (invalid_output) and the round IS
    # recorded — the raw (empty) response and the fail-closed verdict are exactly
    # what the validation counters need. No order can result, and the run exits 3
    # (distinct from success) so a naive scheduler can alert on model drift.
    written = {}
    _stub_engine(monkeypatch, final_state={"final_trade_decision": ""})
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 3
    assert "failing closed" in capsys.readouterr().err
    assert written["parsed"].is_valid is False
    assert written["parsed"].invalid_reason == "invalid_output"
    result = written["risk_result"]
    assert result.risk_action.value == "invalid_fail_closed"
    assert result.order_created is False
    # Sizing inputs must reach the record even on fail-closed rounds — they are
    # what makes a no-order record (target_* all None) reconstructible later.
    assert written["mark_price"] == Decimal("60000")
    assert written["account_equity"] == Decimal("10000")


def test_run_engine_aborts_before_llm_when_no_account_equity(monkeypatch, capsys):
    # No funded wallet -> account_value == 0 (a live snapshot of 0 is rejected at
    # construction and surfaces as a failed lookup instead). RiskGate would
    # fail-close every directional target against zero equity, so running the engine
    # is guaranteed-wasted LLM spend: run_engine aborts (exit 1) before building the
    # graph, and writes no decision log.
    built = []
    written = {}
    _stub_engine(monkeypatch, account_value=Decimal(0))
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: built.append("built") or object())
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert built == []  # aborted before the engine build / LLM spend
    assert written == {}  # nothing was logged
    assert "no usable account equity" in capsys.readouterr().err


def test_run_engine_warns_on_position_leverage_mismatch(monkeypatch, capsys):
    # A live position whose real leverage differs from risk.leverage (e.g. a
    # manually opened 5x position under the default leverage: 1) disables the
    # rebalance deadband inside the gate — the operator must see that condition
    # named on stderr, not discover it from an unexpected order. The fixture
    # pins current margin_pct == the requested 35%, the exact case a matching
    # leverage would have swallowed as within_deadband.
    written = {}
    _stub_engine(monkeypatch)
    position = PerpPosition(
        coin="BTC",
        size=Decimal("0.29"),  # ~17400 notional at mark 60000 (a real 5x position)
        entry_price=Decimal("59000"),
        unrealized_pnl=Decimal("10"),
        margin_used=Decimal("3500"),  # margin_pct = 35 == the stubbed request
        leverage=Decimal(5),
    )
    monkeypatch.setattr(
        main_mod, "_load_position", lambda *a, **k: (position, Decimal("10000"), True)
    )
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 0  # the mismatch is a warning, not an abort
    err = capsys.readouterr().err
    assert "position leverage 5 != configured risk.leverage 1" in err
    result = written["risk_result"]
    assert result.order_created is True  # deadband disabled — not within_deadband
    assert result.no_order_reason is None


def test_run_engine_fails_closed_on_unparseable_engine_output(monkeypatch, capsys):
    # A non-empty engine output with no structured JSON target is a malformed
    # response, not a deliberate maintain: the contract fails closed to
    # maintain_current, records the raw response verbatim, creates no order,
    # and exits 3 so the contract failure is visible to a scheduler.
    written = {}
    _stub_engine(monkeypatch, final_state={"final_trade_decision": "I cannot help with that."})
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 3
    assert "failing closed" in capsys.readouterr().err
    assert written["parsed"].is_valid is False
    assert written["parsed"].raw_response == "I cannot help with that."
    assert written["risk_result"].order_created is False


# --------------------------------------------------------------------------
# main — top-level exit codes
# --------------------------------------------------------------------------


def test_main_exchange_error_returns_exit_code_1(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "load_config", lambda path: {})

    def _boom(config, coin):
        raise ExchangeError("bad read")

    monkeypatch.setattr(main_mod, "run_engine", _boom)
    rc = main_mod.main(["--coin", "BTC"])
    assert rc == 1
    assert "error: bad read" in capsys.readouterr().err


def test_main_invalid_config_returns_exit_code_1(monkeypatch, capsys):
    # A ValueError out of load_config (typo'd top-level block, non-mapping YAML)
    # is an actionable operator error — a named exit 1, not the exit-2 bucket.
    def _bad(path):
        raise ValueError("unknown top-level config key(s): 'riks'.")

    monkeypatch.setattr(main_mod, "load_config", _bad)
    rc = main_mod.main(["--coin", "BTC"])
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err


def test_main_yaml_syntax_error_returns_exit_code_1(tmp_path, capsys):
    # A YAML syntax error (the most common config mistake of all) must land in
    # the same named exit-1 bucket as validation failures — yaml.YAMLError is
    # not a ValueError, so without the explicit catch it would escape main()
    # as a raw traceback.
    bad = tmp_path / "broken.yaml"
    bad.write_text("risk: {leverage: 1\n", encoding="utf-8")  # unclosed mapping
    rc = main_mod.main(["--config", str(bad), "--coin", "BTC"])
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err


def test_main_missing_config_path_returns_exit_code_1(tmp_path, capsys):
    # Same contract for a bad --config path: FileNotFoundError is an OSError,
    # caught and named, keeping load_config's helpful copy-the-example message.
    rc = main_mod.main(["--config", str(tmp_path / "nope.yaml"), "--coin", "BTC"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid config" in err
    assert "config not found" in err


def test_main_unexpected_error_returns_exit_code_2(monkeypatch, capsys, caplog):
    monkeypatch.setattr(main_mod, "load_config", lambda path: {})

    def _boom(config, coin):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main_mod, "run_engine", _boom)
    with caplog.at_level(logging.ERROR, logger="contrib.hyperliquid_perp.main"):
        rc = main_mod.main(["--coin", "BTC"])
    assert rc == 2
    assert "unexpected error" in capsys.readouterr().err
    # The full traceback is logged (not just the one-line stderr message) so an
    # unexpected failure is diagnosable from a configured handler.
    assert any(r.exc_info is not None for r in caplog.records)
    assert "kaboom" in caplog.text
