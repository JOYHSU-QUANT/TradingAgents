"""Tests for the orchestration glue in ``main.py`` and ``engine_bridge.py``.

Covers the deterministic seams that do not need a live engine or network:
``_build_engine_config`` (config overlay onto the engine DEFAULT_CONFIG),
``_load_position`` (wallet/​error/​success branches) and the pre-LLM context
guards — all defined in ``engine_bridge`` and exercised as ``bridge_mod`` —
plus ``main.py``'s entry-point shells (``run_engine`` / ``run_context_only`` /
``main``), exercised as ``main_mod``. Patch targets follow the DEFINING module:
main reaches every bridge symbol through ``engine_bridge.X`` attribute access,
so bridge functions and their collaborators are ALWAYS patched on
``bridge_mod`` — even when the test drives a main entry point — while main's
own imports (``build_graph``, ``wallet_address``, …) are patched on
``main_mod``. The full ``run_engine`` path needs a key + network and is left
to integration testing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp import engine_bridge as bridge_mod, main as main_mod
from contrib.hyperliquid_perp.domains.perp import freshness as freshness_mod
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
    assert bridge_mod._indicator_names({}) == bridge_mod._DEFAULT_INDICATORS
    assert bridge_mod._indicator_names({"indicators": None}) == bridge_mod._DEFAULT_INDICATORS
    assert bridge_mod._indicator_names({"indicators": []}) == []
    assert bridge_mod._indicator_names({"indicators": ["rsi_14"]}) == ["rsi_14"]


def test_resolve_coin_warns_when_multiple_configured(capsys):
    # No --coin against a multi-coin config trades only the first; warn so the
    # ignored coins are not a silent selection (pass --coin to choose explicitly).
    coin = bridge_mod._resolve_coin(None, {"coins": ["btc", "eth", "sol"]})
    assert coin == "BTC"
    err = capsys.readouterr().err
    assert "3 coins configured" in err
    assert "ETH" in err and "SOL" in err


def test_resolve_coin_silent_for_single_coin(capsys):
    coin = bridge_mod._resolve_coin(None, {"coins": ["btc"]})
    assert coin == "BTC"
    assert capsys.readouterr().err == ""


def test_resolve_coin_takes_the_bare_cli_value_not_a_namespace():
    # Issue #53: the shared composition layer takes the ``--coin`` string, so
    # neither entry point hands it an argparse object (and engine_bridge no
    # longer imports argparse at all).
    assert bridge_mod._resolve_coin("eth", {"coins": ["btc"]}) == "ETH"
    assert not hasattr(bridge_mod, "argparse")


def test_build_engine_config_defaults():
    engine_config, selected = bridge_mod._build_engine_config({})

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
    engine_config, _ = bridge_mod._build_engine_config({"engine": {"structured_output": True}})
    assert engine_config["structured_output"] is True
    assert "engine.structured_output: true" in capsys.readouterr().err
    # An explicit false is preserved (same value as the perp default; this
    # pins the passthrough accepting False) — and stays signal-free.
    engine_config, _ = bridge_mod._build_engine_config({"engine": {"structured_output": False}})
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
    engine_config, selected = bridge_mod._build_engine_config(config)

    assert engine_config["llm_provider"] == "custom"
    assert engine_config["deep_think_llm"] == "deep-x"
    assert engine_config["quick_think_llm"] == "quick-y"
    assert engine_config["backend_url"] is None
    assert selected == ["market"]


def test_build_engine_config_does_not_mutate_default_config():
    from tradingagents.default_config import DEFAULT_CONFIG

    before = DEFAULT_CONFIG["llm_provider"]
    bridge_mod._build_engine_config({"engine": {"llm_provider": "openrouter"}})
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
    engine_config, selected = bridge_mod._build_engine_config(config)
    assert engine_config["structured_output"] is False  # blank -> perp default
    assert engine_config["llm_provider"] == "openrouter"
    assert engine_config["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert engine_config["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert selected == ["market", "social", "news"]


def test_build_engine_config_preserves_explicit_empty_analysts():
    # An explicit empty list is a deliberate "no analysts" choice — it must be
    # preserved, not silently replaced by the default suite (None still falls back).
    _engine_config, selected = bridge_mod._build_engine_config(
        {"engine": {"selected_analysts": []}}
    )
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
    with pytest.raises(bridge_mod.EngineImportError, match="UTF-8"):
        bridge_mod._build_engine_config({})


def test_build_engine_config_oserror_read_maps_to_named_error(monkeypatch):
    # DOTENV_READ_ERRORS is (OSError, UnicodeDecodeError); the OSError half
    # (e.g. an unreadable .env under the package init's unguarded load) must
    # take the same named-EngineImportError lane, not fall through to exit 2.
    # Guards a refactor narrowing this except to UnicodeDecodeError only.
    _block_tradingagents_import(monkeypatch, OSError("[Errno 13] Permission denied: '.env'"))
    with pytest.raises(bridge_mod.EngineImportError, match="Permission denied"):
        bridge_mod._build_engine_config({})


def test_build_engine_config_import_error_names_the_missing_module(monkeypatch):
    # Callers print only str(exc) on this named-exit path — the chained cause
    # never reaches a traceback-printing handler — so a broken transitive
    # dependency must ride in the message itself, or the operator gets a
    # misdirecting "is tradingagents installed?" with no module name.
    _block_tradingagents_import(monkeypatch, ModuleNotFoundError("No module named 'langchain'"))
    with pytest.raises(bridge_mod.EngineImportError, match="langchain"):
        bridge_mod._build_engine_config({})


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
    position, account_value, ok = bridge_mod._load_position(client=None, addr=None, coin="BTC")
    assert position is None
    assert account_value == Decimal(0)
    assert ok is True


def test_load_position_exchange_error_returns_not_ok(monkeypatch, capsys):
    monkeypatch.setattr(bridge_mod, "HyperliquidAccount", _FakeAccount(ExchangeError("boom")))
    position, account_value, ok = bridge_mod._load_position(
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
        bridge_mod,
        "HyperliquidAccount",
        _FakeAccount(ValueError("AccountSnapshot.account_value must be > 0, got 0")),
    )
    position, account_value, ok = bridge_mod._load_position(
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
    monkeypatch.setattr(bridge_mod, "HyperliquidAccount", _FakeAccount(snapshot))

    position, account_value, ok = bridge_mod._load_position(
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
    monkeypatch.setattr(bridge_mod, "HyperliquidAccount", _FakeAccount(snapshot))

    position, account_value, ok = bridge_mod._load_position(
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
        candle_interval = "4h"
        exchange_time = None  # fixture shape: the guard falls back to the wall clock

        @property
        def as_of(self):
            # A live feed, evaluated per call rather than pinned at import: the
            # staleness guard measures this against the wall clock, and these
            # tests are about what happens AFTER the context guards pass.
            return datetime.now(timezone.utc)

    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (_Ctx(), object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda ctx: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "0xReadOnly")
    monkeypatch.setattr(
        bridge_mod,
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
        raise bridge_mod.EngineImportError(
            "importing tradingagents failed, most likely while its package init "
            "read a repo .env file"
        )

    monkeypatch.setattr(bridge_mod, "_build_engine_config", _boom)
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
        # Realistic under-warm shape: compute_indicators seeds every configured
        # name via dict.fromkeys, so the keys exist with all-None values. That
        # shape also satisfies the dead-set/regime guards' preconditions, so
        # this pins the warm-up guard's precedence (guard order is the
        # operator-facing diagnosis: "wait for warm-up" vs "engine broken").
        indicators = {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None}

    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (_ThinCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built
    assert "under-warmed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("candle_count", "indicators", "expected"),
    [
        # Under-warmed: below the 50 candles that ema_50 needs. Keys present
        # with all-None values (compute_indicators' real under-warm output) —
        # the shape also satisfies the dead-set/regime guards, so a guard-order
        # swap would surface their messages instead and fail this case.
        (
            5,
            {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None},
            "under-warmed",
        ),
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
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (ctx, object()))
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
        candle_interval="4h",
        exchange_time=None,
        as_of=datetime.now(timezone.utc),  # a live feed clears the staleness guard
    )
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (ctx, object()))
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
        bridge_mod,
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

    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (_DeadCtx(), object()))
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

    monkeypatch.setattr(
        bridge_mod, "_build_context", lambda config, coin: (_AtrDeadCtx(), object())
    )
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

    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (_NoAtrCtx(), object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert "atr_14 is unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config", "candle_count", "ctx_indicators", "expected_msg"),
    [
        # A dead EMA is as regime-critical as a dead ATR: classify_regime
        # silently defaults to RANGING when ema_20 or ema_50 is None (hiding a
        # trending or volatile market), so a live atr_14 alone must not clear
        # the guard.
        (
            {},
            200,
            {"rsi_14": 55.0, "ema_20": None, "ema_50": 59000.0, "atr_14": 250.0},
            "ema_20 is unavailable",
        ),
        # Regression lock for the documented `indicators: []` semantics
        # (RUNBOOK): the empty list loads cleanly ("no indicators" is a
        # deliberate choice, warm-up threshold 0) but every engine cycle is
        # refused at the regime guard — all three regime names are absent. If
        # someone later special-cases empty lists in _context_refusal_error,
        # this fails. candle_count 5 sits below the default indicator set's
        # threshold (50): the regime message only appears if the empty list
        # really zeroes the warm-up threshold — a fallback to the default set
        # reports "under-warmed" instead.
        ({"indicators": []}, 5, {}, "atr_14, ema_20, ema_50 are unavailable"),
    ],
)
def test_run_engine_refuses_untradeable_regime_indicators(
    monkeypatch, capsys, config, candle_count, ctx_indicators, expected_msg
):
    _stub_engine(monkeypatch)
    ctx = SimpleNamespace(candle_count=candle_count, indicators=ctx_indicators)
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (ctx, object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine(config, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert expected_msg in capsys.readouterr().err


# --------------------------------------------------------------------------
# _context_refusal_error — market-data freshness (issue #37)
# --------------------------------------------------------------------------

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _ctx_closing_at(
    as_of,
    *,
    interval="4h",
    candle_count=200,
    exchange_time=None,
    host_skew=None,
    paired=True,
):
    """A context that clears the first three guards; only its age varies.

    ``exchange_time=None`` is the fixture/replay shape — no exchange clock, so
    the guard measures against ``now`` (the pre-#51 behaviour these tests pin
    as the fallback). Pass one to exercise the exchange-clock path.

    ``host_skew`` is how far this host's clock sat from the exchange's AT THE
    MOMENT the exchange clock was read — the only pairing the guard will
    measure skew from, precisely because ``now`` on the daemon path is a
    reading from before the fetch and would report elapsed time as drift.

    ``paired=False`` is the shape ``schema.py`` allows but ``_build_context``
    never produces: an exchange clock with no host reading taken beside it,
    so the guard has a real age and no skew to report.
    """
    host_at_read = (
        None if exchange_time is None or not paired else exchange_time + (host_skew or timedelta(0))
    )
    return SimpleNamespace(
        candle_count=candle_count,
        indicators={"rsi_14": 55.0, "ema_20": 60000.0, "ema_50": 59000.0, "atr_14": 250.0},
        candle_interval=interval,
        as_of=as_of,
        exchange_time=exchange_time,
        host_time_at_exchange_read=host_at_read,
    )


# The two measuring clocks the guard can use. ``_build_context`` always supplies
# the exchange's, so that is the production branch; ``None`` is the fixture /
# replay fallback, measured against ``now``. The bound, floor, ceiling and
# age-format tests run under BOTH: until issue #94 every one of them ran on
# the fallback alone, and the floor and ceiling had no test at all on the
# branch production takes.
_BOTH_CLOCKS = pytest.mark.parametrize(
    "exchange_time",
    [pytest.param(None, id="host-clock"), pytest.param(_NOW, id="exchange-clock")],
)
# ...and the phrase each branch prints for a stale age / a candle ahead of it.
_BEFORE = {None: "before now", _NOW: "before the exchange's clock"}
_AFTER = {None: "AFTER the current time", _NOW: "AFTER the exchange's clock"}


def test_context_refusal_flags_a_stalled_candle_feed():
    # The whole point of the guard: a feed that stopped advancing yields a
    # context whose indicators all compute and whose regime reads healthy, so
    # the three guards above pass it — it just describes 14h ago. Host-clock
    # fallback path (no exchange clock on the context).
    ctx = _ctx_closing_at(_NOW - timedelta(hours=14))
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    # Named numbers, not a bare "stale": an operator must be able to tell a
    # 14h-old feed from a 3-day-old one without reading the code.
    assert "2026-08-16T22:00:00Z" in msg
    assert "14h 0m 0s" in msg
    assert "12h 0m 0s freshness limit (3 x 4h)" in msg
    # Both causes named, neither asserted: a host clock running AHEAD lands
    # here (the exchange has no future candles to truncate, so the age really
    # does read large) and is indistinguishable from a feed that stopped —
    # blaming the exchange would send an operator down the wrong path half the
    # time. Without an exchange clock the message must say it cannot tell.
    assert "feed stopped advancing" in msg
    assert "host's clock is ahead" in msg
    assert "carries no exchange clock" in msg


def test_refusals_carry_the_error_type_the_validators_count():
    # Issue #50: the escalation counts consecutive api_failed cycles classed
    # ``stale_market_data``, so WHICH guard fired has to survive into the
    # durable record. Every freshness verdict — both measuring paths, both
    # directions, and the unmeasurable-interval branch — is that class,
    # because all of them keep refusing until a human fixes the feed or the
    # clock. The "cannot be reasoned over" guards stay ``server_error``: a
    # too-young listing or a broken indicator engine heals on its own, and
    # counting a warm-up hold as a stalled feed would fire the escalation on
    # exactly the case RUNBOOK §7 calls expected.
    stale = _NOW - timedelta(hours=14)
    ahead = _NOW + timedelta(hours=13)
    for label, ctx, now in (
        ("stale via the exchange clock", _ctx_closing_at(stale, exchange_time=_NOW), _NOW),
        ("stale via the host-clock fallback", _ctx_closing_at(stale), _NOW),
        (
            "candle ahead of the exchange clock",
            _ctx_closing_at(ahead, exchange_time=_NOW),
            _NOW + timedelta(hours=14),
        ),
        ("candle ahead of the host clock", _ctx_closing_at(ahead), _NOW),
        ("age unmeasurable", _ctx_closing_at(_NOW, interval="4H", exchange_time=_NOW), _NOW),
    ):
        refusal = bridge_mod._context_refusal(ctx, "BTC", {}, now=now)
        assert refusal is not None, label
        assert refusal.error_type == "stale_market_data", label

    thin = _ctx_closing_at(_NOW, candle_count=5, exchange_time=_NOW)
    thin.indicators = dict.fromkeys(thin.indicators)
    assert bridge_mod._context_refusal(thin, "BTC", {}, now=_NOW).error_type == "server_error"
    dead = _ctx_closing_at(_NOW, exchange_time=_NOW)
    dead.indicators = dict.fromkeys(dead.indicators)
    assert bridge_mod._context_refusal(dead, "BTC", {}, now=_NOW).error_type == "server_error"


def test_context_refusal_error_is_the_message_view_of_the_same_verdict():
    # The one-shot shells print a sentence and exit; only the daemon needs the
    # class. Pin that the two never disagree about WHETHER to refuse — a
    # wrapper that fell out of step would let --context-only pass a context the
    # daemon refuses, the exact drift the shared guard exists to prevent.
    for ctx in (
        _ctx_closing_at(_NOW - timedelta(hours=14), exchange_time=_NOW),
        _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=_NOW),
    ):
        typed = bridge_mod._context_refusal(ctx, "BTC", {}, now=_NOW)
        message = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
        assert message == (None if typed is None else typed.message)


@_BOTH_CLOCKS
def test_context_refusal_passes_a_live_candle_feed(exchange_time):
    # The healthy witness. get_candles drops the still-forming bar, so on a
    # healthy feed the newest CLOSED candle is under one interval old — and a
    # full interval, the case here, is what a single unpublished boundary
    # already looks like. Refusing that would refuse a cycle over ordinary
    # exchange jitter.
    ctx = _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=exchange_time)
    assert bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW) is None


@_BOTH_CLOCKS
def test_context_refusal_freshness_bound_is_exclusive(exchange_time):
    # Exactly at 3 x 4h is fresh, one second past it is not: pins the
    # comparison as a strict `>` on both measuring clocks. A `>=` would refuse
    # a feed that the boundary case here calls healthy.
    at_limit = _ctx_closing_at(_NOW - timedelta(hours=12), exchange_time=exchange_time)
    assert bridge_mod._context_refusal_error(at_limit, "BTC", {}, now=_NOW) is None
    past_limit = _ctx_closing_at(_NOW - timedelta(hours=12, seconds=1), exchange_time=exchange_time)
    assert "freshness limit" in bridge_mod._context_refusal_error(past_limit, "BTC", {}, now=_NOW)


@_BOTH_CLOCKS
def test_context_refusal_freshness_limit_tracks_the_candle_interval(exchange_time):
    # The bound is N x interval, not a fixed span: 5h is a healthy age for 4h
    # bars and a stalled feed for 1h bars. A hardcoded hour count would pass
    # one of these two and fail the other.
    age = _NOW - timedelta(hours=5)
    four_hourly = _ctx_closing_at(age, exchange_time=exchange_time)
    assert bridge_mod._context_refusal_error(four_hourly, "BTC", {}, now=_NOW) is None
    hourly = _ctx_closing_at(age, interval="1h", exchange_time=exchange_time)
    assert "freshness limit" in bridge_mod._context_refusal_error(hourly, "BTC", {}, now=_NOW)


@_BOTH_CLOCKS
def test_context_refusal_freshness_limit_is_capped_at_three_decision_cycles(exchange_time):
    # The candle interval is operator-configurable but the decision cycle is
    # fixed at 4h, so 3 x 1d would let 18 cycles trade through a three-day
    # outage. The cap binds instead — and the message says the cap is what bound
    # it, since "12h" alone would read as the ordinary 3 x 4h bound.
    daily = _ctx_closing_at(_NOW - timedelta(hours=13), interval="1d", exchange_time=exchange_time)
    msg = bridge_mod._context_refusal_error(daily, "BTC", {}, now=_NOW)
    assert msg is not None and "capped at 3 x the 4h decision cycle" in msg
    # Just inside the cap still passes: the cap is a bound, not a second guard.
    fresh = _ctx_closing_at(_NOW - timedelta(hours=11), interval="1d", exchange_time=exchange_time)
    assert bridge_mod._context_refusal_error(fresh, "BTC", {}, now=_NOW) is None


@_BOTH_CLOCKS
def test_context_refusal_freshness_limit_has_a_floor(exchange_time):
    # The other end: 3 x 1m would refuse a whole cycle over three minutes of
    # feed jitter, far tighter than the 4h decision cadence needs.
    minutely = _ctx_closing_at(
        _NOW - timedelta(minutes=20), interval="1m", exchange_time=exchange_time
    )
    assert bridge_mod._context_refusal_error(minutely, "BTC", {}, now=_NOW) is None
    past_floor = _ctx_closing_at(
        _NOW - timedelta(minutes=31), interval="1m", exchange_time=exchange_time
    )
    msg = bridge_mod._context_refusal_error(past_floor, "BTC", {}, now=_NOW)
    assert msg is not None and "raised to the 30m floor" in msg


def test_freshness_ceiling_tracks_the_decision_cycle():
    # Drift lock. The freshness module writes the ceiling out instead of
    # importing CYCLE_INTERVAL (that import drags the paper engine into the
    # keyless --context-only path), so this test is what keeps "3 x the 4h
    # decision cycle" — the phrase the refusal message prints at an operator —
    # true.
    from contrib.hyperliquid_perp.paper.scheduler import CYCLE_INTERVAL

    cycle_ms = int(CYCLE_INTERVAL.total_seconds() * 1000)
    assert (
        freshness_mod._MAX_CANDLE_AGE_INTERVALS * cycle_ms
        == freshness_mod._MAX_CANDLE_AGE_CEILING_MS
    )
    assert f"{int(CYCLE_INTERVAL.total_seconds()) // 3600}h" == freshness_mod._CYCLE_LABEL


def test_the_freshness_floor_label_is_derived_from_the_floor():
    # The ceiling's label above is derived and drift-locked; the floor's was
    # written out as "30m". Both are read by an operator judging whether a
    # refusal is reasonable, so both have to keep tracking their constant —
    # which is what _candle_age_limit's docstring claims for every branch.
    _, how = freshness_mod._candle_age_limit(60_000, "1m")  # 3 x 1m -> under the floor
    assert how == "3 x 1m raised to the 30m floor"
    assert f"{freshness_mod._MAX_CANDLE_AGE_FLOOR_MS // 60_000}m floor" in how
    # The label renders whole minutes by floor division, so it is only honest
    # while the constant IS whole minutes. Left unstated, a floor of 25m30s
    # would print "25m" and understate the bound actually being enforced —
    # the same "label drifted from its constant" defect one size smaller.
    assert freshness_mod._MAX_CANDLE_AGE_FLOOR_MS % 60_000 == 0


@_BOTH_CLOCKS
def test_refusal_age_carries_seconds_past_the_limit(exchange_time):
    # The whole reason _format_duration_ms grew a seconds field: at minute
    # resolution every age in the first minute past the limit renders AS the
    # limit, and the message reads "X is past the X limit". 30 seconds over is
    # the case that must not collide.
    ctx = _ctx_closing_at(_NOW - timedelta(hours=12, seconds=30), exchange_time=exchange_time)
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert f"12h 0m 30s {_BEFORE[exchange_time]}" in msg
    assert "12h 0m 0s freshness limit" in msg


@_BOTH_CLOCKS
def test_refusal_age_reads_in_days_once_it_is_long(exchange_time):
    # A feed down for days renders as days, not a three-figure hour count. The
    # limit is capped far below this band, so the two can never collide here.
    ctx = _ctx_closing_at(_NOW - timedelta(days=5, hours=3), exchange_time=exchange_time)
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None and f"5d 3h {_BEFORE[exchange_time]}" in msg


@_BOTH_CLOCKS
def test_refusal_age_stays_in_hours_for_an_overnight_outage(exchange_time):
    # The day form starts at two days, not one: the most common real outage
    # length reads better as hours. Pins the readability choice the constant
    # exists for — a threshold of one day renders this as "1d 6h".
    ctx = _ctx_closing_at(_NOW - timedelta(hours=30), exchange_time=exchange_time)
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None and f"30h 0m 0s {_BEFORE[exchange_time]}" in msg


@_BOTH_CLOCKS
def test_context_refusal_future_bound_is_exclusive(exchange_time):
    # The future side mirrors the stale side's strict comparison, so the
    # tolerance is symmetric: exactly at the bound passes, one second past it
    # refuses — on both measuring clocks.
    at_bound = _ctx_closing_at(_NOW + timedelta(hours=12), exchange_time=exchange_time)
    assert bridge_mod._context_refusal_error(at_bound, "BTC", {}, now=_NOW) is None
    past_bound = _ctx_closing_at(_NOW + timedelta(hours=12, seconds=1), exchange_time=exchange_time)
    msg = bridge_mod._context_refusal_error(past_bound, "BTC", {}, now=_NOW)
    assert msg is not None and _AFTER[exchange_time] in msg


def test_context_refusal_flags_a_clock_that_jumped():
    # A candle closing far after the caller's clock reading. Deliberately NOT
    # claimed to detect a clock merely set behind — get_candles takes its window
    # end from the same clock, so a clock BEHIND truncates the candles by the
    # same amount and the age reads ordinary (issue #51). What lands here is the
    # clock JUMPING between the two readings, or a ctx that never came from a
    # live fetch; either way the timestamps are incomparable.
    future = _ctx_closing_at(_NOW + timedelta(hours=13))
    msg = bridge_mod._context_refusal_error(future, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "jumped between the two readings" in msg
    assert "did not come from a live market fetch" in msg
    assert "13h 0m 0s AFTER" in msg
    assert "12h 0m 0s tolerance (3 x 4h)" in msg
    # No direction claimed: the daemon reads its clock BEFORE the fetch and the
    # one-shot callers AFTER it, so the same branch means a forward jump on one
    # path and a backward jump on the other. Naming either would be wrong half
    # the time — and this message reaches an operator.
    assert "backward" not in msg and "forward" not in msg


def _host_clock_offset_ctx(offset_hours, *, exchange_clock):
    """A context fetched by a host whose clock is ``offset_hours`` off.

    Models production faithfully: get_candles bounds its window by the HOST
    clock, so the newest closed candle it can see is never later than that
    bound — and, with the exchange at ``_NOW``, never later than the exchange
    either. Returns ``(ctx, host_now)``.
    """
    host = _NOW + timedelta(hours=offset_hours)
    newest_candle = min(_NOW, host)  # what `close_time <= end` leaves behind
    ctx = _ctx_closing_at(
        newest_candle,
        exchange_time=_NOW if exchange_clock else None,
        host_skew=timedelta(hours=offset_hours) if exchange_clock else None,
    )
    return ctx, host


def test_freshness_guard_without_an_exchange_clock_is_blind_to_a_clock_behind():
    # The fallback path's known asymmetry, pinned so it stays a documented gap
    # rather than an assumption: with NO exchange clock on the context the
    # guard has only the host clock, which also bounded the candle window.
    def verdict(offset_hours):
        ctx, host = _host_clock_offset_ctx(offset_hours, exchange_clock=False)
        return bridge_mod._context_refusal_error(ctx, "BTC", {}, now=host)

    # Behind: the candles are truncated by the same amount, so the age reads
    # ordinary and NOTHING fires — the run would trade on a day-old market.
    assert verdict(-24) is None
    # Ahead: the exchange has no future candles to truncate, so the age really
    # is large and the staleness branch catches it.
    assert "freshness limit" in verdict(+24)


def test_freshness_guard_catches_a_clock_that_runs_behind_via_the_exchange_clock():
    # Issue #51 closed: measured against the exchange's clock the day-old
    # window IS a day old, whatever the host clock says. Same fixture shape as
    # the blind test above, differing only in the exchange clock being present
    # — that difference is the whole fix.
    ctx, _host = _host_clock_offset_ctx(-24, exchange_clock=True)
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "24h 0m 0s before the exchange's clock" in msg
    assert "12h 0m 0s freshness limit (3 x 4h)" in msg
    # And the cause is no longer a coin-flip: the host clock is named, with
    # its direction and size, as what truncated the window.
    assert "24h 0m 0s behind the exchange's" in msg
    assert "fix time sync (NTP)" in msg
    assert "feed itself stopped advancing" not in msg


def test_freshness_guard_splits_the_blame_when_the_skew_is_only_part_of_the_age():
    # The honest middle band: a host 6h behind with 4h bars contributes 6h of a
    # 20h age but cannot account for all of it. Naming EITHER cause outright
    # here would send half the investigations down the wrong path, so the
    # message must name the offset's SIZE and point at both.
    ctx = _ctx_closing_at(
        _NOW - timedelta(hours=20), exchange_time=_NOW, host_skew=-timedelta(hours=6)
    )
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "accounts for 6h 0m 0s of this age but not all of it" in msg
    assert "check time sync (NTP) AND the exchange's candle feed" in msg
    # Neither single-cause sentence appears.
    assert "by itself puts the newest" not in msg
    assert "feed itself stopped advancing" not in msg


def test_freshness_guard_names_the_feed_when_the_host_clock_agrees():
    # The other half of telling the causes apart: a healthy host clock and a
    # 14h-old newest candle means the EXCHANGE published nothing newer.
    ctx = _ctx_closing_at(
        _NOW - timedelta(hours=14), exchange_time=_NOW, host_skew=timedelta(seconds=3)
    )
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "agrees with the exchange's" in msg
    assert "feed itself stopped advancing" in msg
    assert "fix time sync" not in msg


def test_freshness_guard_reports_the_skew_as_unknown_without_a_paired_reading(caplog):
    # The shape schema.py allows and _build_context never produces: an
    # exchange clock with no host reading taken beside it. The age is still
    # real (measured between exchange-side values), so the verdict is the
    # same; what the guard cannot do is narrow the cause, and it must say so
    # rather than pick one — and it must not warn about a skew it never
    # measured.
    ctx = _ctx_closing_at(_NOW - timedelta(hours=14), exchange_time=_NOW, paired=False)
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "14h 0m 0s before the exchange's clock" in msg
    assert "no paired host-clock reading, so the skew is unknown" in msg
    assert "cannot be narrowed further from here" in msg
    # Neither single-cause sentence, and no "agrees" either — nothing was measured.
    assert "feed itself stopped advancing" not in msg
    assert "fix time sync" not in msg
    assert "agrees with the exchange's" not in msg
    assert not [r for r in caplog.records if "Fix time sync" in r.getMessage()]
    # ...and a fresh unpaired context passes without a warning.
    fresh = _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=_NOW, paired=False)
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        assert bridge_mod._context_refusal_error(fresh, "BTC", {}, now=_NOW) is None
    assert not [r for r in caplog.records if "Fix time sync" in r.getMessage()]


def test_freshness_guard_sole_cause_bound_on_the_skew_is_exclusive():
    # The boundary between "the offset by itself puts the newest candle past
    # the limit" and "it accounts for part of this age": PR #91 made it a
    # strict `<` (an offset equal to the limit alone yields age == limit,
    # which the age check calls fresh), and every other freshness boundary in
    # this file has its exclusive case pinned — this one did not.
    age = _NOW - timedelta(hours=20)
    at_limit = _ctx_closing_at(age, exchange_time=_NOW, host_skew=-timedelta(hours=12))
    msg = bridge_mod._context_refusal_error(at_limit, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "accounts for 12h 0m 0s of this age but not all of it" in msg
    assert "by itself puts the newest" not in msg
    past_limit = _ctx_closing_at(age, exchange_time=_NOW, host_skew=-timedelta(hours=12, seconds=1))
    msg = bridge_mod._context_refusal_error(past_limit, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "by itself puts the newest" in msg
    assert "but not all of it" not in msg


def test_context_refusal_rejects_a_class_outside_the_error_type_registry():
    # Issue #94: the class is what the daemon writes to decision_attempts,
    # and the write boundary validates it — but only when a cycle is recorded.
    # A misspelt class would pass both one-shot entry points (they print the
    # sentence and exit) and surface as a ValueError out of the repository on
    # the daemon's first refused cycle. Bind it at construction instead.
    with pytest.raises(ValueError, match="ContextRefusal.error_type"):
        freshness_mod.ContextRefusal("sever_error", "typo")
    # Both words the guard family writes are members — the constructor above
    # is what enforces it, so this is the positive witness of the same check.
    from contrib.hyperliquid_perp.common.constants import ERROR_TYPES

    assert {freshness_mod.UNUSABLE_CONTEXT_ERROR, freshness_mod.STALE_CONTEXT_ERROR} <= ERROR_TYPES


def test_the_error_type_registry_has_one_definition():
    # The guard validates against common.constants.ERROR_TYPES (it must not
    # import the persistence package); the write boundary validates against
    # repository._vocab.ERROR_TYPES. If someone re-declares the set on the
    # vocabulary side "for clarity", the two check sites can drift apart
    # silently — identity, not equality, is what pins a single definition.
    from contrib.hyperliquid_perp.common.constants import ERROR_TYPES
    from contrib.hyperliquid_perp.persistence.repository import _vocab

    assert _vocab.ERROR_TYPES is ERROR_TYPES


def test_freshness_guard_measures_skew_between_the_paired_readings_not_now(caplog):
    # The pairing that makes the skew honest: ``now`` on the daemon path is the
    # scheduler's clock reading from BEFORE the fetch, so subtracting it would
    # report the fetch's elapsed time as drift — a slow-but-healthy network
    # would warn about NTP on a correctly-synced host, and then misattribute a
    # stalled feed to the clock. Here ``now`` is 10h off and the paired reading
    # says the clocks agree: no warning, and the verdict is unaffected.
    ctx = _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=_NOW)
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        verdict = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW + timedelta(hours=10))
    assert verdict is None
    assert not [r for r in caplog.records if "This host's clock" in r.getMessage()]
    # ...and a genuine 10h lead, recorded in the pair, does warn.
    ahead = _ctx_closing_at(
        _NOW - timedelta(hours=4), exchange_time=_NOW, host_skew=timedelta(hours=10)
    )
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        assert bridge_mod._context_refusal_error(ahead, "BTC", {}, now=_NOW) is None
    warned = [r.getMessage() for r in caplog.records if "This host's clock" in r.getMessage()]
    assert len(warned) == 1
    assert "10h 0m 0s ahead of the exchange's" in warned[0]
    assert "Fix time sync (NTP)" in warned[0]
    # It must NOT tell the operator the decision is unaffected: a host ahead
    # pulls the still-forming bar through get_candles' filter.
    assert "decisions stay correct" not in warned[0]
    assert "has not closed" in warned[0]


def test_freshness_guard_skew_warning_has_a_floor(caplog):
    # A few seconds between the two paired readings is measurement noise, not a
    # broken clock. Below the warn floor: quiet, and the message (were one
    # needed) says the clocks agree.
    just_under = timedelta(milliseconds=freshness_mod._CLOCK_SKEW_WARN_MS - 1)
    ctx = _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=_NOW, host_skew=-just_under)
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        assert bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW) is None
    assert not [r for r in caplog.records if "Fix time sync" in r.getMessage()]
    # Exactly at the floor fires (>=): pins the comparison direction.
    at_floor = timedelta(milliseconds=freshness_mod._CLOCK_SKEW_WARN_MS)
    ctx = _ctx_closing_at(_NOW - timedelta(hours=4), exchange_time=_NOW, host_skew=-at_floor)
    with caplog.at_level(logging.WARNING, logger=freshness_mod.__name__):
        assert bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW) is None
    assert [r for r in caplog.records if "1m 0s behind the exchange's" in r.getMessage()]


def test_freshness_guard_flags_a_candle_closing_past_the_exchanges_clock():
    # The future side. With 4h bars a host lead CANNOT reach it — the forming
    # bar sits at most one interval past the exchange while the tolerance is
    # 3 x interval — so what lands here is a context that did not come from a
    # live fetch. Only 1d bars, where the 12h ceiling clamps the tolerance
    # below one interval, can reach it from a host lead.
    ctx = _ctx_closing_at(_NOW + timedelta(hours=13), exchange_time=_NOW)
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert msg is not None
    assert "13h 0m 0s AFTER the exchange's clock" in msg
    assert "did not come from a live market fetch" in msg
    # Inside the tolerance (a boundary closing during the fetch) passes.
    ctx = _ctx_closing_at(_NOW + timedelta(minutes=4), interval="1m", exchange_time=_NOW)
    assert bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW) is None


def test_build_context_fails_closed_when_the_exchange_clock_is_unreadable(monkeypatch):
    # The fail-closed decision (2026-08-22), pinned end to end: an l2Book the
    # mapper cannot read a clock from must abort the build like the other four
    # reads — the daemon's provider maps MalformedResponseError onto its own
    # retry class, so the cycle fails closed rather than quietly reverting to
    # the host clock the guard cannot trust.
    class _Market:
        def __init__(self, _client):
            pass

        def get_market_snapshot(self, coin):
            return object()

        def get_candles(self, coin, interval, lookback):
            return []

        def get_funding_history(self, coin, window_days):  # pragma: no cover - never reached
            raise AssertionError("the clock read must abort the build before funding")

        def get_exchange_time(self, coin):
            raise MalformedResponseError("l2Book 'time' is unusable as epoch ms (None): ...")

    class _Client:
        network = "testnet"

        @classmethod
        def from_config(cls, config):
            return cls()

    monkeypatch.setattr(bridge_mod, "HyperliquidClient", _Client)
    monkeypatch.setattr(bridge_mod, "HyperliquidMarketData", _Market)
    with pytest.raises(MalformedResponseError, match="'time' is unusable"):
        bridge_mod._build_context({}, "BTC")


def test_build_context_reads_the_exchange_clock_and_hands_it_to_the_builder(monkeypatch):
    # The wiring pin (mutation-checked: dropping the get_exchange_time call or
    # the exchange_time= kwarg in _build_context fails this). The clock must be
    # the one read in THIS fetch, not a default.
    exchange_clock = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
    handed = {}
    stamps = {}

    class _Market:
        def __init__(self, _client):
            pass

        def get_market_snapshot(self, coin):
            return object()

        def get_candles(self, coin, interval, lookback):
            return []

        def get_funding_history(self, coin, window_days):
            stamps["funding_entered"] = datetime.now(timezone.utc)
            return []

        def get_exchange_time(self, coin):
            handed["coin"] = coin
            stamps["clock_returned"] = datetime.now(timezone.utc)
            return exchange_clock

    class _Client:
        network = "testnet"

        @classmethod
        def from_config(cls, config):
            return cls()

    def _builder(*args, **kwargs):
        handed["exchange_time"] = kwargs.get("exchange_time")
        handed["host_at_read"] = kwargs.get("host_time_at_exchange_read")
        return object()

    monkeypatch.setattr(bridge_mod, "HyperliquidClient", _Client)
    monkeypatch.setattr(bridge_mod, "HyperliquidMarketData", _Market)
    monkeypatch.setattr(bridge_mod, "build_market_context", _builder)
    bridge_mod._build_context({}, "BTC")
    assert handed["coin"] == "BTC"
    assert handed["exchange_time"] == exchange_clock
    # ...and the host reading is taken ADJACENT to that read — between the
    # clock call returning and the next REST call starting. Bracketing it that
    # way, rather than merely "some time during the build", is what makes this
    # discriminating: moving the capture down to the builder call would still
    # sit inside the build but would fold the funding read's latency into the
    # skew, which is the whole defect the pairing exists to avoid.
    assert stamps["clock_returned"] <= handed["host_at_read"] <= stamps["funding_entered"]


def test_build_context_hands_the_configured_volume_profile_window_to_the_builder(monkeypatch):
    # Wiring pin: dropping the config read or the volume_profile_window= kwarg
    # in _build_context fails this. Without it the feature would look complete
    # (module tested, renderer tested) while never reaching the builder — and
    # the only symptom would be a prompt section that never appears, which is
    # indistinguishable from the switch being off.
    handed = {}

    class _Market:
        def __init__(self, _client):
            pass

        def get_market_snapshot(self, coin):
            return object()

        def get_candles(self, coin, interval, lookback):
            return []

        def get_funding_history(self, coin, window_days):
            return []

        def get_exchange_time(self, coin):
            return datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)

    class _Client:
        network = "testnet"

        @classmethod
        def from_config(cls, config):
            return cls()

    def _builder(*args, **kwargs):
        handed["window"] = kwargs.get("volume_profile_window")
        return object()

    monkeypatch.setattr(bridge_mod, "HyperliquidClient", _Client)
    monkeypatch.setattr(bridge_mod, "HyperliquidMarketData", _Market)
    monkeypatch.setattr(bridge_mod, "build_market_context", _builder)

    bridge_mod._build_context({"market_data": {"volume_profile_window_candles": 30}}, "BTC")
    assert handed["window"] == 30
    # Absent key -> off, and a blank key (`volume_profile_window_candles:`) is
    # treated like absent, matching candle_lookback/funding window handling.
    bridge_mod._build_context({}, "BTC")
    assert handed["window"] == 0
    bridge_mod._build_context({"market_data": {"volume_profile_window_candles": None}}, "BTC")
    assert handed["window"] == 0


@_BOTH_CLOCKS
def test_context_refusal_tolerates_a_candle_closing_during_the_fetch(exchange_time):
    # The daemon reads its clock BEFORE the market fetch, so a boundary that
    # closes while the five REST calls run (each riding the full
    # network_timeout_s) lands a couple of minutes ahead of it. That is normal,
    # not a broken clock. Checked at the TIGHTEST interval, where the tolerance
    # is the 30m floor rather than 3 x interval — 1m bars would otherwise give
    # a 3-minute tolerance, inside the reach of a slow fetch. On the exchange
    # clock the same slack covers a boundary closing between the candle read
    # and the clock read.
    just_ahead = _ctx_closing_at(
        _NOW + timedelta(minutes=4), interval="1m", exchange_time=exchange_time
    )
    assert bridge_mod._context_refusal_error(just_ahead, "BTC", {}, now=_NOW) is None


def test_context_refusal_defaults_to_the_wall_clock():
    # The one-shot callers pass no clock. The default must be a real reading,
    # not a skipped check — this context is months old whenever the suite runs.
    stale = _ctx_closing_at(datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert "freshness limit" in bridge_mod._context_refusal_error(stale, "BTC", {})
    # ...and the same default does not manufacture a refusal for a live feed.
    live = _ctx_closing_at(datetime.now(timezone.utc))
    assert bridge_mod._context_refusal_error(live, "BTC", {}) is None


def test_context_refusal_fails_closed_on_an_unmeasurable_interval():
    # A mis-cased interval never survives _build_context (get_candles resolves
    # it first), but a context that reaches the guard with one cannot have its
    # age established — refuse rather than skip the check. Not run under both
    # clocks: the interval is resolved before the guard looks at either.
    ctx = _ctx_closing_at(_NOW, interval="4H")
    msg = bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)
    assert "freshness cannot be checked" in msg
    assert "4H" in msg  # the offending value is named


def test_context_refusal_reports_warmup_before_staleness():
    # Guard order is the operator-facing diagnosis. A feed that is both
    # under-warmed and old reports the warm-up cause: "this coin just listed /
    # the window is short" is actionable, "the data is old" follows from it.
    ctx = _ctx_closing_at(_NOW - timedelta(days=30), candle_count=5)
    ctx.indicators = {"rsi_14": None, "ema_20": None, "ema_50": None, "atr_14": None}
    assert "under-warmed" in bridge_mod._context_refusal_error(ctx, "BTC", {}, now=_NOW)


def test_run_engine_aborts_on_a_stale_context(monkeypatch, capsys):
    # End to end through the one-shot path: no engine is built, so a stalled
    # feed costs nothing and exits 1 with the cause on stderr.
    _stub_engine(monkeypatch)
    ctx = _ctx_closing_at(datetime(2026, 3, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (ctx, object()))
    calls = []
    monkeypatch.setattr(main_mod, "build_graph", lambda **k: calls.append("built") or object())
    rc = main_mod.run_engine({}, "BTC")
    assert rc == 1
    assert calls == []  # engine never built — no LLM spend
    assert "freshness limit" in capsys.readouterr().err


def test_run_context_only_warns_on_a_stale_context(monkeypatch, capsys):
    # The diagnostic loop renders it but must not let it read as live signal —
    # a stale context is the one degraded state whose rendering looks entirely
    # healthy (real prices, real indicators, a real regime).
    ctx = _ctx_closing_at(datetime(2026, 3, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin: (ctx, object()))
    monkeypatch.setattr(main_mod, "render_market_context", lambda c: "ctx text")
    monkeypatch.setattr(main_mod, "wallet_address", lambda config: "")
    rc = main_mod.run_context_only({}, "BTC")
    assert rc == 4
    captured = capsys.readouterr()
    assert "freshness limit" in captured.err
    assert "do not read it as live signal" in captured.err


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
        bridge_mod, "_load_position", lambda *a, **k: (position, Decimal("10000"), True)
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
        bridge_mod, "_load_position", lambda *a, **k: (position, Decimal("10000"), True)
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
    monkeypatch.setattr(bridge_mod, "load_config", lambda path: {})

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

    monkeypatch.setattr(bridge_mod, "load_config", _bad)
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
    monkeypatch.setattr(bridge_mod, "load_config", lambda path: {})

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

    monkeypatch.setattr(bridge_mod, "_build_context", _stop)
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
