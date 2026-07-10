"""Tests for config loading and the wallet-address guard.

``wallet_address`` is the gate that decides whether ``--context-only`` attempts a
position read: anything empty, whitespace, or the example placeholder must read
as "no wallet configured" (``None``).
"""

from __future__ import annotations

import os

import pytest

from contrib.hyperliquid_perp.config import (
    _WALLET_PLACEHOLDER,
    load_config,
    load_dotenv_files,
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


@pytest.mark.parametrize("text", ["market_data: 5\n", "engine:\n  - a\n"])
def test_load_config_rejects_non_mapping_block(tmp_path, text):
    # A container block that isn't a mapping would blow up deep in the run
    # (`5.get(...)`) instead of a clean exit-1 — reject it at load time.
    bad = tmp_path / "block.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(bad)


def test_load_config_drops_blank_top_level_keys(tmp_path):
    # ``market_data:`` with nothing after it (a normal state when an operator
    # comments out a block's contents) parses to None, not a missing key. It is
    # treated exactly like absent — dropped — so every consumer's default applies
    # instead of crashing on ``None.get(...)`` deep in the run.
    blank = tmp_path / "blank.yaml"
    blank.write_text("network:\nmarket_data:\nengine:\ncoins: [BTC]\n", encoding="utf-8")
    config = load_config(blank)
    assert "network" not in config
    assert "market_data" not in config
    assert "engine" not in config
    assert config["coins"] == ["BTC"]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("network: mainet\n", "'network' must be"),
        ("network: 5\n", "'network' must be"),
        ("network_timeout_s: abc\n", "'network_timeout_s' must be a number"),
        ("network_timeout_s: [30]\n", "'network_timeout_s' must be a number"),
        ("wallet_address: 123\n", "'wallet_address' must be a string"),
    ],
)
def test_load_config_rejects_bad_phase1_values(tmp_path, text, match):
    # These values are consumed by the Phase-1 client deep inside the run; a bad
    # value there surfaces as an exit-2 traceback instead of a named config error.
    # Rejecting at load keeps operator typos in the CONFIG_LOAD_ERRORS lane.
    bad = tmp_path / "value.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_config(bad)


def test_load_config_accepts_phase1_value_spellings_the_client_accepts(tmp_path):
    # Mixed-case network and a numeric string timeout are both legal downstream
    # (sdk_client strips/lowers and float()s) — load_config must not be stricter
    # than the consumer it fronts.
    good = tmp_path / "good.yaml"
    good.write_text(
        'network: TestNet\nnetwork_timeout_s: "15"\nwallet_address: "0xabc"\n',
        encoding="utf-8",
    )
    assert load_config(good)["network"] == "TestNet"


def test_load_config_rejects_non_list_coins(tmp_path):
    # `coins: BTC` (scalar, not a list) would otherwise silently resolve to the
    # first character "B" — reject it as a config error instead.
    bad = tmp_path / "coins.yaml"
    bad.write_text("coins: BTC\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'coins' must be a list"):
        load_config(bad)


# --------------------------------------------------------------------------
# load_dotenv_files — repo-root .env must satisfy the CLI startup key checks
# --------------------------------------------------------------------------


def test_load_dotenv_files_reads_env_from_cwd(tmp_path, monkeypatch):
    # The engine package loads .env on import, but the CLIs check
    # OPENROUTER_API_KEY before that lazy import — so this helper must find the
    # same file (find_dotenv walks up from the CWD) on its own.
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    load_dotenv_files()

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-dotenv"


def test_load_dotenv_files_never_overrides_exported_vars(tmp_path, monkeypatch):
    # Same contract as tradingagents/__init__: an exported variable always wins
    # over the file, so a shell override for one run cannot be clobbered.
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-exported")

    load_dotenv_files()

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-exported"


def test_load_dotenv_files_reads_env_enterprise(tmp_path, monkeypatch):
    # Both upstream files are mirrored; .env.enterprise is the second load.
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / ".env.enterprise").write_text("HL_ENTERPRISE_MARKER=yes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HL_ENTERPRISE_MARKER", raising=False)

    load_dotenv_files()

    assert os.environ["HL_ENTERPRISE_MARKER"] == "yes"
