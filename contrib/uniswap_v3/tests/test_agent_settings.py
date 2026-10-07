"""The judge's settings: the defaults, the config's ``agent`` section, and what the snapshot leaves out."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.agent.settings import ANALYSTS, AgentSettings
from contrib.uniswap_v3.config import (
    ConfigError,
    config_from_snapshot,
    config_snapshot,
    parse_config,
)


def _document(**extra):
    return {
        "chain_id": 1,
        "quote_token": "USDC",
        "tokens": ["USDC", "WETH", "WBTC"],
        "pools": ["USDC/WETH-500", "WBTC/WETH-500"],
        "strategy": {
            "name": "fixed_weights",
            "params": {"weights": {"USDC": "0.5", "WETH": "0.3", "WBTC": "0.2"}, "band": "0.05"},
        },
        **extra,
    }


def test_the_defaults_are_the_judge_of_the_hyperliquid_paper_run():
    settings = AgentSettings()
    assert (settings.llm_provider, settings.deep_think_llm, settings.quick_think_llm) == (
        "openrouter",
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-chat",
    )
    assert settings.selected_analysts == ("market", "social", "news")
    assert settings.max_tokens == 8192
    assert settings.ask_within_seconds == 14_400
    # The least the first visit's verdict, 10 to 35 minutes after the boundary, clears.
    assert AgentSettings(ask_within_seconds=3600).ask_within_seconds == 3600


def test_a_list_of_analysts_is_kept_as_a_tuple():
    assert AgentSettings(selected_analysts=["market"]).selected_analysts == ("market",)
    assert AgentSettings(selected_analysts=list(ANALYSTS)).selected_analysts == ANALYSTS
    assert ANALYSTS == ("market", "social", "news")


def test_the_fundamentals_analyst_is_refused_as_stock_only():
    with pytest.raises(ValueError, match="reads a company's statements"):
        AgentSettings(selected_analysts=["market", "fundamentals"])


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"llm_provider": ""}, "llm_provider must be a non-empty string"),
        ({"deep_think_llm": 3}, "deep_think_llm must be a non-empty string"),
        ({"quick_think_llm": " "}, "quick_think_llm must be a non-empty string"),
        ({"selected_analysts": []}, "non-empty list of analyst names"),
        ({"selected_analysts": "market"}, "non-empty list of analyst names"),
        ({"selected_analysts": ["market", "weather"]}, "names 'weather'"),
        ({"selected_analysts": ["market", "market"]}, "names an analyst twice"),
        ({"max_tokens": 0}, "max_tokens must be a positive integer"),
        ({"max_tokens": True}, "max_tokens must be a positive integer"),
        ({"max_tokens": "8192"}, "max_tokens must be a positive integer"),
        ({"ask_within_seconds": 3599}, "ask_within_seconds must be an integer of at least 3600"),
    ],
)
def test_settings_that_cannot_be_used_are_refused(changes, match):
    with pytest.raises(ValueError, match=match):
        AgentSettings(**changes)


def test_the_config_reads_the_agent_section_and_defaults_it():
    assert parse_config(_document()).agent == AgentSettings()
    read = parse_config(
        _document(
            agent={
                "deep_think_llm": "vendor/model",
                "selected_analysts": ["market"],
                "max_tokens": 4096,
                "ask_within_seconds": 7200,
            }
        )
    )
    assert read.agent == AgentSettings(
        deep_think_llm="vendor/model",
        selected_analysts=("market",),
        max_tokens=4096,
        ask_within_seconds=7200,
    )


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({"model": "x"}, r"agent must be a mapping with keys from"),
        ("openrouter", r"agent must be a mapping with keys from"),
        ({"max_tokens": 0}, r"agent: max_tokens must be a positive integer"),
        ({"selected_analysts": ["weather"]}, r"agent: selected_analysts names 'weather'"),
    ],
)
def test_an_agent_section_that_cannot_be_used_is_refused(section, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(_document(agent=section))


def test_the_agent_section_is_not_part_of_the_snapshot():
    plain = parse_config(_document())
    judged = parse_config(_document(agent={"max_tokens": 1, "deep_think_llm": "other/model"}))
    assert config_snapshot(plain) == config_snapshot(judged)
    assert "agent" not in config_snapshot(judged)
    # A run carried on under another judge is the same run; the config read back has the defaults.
    assert config_from_snapshot(config_snapshot(judged)).agent == AgentSettings()
