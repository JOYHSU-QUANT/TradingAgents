"""CLI config precedence (#976, #977) and the CLI's completion cap (#183).

An explicit environment override for the debate/risk round counts, or the
checkpoint flag, must win over the interactive research-depth selection — the CLI
must not clobber an env-configured value back to a prompt/flag default. The
completion cap follows the same rule, with one addition: when nothing sets it
the CLI fills in ``DEFAULT_MAX_TOKENS`` rather than sending an uncapped request.
"""

from unittest import mock

import pytest

import cli.main as m
from tradingagents.default_config import DEFAULT_MAX_TOKENS
from tradingagents.llm_clients.factory import create_llm_client

from .conftest import provider_kwargs_for, repo_text

# Minimal selections dict shaped like get_user_selections()'s return value.
SELECTIONS = {
    "research_depth": 5,
    "shallow_thinker": "gpt-5.4-mini",
    "deep_thinker": "gpt-5.5",
    "backend_url": None,
    "llm_provider": "openai",
    "google_thinking_level": None,
    "openai_reasoning_effort": None,
    "anthropic_effort": None,
    "output_language": "English",
}


def test_research_depth_sets_both_rounds_without_env(monkeypatch):
    for var in ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "TRADINGAGENTS_MAX_RISK_ROUNDS"):
        monkeypatch.delenv(var, raising=False)
    cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 5
    assert cfg["max_risk_discuss_rounds"] == 5


def test_env_round_counts_win_over_selection(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    # DEFAULT_CONFIG already reflects the env (applied at import); emulate that.
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2, max_risk_discuss_rounds=4)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # env value, not research_depth=5
    assert cfg["max_risk_discuss_rounds"] == 4


def test_partial_env_only_overrides_that_count(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # env wins
    assert cfg["max_risk_discuss_rounds"] == 5  # falls through to research_depth


def test_checkpoint_none_preserves_env_default():
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=True)  # e.g. env-enabled
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["checkpoint_enabled"] is True  # not clobbered back to False


@pytest.mark.parametrize("flag", [True, False])
def test_checkpoint_flag_overrides_env(flag):
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=not flag)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=flag)
    assert cfg["checkpoint_enabled"] is flag


def test_cli_caps_completion_tokens_when_nothing_sets_one():
    """The CLI never sends an uncapped request (#183).

    DEFAULT_CONFIG leaves ``max_tokens`` None on purpose (library neutrality);
    the interactive CLI is the operator-driven non-perp path, and through a
    gateway an uncapped request lets the upstream substitute the model's full
    context and 400 every call (#177). Pinned on the wire, not on the dict:
    langchain renames the kwarg on the way out.
    """
    patched = dict(m.DEFAULT_CONFIG, max_tokens=None)  # no env cap in this process
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(dict(SELECTIONS, llm_provider="openrouter"), checkpoint=None)
    assert cfg["max_tokens"] == DEFAULT_MAX_TOKENS
    llm = create_llm_client(
        provider=cfg["llm_provider"],
        model=cfg["deep_think_llm"],
        api_key="placeholder",
        **provider_kwargs_for(cfg),
    ).get_llm()
    payload = llm._get_request_payload([("human", "hi")])
    assert payload["max_completion_tokens"] == DEFAULT_MAX_TOKENS


def test_env_completion_cap_wins_over_the_cli_default():
    # TRADINGAGENTS_MAX_TOKENS rides DEFAULT_CONFIG like every sibling env knob
    # (stored as the env string; the graph int()s it at consumption) and must
    # not be clobbered back to the CLI default.
    patched = dict(m.DEFAULT_CONFIG, max_tokens="4096")
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_tokens"] == "4096"


@pytest.mark.parametrize(
    "env_value,expected_source",
    [(None, "CLI default"), ("4096", "from TRADINGAGENTS_MAX_TOKENS")],
)
def test_cli_announces_the_cap_and_its_source(monkeypatch, env_value, expected_source):
    # A cap that binds is invisible downstream (the answer just comes back
    # truncated), so the number and where it came from must be on screen.
    if env_value is None:
        monkeypatch.delenv("TRADINGAGENTS_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("TRADINGAGENTS_MAX_TOKENS", env_value)
    cap = env_value or DEFAULT_MAX_TOKENS
    with mock.patch.object(m, "console") as console:
        m._announce_completion_cap({"max_tokens": cap})
    (line,), _ = console.print.call_args
    assert f"{cap} tokens" in line
    assert expected_source in line


def test_the_readme_and_env_example_quote_the_cli_cap_default():
    # Derived, not retyped — the drift lock the perp side keeps for SETUP.md.
    assert f"default of {DEFAULT_MAX_TOKENS}" in repo_text("README.md")
    env_example = repo_text(".env.example")
    assert f"#TRADINGAGENTS_MAX_TOKENS={DEFAULT_MAX_TOKENS}" in env_example
    assert f"CLI applies {DEFAULT_MAX_TOKENS}" in env_example
