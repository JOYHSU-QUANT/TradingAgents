"""Tests for the orchestration glue in ``main.py``.

Covers the two deterministic seams that do not need a live engine or network:
``_build_engine_config`` (config overlay onto the engine DEFAULT_CONFIG) and
``_load_position`` (wallet/​error/​success branches). The full ``run_engine``
path needs a key + network and is left to integration testing.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from types import SimpleNamespace

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


def test_resolve_coin_warns_when_multiple_configured(capsys):
    # No --coin against a multi-coin config trades only the first; warn so the
    # ignored coins are not a silent selection (pass --coin to choose explicitly).
    import argparse

    coin = main_mod._resolve_coin(argparse.Namespace(coin=None), {"coins": ["btc", "eth", "sol"]})
    assert coin == "BTC"
    err = capsys.readouterr().err
    assert "3 coins configured" in err
    assert "ETH" in err and "SOL" in err


def test_resolve_coin_silent_for_single_coin(capsys):
    import argparse

    coin = main_mod._resolve_coin(argparse.Namespace(coin=None), {"coins": ["btc"]})
    assert coin == "BTC"
    assert capsys.readouterr().err == ""


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
    # Perp runs force the free-text path by default (engine default is True):
    # the Phase 2 target JSON only survives in free text.
    assert engine_config["structured_output"] is False


def test_build_engine_config_structured_output_escape_hatch(capsys):
    # Arming the escape hatch must be loud (dual-channel warning): with the
    # prompt-injected contract it fail-closes every cycle as invalid_output.
    engine_config, _ = main_mod._build_engine_config({"engine": {"structured_output": True}})
    assert engine_config["structured_output"] is True
    assert "engine.structured_output: true" in capsys.readouterr().err
    # An explicit false is preserved (same value as the perp default; this
    # pins the passthrough accepting False) — and stays signal-free.
    engine_config, _ = main_mod._build_engine_config({"engine": {"structured_output": False}})
    assert engine_config["structured_output"] is False
    assert capsys.readouterr().err == ""


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
            "structured_output": None,
        }
    }
    engine_config, selected = main_mod._build_engine_config(config)
    assert engine_config["structured_output"] is False  # blank -> perp default
    assert engine_config["llm_provider"] == "openrouter"
    assert engine_config["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert engine_config["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert selected == ["market", "social", "news"]


def test_build_engine_config_preserves_explicit_empty_analysts():
    # An explicit empty list is a deliberate "no analysts" choice — it must be
    # preserved, not silently replaced by the default suite (None still falls back).
    _engine_config, selected = main_mod._build_engine_config({"engine": {"selected_analysts": []}})
    assert selected == []


def _block_tradingagents_import(monkeypatch, exc: BaseException) -> None:
    """Make the process's next ``tradingagents`` import raise ``exc``.

    Purges cached modules first so ``_build_engine_config``'s deferred import
    really re-executes, then poisons ``builtins.__import__`` for exactly the
    ``tradingagents`` package.
    """
    import builtins
    import sys

    real_import = builtins.__import__

    def _poisoned(name, *args, **kwargs):
        if name.split(".")[0] == "tradingagents":
            raise exc
        return real_import(name, *args, **kwargs)

    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "tradingagents"]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setattr(builtins, "__import__", _poisoned)


def test_build_engine_config_names_corrupt_dotenv_read(monkeypatch):
    # tradingagents/__init__ loads the repo .env files with no read guard, so
    # the process's first tradingagents import can raise UnicodeDecodeError
    # (not ImportError) on a corrupt file — the guard must turn it into the
    # named EngineImportError instead of an exit-2 traceback.
    _block_tradingagents_import(
        monkeypatch, UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
    )
    with pytest.raises(main_mod.EngineImportError, match="UTF-8"):
        main_mod._build_engine_config({})


def test_build_engine_config_oserror_read_maps_to_named_error(monkeypatch):
    # DOTENV_READ_ERRORS is (OSError, UnicodeDecodeError); the OSError half
    # (e.g. an unreadable .env under the package init's unguarded load) must
    # take the same named-EngineImportError lane, not fall through to exit 2.
    # Guards a refactor narrowing this except to UnicodeDecodeError only.
    _block_tradingagents_import(monkeypatch, OSError("[Errno 13] Permission denied: '.env'"))
    with pytest.raises(main_mod.EngineImportError, match="Permission denied"):
        main_mod._build_engine_config({})


def test_build_engine_config_import_error_names_the_missing_module(monkeypatch):
    # Callers print only str(exc) on this named-exit path — the chained cause
    # never reaches a traceback-printing handler — so a broken transitive
    # dependency must ride in the message itself, or the operator gets a
    # misdirecting "is tradingagents installed?" with no module name.
    _block_tradingagents_import(monkeypatch, ModuleNotFoundError("No module named 'langchain'"))
    with pytest.raises(main_mod.EngineImportError, match="langchain"):
        main_mod._build_engine_config({})


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
        # Live signals including the regime-critical trio (atr_14/ema_20/ema_50):
        # run_engine refuses a context where any of them is unusable (else the
        # regime silently defaults to RANGING), so a normal healthy context
        # carries all three.
        indicators = {"rsi_14": 55.0, "ema_20": 100.0, "ema_50": 95.0, "atr_14": 250.0}
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
    assert "invalid risk:/decision:/paper_trading: config" in err
    assert "max_target_margin_pct" in err


def test_run_engine_reports_bad_paper_trading_block_as_config_error(monkeypatch, capsys):
    # The paper_trading: gate itself, end to end: an actually-invalid block (not
    # just a bad risk:) must abort pre-engine with exit 1 and name the bad key —
    # if the PaperTradingConfig.from_dict call were dropped or reordered after
    # build_graph, this test fails.
    calls = []
    _stub_engine(monkeypatch)
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({"paper_trading": {"execution": {"taker_fee_rate": -1}}}, "BTC")
    assert rc == 1
    assert calls == []  # aborted before the engine was built
    err = capsys.readouterr().err
    assert "invalid risk:/decision:/paper_trading: config" in err
    assert "taker_fee_rate" in err


def test_run_engine_missing_key_message_embeds_dotenv_diagnosis(monkeypatch):
    # The abort text must carry config.dotenv_diagnosis's verdict for the actual
    # variable — dropping the interpolation (or diagnosing the wrong var) is
    # invisible to every substring the message carried before the .env work.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(main_mod, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    with pytest.raises(SystemExit, match=r"DIAG\[OPENROUTER_API_KEY\]"):
        main_mod.run_engine({}, "BTC")


def test_run_engine_reports_engine_import_failure_as_named_error(monkeypatch, capsys):
    # _build_engine_config's EngineImportError (see there for the causes) is
    # operator-fixable — exit 1 with the message, not main's exit-2
    # "unexpected error" bucket.
    calls = []
    _stub_engine(monkeypatch)
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())

    def _boom(config):
        raise main_mod.EngineImportError(
            "importing tradingagents failed, most likely while its package init "
            "read a repo .env file"
        )

    monkeypatch.setattr(main_mod, "_build_engine_config", _boom)
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built
    err = capsys.readouterr().err
    assert "error: importing tradingagents failed" in err


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


@pytest.mark.parametrize(
    ("candle_count", "indicators", "expected"),
    [
        # Under-warmed: below the 50 candles that ema_50 needs.
        (5, {}, "under-warmed"),
        # Fully-dead known-indicator set past the warm-up gate.
        (
            200,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "every technical indicator failed",
        ),
        # Only the load-bearing atr_14 dead past the warm-up gate.
        (
            200,
            {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0, "atr_14": None},
            "atr_14 is unavailable",
        ),
        # A dead EMA is just as regime-critical as a dead ATR: classify_regime
        # silently defaults to RANGING when any of the trio is None.
        (
            200,
            {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": None, "atr_14": 250.0},
            "ema_50 is unavailable",
        ),
    ],
)
def test_run_context_only_warns_and_exits_4_on_degraded_context(
    monkeypatch, capsys, candle_count, indicators, expected
):
    # --context-only renders rather than aborts, but shares run_engine's *full*
    # refusal guard (warm-up, fully-dead set, dead/missing regime indicators):
    # a context the engine would refuse must not render as a clean-looking live
    # signal — the diagnostic loop is exactly where an operator investigating a
    # RUNBOOK refusal will look. The degraded verdict also exits 4 (the repo's
    # probe convention) so a preflight can gate on the code, not stderr text.
    ctx = SimpleNamespace(candle_count=candle_count, indicators=indicators)
    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (ctx, object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda c: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "")  # skip position block
    rc = main_mod.run_context_only({}, "BTC")
    assert rc == 4  # rendered for diagnosis, but automation must see "degraded"
    captured = capsys.readouterr()
    assert "PerpMarketContext - BTC" in captured.out  # render not truncated by the verdict
    assert expected in captured.err
    assert "do not read it as live signal" in captured.err


def test_run_context_only_exits_0_on_healthy_context(monkeypatch, capsys):
    # The healthy-path witness for the 0/4 probe contract: a context passing
    # every refusal guard renders with no degraded warning and exits 0.
    ctx = SimpleNamespace(
        candle_count=200,
        indicators={"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0, "atr_14": 250.0},
    )
    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (ctx, object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda c: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "")  # skip position block
    rc = main_mod.run_context_only({}, "BTC")
    assert rc == 0
    assert "do not read it as live signal" not in capsys.readouterr().err


def test_run_context_only_rejects_bad_risk_decision_config(monkeypatch, capsys):
    # --context-only runs the same risk:/decision: validation as the paid run:
    # a broken config fails the free smoke run (named exit 1, before any network
    # fetch) instead of surfacing only on the next paid cycle.
    fetched = []
    monkeypatch.setattr(
        main_mod,
        "_build_context",
        lambda config, coin: fetched.append("fetched") or (object(), object()),
    )
    rc = main_mod.run_context_only({"risk": {"max_target_margin_pct": 150}}, "BTC")
    assert rc == 1
    assert fetched == []  # aborted before any network fetch
    assert "invalid risk:/decision:/paper_trading: config" in capsys.readouterr().err


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
    assert "atr_14 is unavailable" in capsys.readouterr().err


def test_run_engine_aborts_when_atr_not_configured(monkeypatch, capsys):
    # Decision B: requiring atr_14 for the regime is not conditional on it being in
    # the configured indicator set. A user who drops atr_14 from `indicators:` gets
    # a context with no atr_14 key at all — run_engine must still refuse rather than
    # trade on a silently-RANGING regime.
    _stub_engine(monkeypatch)

    class _NoAtrCtx:
        candle_count = 200
        indicators = {"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0}  # no atr_14 key

    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (_NoAtrCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert "atr_14 is unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config", "ctx_indicators", "expected_msg"),
    [
        # A dead EMA is as regime-critical as a dead ATR: classify_regime
        # silently defaults to RANGING when ema_20 or ema_50 is None (hiding a
        # trending or volatile market), so a live atr_14 alone must not clear
        # the guard.
        (
            {},
            {"rsi_14": 55.0, "ema_20": None, "ema_50": 59000.0, "atr_14": 250.0},
            "ema_20 is unavailable",
        ),
        # Regression lock for the documented `indicators: []` semantics
        # (RUNBOOK): the empty list loads cleanly ("no indicators" is a
        # deliberate choice, warm-up threshold 0) but every engine cycle is
        # refused at the regime guard — all three regime names are absent. If
        # someone later special-cases empty lists in _context_refusal_error,
        # this fails.
        ({"indicators": []}, {}, "atr_14, ema_20, ema_50 are unavailable"),
    ],
)
def test_run_engine_refuses_untradeable_regime_indicators(
    monkeypatch, capsys, config, ctx_indicators, expected_msg
):
    _stub_engine(monkeypatch)
    ctx = SimpleNamespace(candle_count=200, indicators=ctx_indicators)
    monkeypatch.setattr(main_mod, "_build_context", lambda config, coin: (ctx, object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine(config, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert expected_msg in capsys.readouterr().err


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


def test_run_engine_healthy_risk_rejection_exits_zero(monkeypatch, capsys):
    # A contract-valid decision the gate risk-REJECTS (low confidence) is a
    # healthy outcome: the round completes, the audit record is written, and the
    # run exits 0 — non-zero codes are reserved for contract failures (3) and
    # config/env/audit errors (1, 2).
    written = {}
    low_conf = _VALID_DECISION_TEXT.replace('"confidence": 0.78', '"confidence": 0.1')
    _stub_engine(monkeypatch, final_state={"final_trade_decision": low_conf})
    monkeypatch.setattr(
        main_mod,
        "log_target_decision",
        lambda **k: written.update(k) or ({}, "/tmp/perp_decisions/BTC.json"),
    )
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 0
    assert written["parsed"].is_valid  # the contract was honoured
    result = written["risk_result"]
    assert result.risk_action.value == "rejected"
    assert result.risk_reason == "low_confidence"
    assert result.order_created is False
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
    # risk-reject (no_account_equity) every directional target against zero equity,
    # so running the engine is guaranteed-wasted LLM spend: run_engine aborts
    # (exit 1) before building the graph, and writes no decision log.
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


def test_run_engine_warns_on_unusable_position_margin(monkeypatch, capsys):
    # A sized position with no usable margin_used (degraded account read) means
    # the gate cannot evaluate the rebalance deadband, so the same-side rebalance
    # skips it (the zero-delta check and the resize confidence bar still
    # apply) — the operator must see that condition named on
    # stderr, same visibility contract as the leverage-mismatch warning above.
    # leverage stays None (unreported) so only the margin warning fires.
    written = {}
    _stub_engine(monkeypatch)
    position = PerpPosition(
        coin="BTC",
        size=Decimal("0.058"),  # ~3480 notional at mark 60000 (~1x sizing)
        entry_price=Decimal("59000"),
        unrealized_pnl=Decimal("10"),
        margin_used=None,  # exchange read carried no usable committed margin
        leverage=None,
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
    assert rc == 0  # the degraded read is a warning, not an abort
    err = capsys.readouterr().err
    assert "position margin_used is unusable" in err
    assert "position leverage" not in err  # unreported leverage stays un-warned
    result = written["risk_result"]
    assert result.order_created is True  # deadband skipped — not within_deadband
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


def test_main_bad_phase1_config_value_returns_exit_code_1(tmp_path, capsys):
    # End-to-end pin for the load-time value guards: a typo'd network value is a
    # ValueError from load_config, so it rides the CONFIG_LOAD_ERRORS lane to a
    # named exit 1 — not an exit-2 traceback from deep inside the SDK client.
    bad = tmp_path / "value.yaml"
    bad.write_text("network: mainet\ncoins: [BTC]\n", encoding="utf-8")
    rc = main_mod.main(["--config", str(bad), "--coin", "BTC"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid config" in err
    assert "'network' must be" in err


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


def test_main_loads_dotenv_before_key_check(tmp_path, monkeypatch):
    # A key kept only in the repo-root .env must satisfy run_engine's startup
    # check: the engine package that would load .env itself is imported lazily,
    # only after that check, so main() has to perform the load first. The
    # suite-wide autouse fixture stubs the loader out; re-bind the real one.
    from contrib.hyperliquid_perp import config as config_mod

    monkeypatch.setattr(main_mod, "load_dotenv_files", config_mod.load_dotenv_files)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _stop(config, coin):
        raise ExchangeError("stop before any network call")

    monkeypatch.setattr(main_mod, "_build_context", _stop)
    rc = main_mod.main(["--coin", "BTC"])

    # Reaching the patched _build_context proves the key check passed on the
    # .env value; without the load main() would exit on the missing-key path.
    assert rc == 1
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-dotenv"


def test_main_loads_dotenv_unconditionally_first(monkeypatch):
    # The companion of test_cli's every-invocation pin: main() performs the
    # load as its very first act, before argv parsing — a regression moving it
    # into run_engine (still "before the key check") would silently stop
    # loading .env for --context-only and argv-error runs.
    calls: list[bool] = []
    monkeypatch.setattr(main_mod, "load_dotenv_files", lambda: calls.append(True))
    with pytest.raises(SystemExit):
        main_mod.main(["--no-such-flag"])
    assert calls == [True]
