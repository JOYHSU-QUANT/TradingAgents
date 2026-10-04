"""The config loader: the shipped example loads, and every malformed shape is refused by name."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3 import config as config_module
from contrib.uniswap_v3.chain.rpc import DEFAULT_URL_ENV
from contrib.uniswap_v3.config import (
    ConfigError,
    StrategySpec,
    UniswapConfig,
    config_from_snapshot,
    config_snapshot,
    load_config,
    parse_config,
)
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.bars import BarSettings
from contrib.uniswap_v3.domain.execution import ExecutionSettings
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


# --- bars and rpc ----------------------------------------------------------


def test_bars_and_rpc_may_be_left_out_and_then_the_defaults_stand():
    config = parse_config(_document())
    assert config.bars == BarSettings()
    assert config.bars.interval_seconds == 86_400
    assert config.rpc_url_env is None


def test_the_shipped_example_spells_out_the_default_bars_and_the_default_variable():
    config = load_config(EXAMPLE)
    assert config.bars == BarSettings()
    assert config.rpc_url_env == DEFAULT_URL_ENV


def test_bars_keys_are_read_one_by_one_over_the_defaults():
    config = parse_config(
        _document(bars={"interval_seconds": 3_600, "max_twap_deviation": "0.02"})
    )
    assert config.bars == BarSettings(
        interval_seconds=3_600, max_twap_deviation=Decimal("0.02")
    )
    assert parse_config(_document(bars={})).bars == BarSettings()
    assert parse_config(_document(rpc={"url_env": "MY_NODE"})).rpc_url_env == "MY_NODE"
    assert parse_config(_document(rpc={})).rpc_url_env is None


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"bars": {"interval": 3_600}}, "bars must be a mapping with keys from"),
        ({"bars": [86_400]}, "bars must be a mapping"),
        ({"bars": None}, "bars must be a mapping"),
        ({"bars": {"interval_seconds": 0}}, "bars: interval_seconds must be a positive integer"),
        ({"bars": {"interval_seconds": "86400"}}, "bars: interval_seconds"),
        ({"bars": {"twap_window_seconds": 2**32}}, "bars: twap_window_seconds"),
        ({"bars": {"max_twap_deviation": 0.05}}, "bars.max_twap_deviation must be a quoted decimal"),
        ({"bars": {"max_move": "0"}}, "bars: max_move must be a finite, positive Decimal"),
        ({"rpc": {"url": "https://node.example"}}, "rpc must be a mapping with keys from"),
        ({"rpc": "ETH_RPC_URL"}, "rpc must be a mapping"),
        ({"rpc": {"url_env": ""}}, "rpc.url_env must be the name of an environment variable"),
        ({"rpc": {"url_env": 7}}, "rpc.url_env must be the name of an environment variable"),
    ],
)
def test_a_malformed_bars_or_rpc_section_is_refused_by_name(overrides, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(_document(**overrides))


@pytest.mark.parametrize("value", ["https://node.example/v2/secret-key", "MY NODE", "1NODE", "a-b"])
def test_a_url_env_that_is_not_a_variable_name_is_refused_without_being_echoed(value):
    with pytest.raises(ConfigError) as refusal:
        parse_config(_document(rpc={"url_env": value}))
    assert "the name of an environment variable" in str(refusal.value)
    assert value not in str(refusal.value)


def test_a_config_built_by_hand_checks_its_bars_and_its_variable_name():
    fields = {
        "chain_id": ETHEREUM_MAINNET,
        "quote": _TOKENS["USDC"],
        "tokens": (_TOKENS["USDC"], _TOKENS["WETH"]),
        "pools": (_POOLS["USDC/WETH-500"],),
        "strategy": StrategySpec(name="fixed_weights", params={}),
    }
    with pytest.raises(ConfigError, match="bars must be a BarSettings"):
        UniswapConfig(**fields, bars={"interval_seconds": 3_600})
    with pytest.raises(ConfigError, match="rpc.url_env must be the name of an environment variable"):
        UniswapConfig(**fields, rpc_url_env=" ")
    with pytest.raises(ConfigError, match="execution must be an ExecutionSettings"):
        UniswapConfig(**fields, execution={"delay_blocks": 25})


# --- execution -------------------------------------------------------------


def test_execution_may_be_left_out_and_the_shipped_example_spells_out_its_defaults():
    assert parse_config(_document()).execution == ExecutionSettings()
    assert parse_config(_document(execution={})).execution == ExecutionSettings()
    assert parse_config(_document(execution={"model": {}})).execution == ExecutionSettings()
    assert load_config(EXAMPLE).execution == ExecutionSettings()


def test_execution_keys_are_read_one_by_one_over_the_defaults():
    config = parse_config(
        _document(
            execution={
                "min_trade_value": "25",
                "delay_blocks": 5,
                "model": {"slippage": "0.002", "gas_units_per_hop": 180_000},
            }
        )
    )
    assert config.execution == ExecutionSettings(
        min_trade_value=Decimal("25"),
        delay_blocks=5,
        model_slippage=Decimal("0.002"),
        model_gas_units_per_hop=180_000,
    )
    exact = parse_config(_document(execution={"max_slippage": 0, "model": {"slippage": 0}}))
    assert exact.execution.max_slippage == 0


@pytest.mark.parametrize(
    ("execution", "match"),
    [
        ({"slippage": "0.01"}, "execution must be a mapping with keys from"),
        ([25], "execution must be a mapping"),
        ({"max_slippage": 0.005}, "execution.max_slippage must be a quoted decimal"),
        ({"max_slippage": "1"}, r"execution: max_slippage must be a Decimal in \[0, 1\)"),
        ({"min_trade_value": "-10"}, "execution: min_trade_value"),
        ({"delay_blocks": "25"}, "execution: delay_blocks must be an integer"),
        ({"model": {"gas": 1}}, "execution.model must be a mapping with keys from"),
        ({"model": "cheap"}, "execution.model must be a mapping"),
        ({"model": {"slippage": 0.001}}, "execution.model.slippage must be a quoted decimal"),
        ({"model": {"gas_units_per_hop": 0}}, "execution: model_gas_units_per_hop"),
        ({"quote": {"gas_overhead_units": -1}}, "execution: quote_gas_overhead_units"),
        ({"quote": {"gas_overhead_units": "50000"}}, "execution: quote_gas_overhead_units"),
        ({"quote": {"gas_units": 1}}, "execution.quote must be a mapping with keys from"),
        ({"quote": 50_000}, "execution.quote must be a mapping with keys from"),
        ({"model": {"slippage": "0.01"}}, "execution: model_slippage 0.01 must not be above"),
    ],
)
def test_a_malformed_execution_section_is_refused_by_name(execution, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(_document(execution=execution))


# --- the pools form a tree -------------------------------------------------


def test_pools_that_form_a_loop_are_refused(monkeypatch):
    # No such pool is in the tables: with it a token would have two paths to the quote.
    third = Pool("0x" + "1" * 40, _TOKENS["WBTC"], _TOKENS["USDC"], 3000)
    monkeypatch.setattr(
        config_module, "POOLS", {ETHEREUM_MAINNET: {**_POOLS, "WBTC/USDC-3000": third}}
    )
    with pytest.raises(ConfigError, match="3 tokens are joined by 2 pools, and .* form a loop"):
        parse_config(_document(pools=["USDC/WETH-500", "WBTC/WETH-500", "WBTC/USDC-3000"]))


def test_pools_that_do_not_reach_every_token_from_the_quote_are_refused(monkeypatch):
    # Two more tokens with a pool of their own: every token is in a pool, and two are cut off.
    dai, link = Token("DAI", "0x" + "6" * 40, 18), Token("LINK", "0x" + "7" * 40, 18)
    island = Pool("0x" + "1" * 40, dai, link, 3000)
    monkeypatch.setattr(
        config_module, "TOKENS", {ETHEREUM_MAINNET: {**_TOKENS, "DAI": dai, "LINK": link}}
    )
    monkeypatch.setattr(
        config_module, "POOLS", {ETHEREUM_MAINNET: {**_POOLS, "DAI/LINK-3000": island}}
    )
    with pytest.raises(ConfigError, match="pools: no pool path joins USDC to DAI"):
        parse_config(
            _document(
                tokens=["USDC", "WETH", "DAI", "LINK"], pools=["USDC/WETH-500", "DAI/LINK-3000"]
            )
        )


# --- the snapshot ----------------------------------------------------------


def test_a_snapshot_names_everything_a_run_depends_on_and_is_the_same_each_time():
    config = load_config(EXAMPLE)
    snapshot = config_snapshot(config)
    assert snapshot == config_snapshot(load_config(EXAMPLE))
    assert json.loads(snapshot) == {
        "chain_id": 1,
        "quote_token": "USDC",
        "tokens": ["USDC", "WETH", "WBTC"],
        "pools": ["USDC/WETH-500", "WBTC/WETH-500"],
        "strategy": {
            "name": "fixed_weights",
            "params": {"weights": {"USDC": "0.5", "WETH": "0.3", "WBTC": "0.2"}, "band": "0.05"},
        },
        "bars": {
            "interval_seconds": 86_400,
            "twap_window_seconds": 1_800,
            "max_twap_deviation": "0.02",
            "max_move": "0.5",
        },
        # The config file's own shape, so a section's keys sit under its name.
        "execution": {
            "min_trade_value": "10",
            "max_slippage": "0.005",
            "delay_blocks": 25,
            "model": {"slippage": "0.0005", "gas_units_per_hop": 150_000},
            "quote": {"gas_overhead_units": 50_000},
        },
    }


def test_a_snapshot_changes_with_what_changes_a_run_and_not_with_where_the_node_is():
    config = parse_config(_document())
    snapshot = config_snapshot(config)
    assert config_snapshot(parse_config(_document(rpc={"url_env": "MY_NODE"}))) == snapshot
    # A number written with padding is the same number.
    assert config_snapshot(parse_config(_document(bars={"max_move": "0.50"}))) == snapshot
    for changed in (
        _document(execution={"delay_blocks": 24}),
        _document(execution={"model": {"gas_units_per_hop": 150_001}}),
        _document(bars={"max_move": "0.4"}),
        _document(strategy={"name": "fixed_weights", "params": {"band": "0.06"}}),
        _document(quote_token="WETH"),
    ):
        assert config_snapshot(parse_config(changed)) != snapshot


def test_a_snapshot_reads_back_as_the_config_it_was_taken_of():
    config = load_config(EXAMPLE)
    read_back = config_module.config_from_snapshot(config_snapshot(config))
    # Where the node's URL comes from is no part of a snapshot.
    assert read_back.rpc_url_env is None
    assert config_snapshot(read_back) == config_snapshot(config)
    assert (read_back.tokens, read_back.pools, read_back.bars, read_back.execution) == (
        config.tokens,
        config.pools,
        config.bars,
        config.execution,
    )
    with pytest.raises(ConfigError, match="the config snapshot is not JSON"):
        config_module.config_from_snapshot("chain_id: 1")


def test_a_snapshot_writes_frozen_params_down_and_refuses_what_json_cannot_hold():
    spec = StrategySpec(name="x", params={"only": {"WETH", "USDC"}, "levels": [Decimal("0.1")]})
    snapshot = json.loads(config_snapshot(_config(strategy=spec)))
    assert snapshot["strategy"]["params"] == {"only": ["USDC", "WETH"], "levels": ["0.1"]}
    for unwritable in (object(), float("nan")):
        with pytest.raises(ConfigError, match="cannot be written down as JSON"):
            config_snapshot(_config(strategy=StrategySpec(name="x", params={"odd": unwritable})))


def test_the_quoted_fills_gas_overhead_is_read_and_may_be_zero():
    assert parse_config(_document()).execution.quote_gas_overhead_units == 50_000
    config = parse_config(_document(execution={"quote": {"gas_overhead_units": 0}}))
    assert config.execution.quote_gas_overhead_units == 0
    assert config_from_snapshot(config_snapshot(config)) == config


def test_a_bar_is_suspect_two_percent_from_its_twap_unless_the_config_says_otherwise():
    assert parse_config(_document()).bars.max_twap_deviation == Decimal("0.02")
