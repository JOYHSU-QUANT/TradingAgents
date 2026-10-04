"""The config loader: the shipped example loads, and every malformed shape is refused by name."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3 import config as config_module
from contrib.uniswap_v3.config import (
    ConfigError,
    StrategySpec,
    UniswapConfig,
    load_config,
    parse_config,
)
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.types import Pool, Token
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
    with pytest.raises(ConfigError, match="cannot be parsed"):
        load_config(broken)
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="must hold a mapping"):
        load_config(empty)
    not_utf8 = tmp_path / "latin1.yaml"
    not_utf8.write_bytes(b"chain_id: \xff\n")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(not_utf8)


@pytest.mark.parametrize(
    ("repeat", "key"),
    [
        ("quote_token: WETH\n", "quote_token"),
        ("tokens: [USDC, WETH]\n", "tokens"),
        ("strategy:\n  name: other\n", "strategy"),
    ],
)
def test_a_key_written_twice_is_refused_rather_than_the_last_winning(tmp_path, repeat, key):
    doubled = tmp_path / "doubled.yaml"
    doubled.write_text(EXAMPLE.read_text(encoding="utf-8") + repeat, encoding="utf-8")
    with pytest.raises(ConfigError, match=f"the key '{key}' appears more than once"):
        load_config(doubled)


def test_a_key_written_twice_inside_the_strategy_is_refused_too(tmp_path):
    doubled = tmp_path / "doubled.yaml"
    doubled.write_text(
        EXAMPLE.read_text(encoding="utf-8") + '    band: "0.5"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="the key 'band' appears more than once"):
        load_config(doubled)


@pytest.mark.parametrize(
    ("strategy", "match"),
    [
        # An anchor that contains itself loads, then never stops being frozen.
        ("strategy: {name: x, params: &loop {again: *loop}}\n", "cannot be parsed"),
        ("strategy: {name: x, params: {<<: {band: '0.05'}}}\n", "merge keys"),
    ],
)
def test_yaml_that_loops_or_merges_is_refused(tmp_path, strategy, match):
    path = tmp_path / "odd.yaml"
    path.write_text(
        "chain_id: 1\nquote_token: USDC\ntokens: [USDC, WETH]\npools: [USDC/WETH-500]\n"
        + strategy,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=match):
        load_config(path)


def test_a_set_in_strategy_params_is_frozen_too():
    assert StrategySpec(name="x", params={"only": {"USDC"}}).params["only"] == frozenset({"USDC"})
    assert isinstance(StrategySpec(name="x", params={"only": {"USDC"}}).params["only"], frozenset)


def test_strategy_params_are_read_only_all_the_way_down():
    params = load_config(EXAMPLE).strategy.params
    with pytest.raises(TypeError):
        params["weights"]["USDC"] = "1"  # type: ignore[index]
    spec = StrategySpec(name="x", params={"levels": [{"a": 1}]})
    assert spec.params["levels"] == ({"a": 1},)
    with pytest.raises(TypeError):
        spec.params["levels"][0]["a"] = 2  # type: ignore[index]


def _config(**overrides: object) -> UniswapConfig:
    fields: dict = {
        "chain_id": 1,
        "quote": _TOKENS["USDC"],
        "tokens": (_TOKENS["USDC"], _TOKENS["WETH"]),
        "pools": (_POOLS["USDC/WETH-500"],),
        "strategy": StrategySpec(name="x", params={}),
    }
    return UniswapConfig(**{**fields, **overrides})


def test_a_config_built_by_hand_from_the_tables_is_accepted():
    assert _config().quote is _TOKENS["USDC"]


IMPOSTOR = Token("WETH", "0x" + "f" * 40, 18)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"chain_id": 999}, "chain_id must be one of"),
        ({"chain_id": True}, "chain_id must be one of"),
        # The right symbol at another address is not the allowlisted token.
        ({"tokens": (_TOKENS["USDC"], IMPOSTOR)}, "is not in chain 1's token table"),
        ({"quote": "USDC"}, "is not in chain 1's token table"),
        (
            {"pools": (Pool("0x" + "1" * 40, _TOKENS["USDC"], _TOKENS["WETH"], 500),)},
            "is not in chain 1's pool table",
        ),
        ({"tokens": [_TOKENS["USDC"], _TOKENS["WETH"]]}, "must be tuples"),
        ({"pools": [_POOLS["USDC/WETH-500"]]}, "must be tuples"),
        ({"strategy": {"name": "x"}}, "strategy must be a StrategySpec"),
    ],
)
def test_a_config_built_by_hand_cannot_step_outside_the_tables(overrides, match):
    with pytest.raises(ConfigError, match=match):
        _config(**overrides)


def test_two_pools_for_one_pair_are_refused(monkeypatch):
    # The tables hold one pool per pair today, so a second fee tier is added
    # for the test: a swap names two tokens and could not say which to use.
    second = Pool("0x" + "1" * 40, _TOKENS["USDC"], _TOKENS["WETH"], 3000)
    monkeypatch.setattr(
        config_module, "POOLS", {ETHEREUM_MAINNET: {**_POOLS, "USDC/WETH-3000": second}}
    )
    with pytest.raises(ConfigError, match=r"more than one pool for the pair \['USDC', 'WETH'\]"):
        parse_config(_document(pools=["USDC/WETH-500", "WBTC/WETH-500", "USDC/WETH-3000"]))
