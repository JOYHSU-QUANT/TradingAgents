"""Tests for per-network agent-key loading (phase3-spec §6).

The load must pick the env var matching ``live.network`` — with both keys
set simultaneously (the intended ``.env`` state) each network still reads only
its own — and blank/whitespace values must read as missing so §6 rule 6 fires.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.live.secrets import (
    AGENT_KEY_ENV_VARS,
    agent_key_env_var,
    load_agent_key,
)

_TESTNET_KEY = "0x" + "11" * 32
_MAINNET_KEY = "0x" + "22" * 32


def test_env_var_names_are_split_per_network():
    assert agent_key_env_var("testnet") == "HYPERLIQUID_AGENT_KEY_TESTNET"
    assert agent_key_env_var("mainnet") == "HYPERLIQUID_AGENT_KEY_MAINNET"


def test_unknown_network_raises():
    with pytest.raises(ValueError, match="network must be one of"):
        agent_key_env_var("prod")


def test_both_keys_set_each_network_reads_its_own(monkeypatch):
    # §6 v3: both keys coexist in .env; switching networks can never pick up
    # the other network's key.
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_TESTNET", _TESTNET_KEY)
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", _MAINNET_KEY)
    assert load_agent_key("testnet") == _TESTNET_KEY
    assert load_agent_key("mainnet") == _MAINNET_KEY


def test_missing_var_is_none(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_AGENT_KEY_TESTNET", raising=False)
    assert load_agent_key("testnet") is None


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_var_is_none(monkeypatch, value):
    # A blank assignment in .env must fire §6 rule 6 (forced allow_real_orders
    # off), not reach key derivation as "".
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_TESTNET", value)
    assert load_agent_key("testnet") is None


def test_value_is_stripped(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", f"  {_MAINNET_KEY}  ")
    assert load_agent_key("mainnet") == _MAINNET_KEY


def test_env_var_map_covers_exactly_the_legal_networks():
    assert set(AGENT_KEY_ENV_VARS) == {"mainnet", "testnet"}
