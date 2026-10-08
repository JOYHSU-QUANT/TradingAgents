"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import pytest

import tradingagents.default_config as default_config_module


def test_no_env_uses_built_in_defaults(reload_default_config):
    dc = reload_default_config()
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gpt-5.6"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gpt-5.6-luna"
    assert dc.DEFAULT_CONFIG["backend_url"] is None
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is False


def test_string_overrides(reload_default_config):
    dc = reload_default_config(
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "google"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gemini-3-pro-preview"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gemini-3-flash-preview"
    assert dc.DEFAULT_CONFIG["backend_url"] == "https://example.invalid/v1"
    assert dc.DEFAULT_CONFIG["output_language"] == "Chinese"


def test_int_coercion(reload_default_config):
    dc = reload_default_config(
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert isinstance(dc.DEFAULT_CONFIG["max_debate_rounds"], int)
    assert dc.DEFAULT_CONFIG["max_risk_discuss_rounds"] == 2
    assert isinstance(dc.DEFAULT_CONFIG["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(reload_default_config, raw, expected):
    dc = reload_default_config(TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is expected


def test_reasoning_thinking_overrides(reload_default_config):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    dc = reload_default_config(
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] == "high"
    assert dc.DEFAULT_CONFIG["google_thinking_level"] == "minimal"
    assert dc.DEFAULT_CONFIG["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(reload_default_config):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    dc = reload_default_config()
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] is None
    assert dc.DEFAULT_CONFIG["google_thinking_level"] is None
    assert dc.DEFAULT_CONFIG["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(reload_default_config):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    dc = reload_default_config(
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1


def test_invalid_int_raises(reload_default_config):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        reload_default_config(TRADINGAGENTS_MAX_DEBATE_ROUNDS="not-a-number")


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(reload_default_config, bad):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    with pytest.raises(ValueError, match="TRADINGAGENTS_CHECKPOINT_ENABLED"):
        reload_default_config(TRADINGAGENTS_CHECKPOINT_ENABLED=bad)


def test_unknown_env_var_is_ignored(reload_default_config):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    dc = reload_default_config(
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in dc.DEFAULT_CONFIG


# --- the reload must not leak past its test (#346) ----------------------------


def test_reload_fixture_restore_undoes_the_overlay(reload_default_config):
    """``restore()`` (what teardown runs) puts ``DEFAULT_CONFIG`` back as it was (#346)."""
    before = dict(default_config_module.DEFAULT_CONFIG)
    reload_default_config(TRADINGAGENTS_MAX_TOKENS="8192")

    reload_default_config.restore()
    assert before == default_config_module.DEFAULT_CONFIG


def test_reload_fixture_restore_ignores_a_sibling_monkeypatch(
    monkeypatch, reload_default_config
):
    """An env var set by a ``monkeypatch`` that outlives this fixture is not baked in."""
    before = dict(default_config_module.DEFAULT_CONFIG)
    monkeypatch.setenv("TRADINGAGENTS_MAX_TOKENS", "8192")
    reload_default_config()

    reload_default_config.restore()
    assert before == default_config_module.DEFAULT_CONFIG
