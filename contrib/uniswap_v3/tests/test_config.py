"""The config loader: the shipped example loads, and every malformed shape is refused by name."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3.config import ConfigError, StrategySpec, load_config, parse_config
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.strategies.fixed_weights import FixedWeights
from contrib.uniswap_v3.strategies.registry import build_strategy

EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_TOKENS = TOKENS[ETHEREUM_MAINNET]
_POOLS = POOLS[ETHEREUM_MAINNET]


def _document(**overrides: object) -> dict:
    document: dict = {
        "chain_id": 1,
        "quote_token": "USDC",
        "tokens": ["USDC", "WETH", "WBTC"],
        "pools": ["USDC/WETH-500", "WBTC/WETH-500"],
        "strategy": {"name": "fixed_weights", "params": {"band": "0.05"}},
    }
    document.update(overrides)
    return document


def test_the_shipped_example_loads_and_names_what_it_says():
    config = load_config(EXAMPLE)
    assert config.chain_id == 1
    assert config.quote is _TOKENS["USDC"]
    assert config.tokens == (_TOKENS["USDC"], _TOKENS["WETH"], _TOKENS["WBTC"])
    assert config.pools == (_POOLS["USDC/WETH-500"], _POOLS["WBTC/WETH-500"])
    assert config.strategy == StrategySpec(
        name="fixed_weights",
        params={"weights": {"USDC": "0.5", "WETH": "0.3", "WBTC": "0.2"}, "band": "0.05"},
    )


def test_the_shipped_examples_strategy_builds_and_targets_the_configured_tokens():
    config = load_config(EXAMPLE)
    strategy = build_strategy(config.strategy.name, config.strategy.params)
    assert isinstance(strategy, FixedWeights)
    assert strategy.band == Decimal("0.05")
    assert set(strategy.target.weights) == {token.symbol for token in config.tokens}


def test_strategy_params_may_be_left_out_and_are_read_only():
    config = parse_config(_document(strategy={"name": "anything"}))
    assert config.strategy.params == {}
    with pytest.raises(TypeError):
        config.strategy.params["band"] = "0.1"  # type: ignore[index]


def test_a_two_token_config_needs_only_the_pool_between_them():
    config = parse_config(
        _document(quote_token="WETH", tokens=["WETH", "WBTC"], pools=["WBTC/WETH-500"])
    )
    assert config.quote is _TOKENS["WETH"]
    assert config.pools == (_POOLS["WBTC/WETH-500"],)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"rpc_url": "https://example.invalid"}, r"unknown config key\(s\) \['rpc_url'\]"),
        ({"chain_id": 5}, r"chain_id must be one of \[1\], got 5"),
        ({"chain_id": True}, "chain_id must be one of"),
        ({"chain_id": "1"}, "chain_id must be one of"),
        ({"quote_token": "DAI"}, "unknown token 'DAI' on chain 1"),
        ({"quote_token": ["USDC"]}, "unknown token"),
        ({"tokens": ["USDC", "WETH", "DAI"]}, "unknown token 'DAI' on chain 1"),
        ({"tokens": []}, "tokens must be a non-empty list"),
        ({"tokens": "USDC"}, "tokens must be a non-empty list"),
        ({"tokens": ["USDC", "WETH", "WBTC", "WETH"]}, r"tokens lists \['WETH'\] more than once"),
        (
            {"tokens": ["WETH", "WBTC"], "pools": ["WBTC/WETH-500"]},
            "quote_token 'USDC' must be one of tokens",
        ),
        ({"pools": ["USDC/WETH-3000"]}, "unknown pool 'USDC/WETH-3000' on chain 1"),
        ({"pools": []}, "pools must be a non-empty list"),
        (
            {"pools": ["USDC/WETH-500", "WBTC/WETH-500", "USDC/WETH-500"]},
            r"pools lists \['USDC/WETH-500'\] more than once",
        ),
        ({"tokens": ["USDC", "WETH"]}, r"pool 'WBTC/WETH-500' trades \['WBTC'\]"),
        ({"pools": ["USDC/WETH-500"]}, r"no configured pool trades \['WBTC'\]"),
        ({"strategy": "fixed_weights"}, "strategy must be a mapping with a name"),
        ({"strategy": {}}, "strategy must be a mapping with a name"),
        ({"strategy": {"name": "x", "band": "0.05"}}, "strategy must be a mapping with a name"),
        ({"strategy": {"name": ""}}, "strategy.name"),
        ({"strategy": {"name": 7}}, "strategy.name"),
        ({"strategy": {"name": "x", "params": ["band"]}}, "strategy.params"),
        ({"strategy": {"name": "x", "params": {1: "0.05"}}}, "strategy.params"),
    ],
)
def test_a_malformed_config_is_refused_by_name(overrides, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(_document(**overrides))


@pytest.mark.parametrize("key", ["chain_id", "quote_token", "tokens", "pools", "strategy"])
def test_every_top_level_key_is_required(key):
    document = _document()
    del document[key]
    with pytest.raises(ConfigError, match=rf"the config lacks \['{key}'\]"):
        parse_config(document)


@pytest.mark.parametrize("document", [None, [], "chain_id: 1", 1])
def test_a_document_that_is_not_a_mapping_is_refused(document):
    with pytest.raises(ConfigError, match="must hold a mapping"):
        parse_config(document)


def test_a_file_that_cannot_be_read_or_parsed_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(tmp_path / "absent.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("tokens: [USDC\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is not YAML"):
        load_config(broken)
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="must hold a mapping"):
        load_config(empty)
