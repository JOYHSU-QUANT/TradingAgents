"""Tests for config loading and the wallet-address guard.

``wallet_address`` is the gate that decides whether ``--context-only`` attempts a
position read: anything empty, whitespace, or the example placeholder must read
as "no wallet configured" (``None``).
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.config import (
    _WALLET_PLACEHOLDER,
    load_config,
    wallet_address,
)


@pytest.mark.parametrize(
    "value",
    ["", "   ", _WALLET_PLACEHOLDER, None],
)
def test_wallet_address_unset_or_placeholder_is_none(value):
    assert wallet_address({"wallet_address": value}) is None


def test_wallet_address_missing_key_is_none():
    assert wallet_address({}) is None


def test_wallet_address_real_value_is_stripped():
    assert wallet_address({"wallet_address": "  0xABC123  "}) == "0xABC123"


def test_load_config_reads_example_by_default():
    # No local.yaml in a clean checkout -> falls back to the committed example.
    config = load_config()
    assert config["network"] == "mainnet"
    assert config["coins"] == ["BTC"]
    assert "market_data" in config


def test_load_config_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_rejects_unknown_top_level_key(tmp_path):
    # A typo'd block name (`riks:`) would otherwise silently drop the whole
    # risk: block and trade at default caps — reject it loudly instead.
    bad = tmp_path / "typo.yaml"
    bad.write_text("riks:\n  leverage: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level config key"):
        load_config(bad)


def test_load_config_rejects_non_mapping(tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(bad)
