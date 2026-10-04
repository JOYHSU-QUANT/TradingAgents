"""The run configuration: a YAML file, read into frozen values and checked.

::

    chain_id: 1
    quote_token: USDC
    tokens: [USDC, WETH, WBTC]
    pools: [USDC/WETH-500, WBTC/WETH-500]
    strategy:
      name: fixed_weights
      params:
        weights: {USDC: "0.5", WETH: "0.3", WBTC: "0.2"}
        band: "0.05"
    bars:                       # optional, and so is each key in it
      interval_seconds: 86400
      twap_window_seconds: 1800
      max_twap_deviation: "0.05"
      max_move: "0.5"
    execution:                  # optional, and so is each key in it
      min_trade_value: "10"
      max_slippage: "0.005"
      delay_blocks: 25
      model:
        slippage: "0.0005"
        gas_units_per_hop: 150000
    rpc:                        # optional
      url_env: ETH_RPC_URL

Tokens and pools are named by their keys in :mod:`.constants`; a config
cannot supply an address, and :class:`UniswapConfig` itself refuses a token
or pool that is not in those tables. The pools must form a tree that reaches
every token from the quote token: one path of pools then joins any two
tokens, and it is both how a token is priced and how it is swapped. Unknown keys are refused rather than
ignored, so a typo cannot silently fall back to a default, and so is a key
written twice, which YAML would otherwise settle in favour of the last. The
strategy's ``params`` are not interpreted here: they belong to the strategy,
which checks them when :func:`~.strategies.registry.build_strategy` builds
it. They are kept as a read-only copy, so a list arrives as a tuple.

``bars`` is read into a :class:`~.domain.bars.BarSettings`, whose defaults
stand for whatever is left out. Its two limits are quoted decimals, as a
strategy's numbers are. ``execution`` is read the same way into an
:class:`~.domain.execution.ExecutionSettings`; its ``model`` keys are the
fill model's own.

A file named ``*.local.yaml`` is gitignored inside this package. The config
holds no secret. ``rpc.url_env`` is the name of the environment variable
that holds the endpoint URL, never the URL; left out, the chain reader
(:mod:`.chain.rpc`) uses its own default, ``ETH_RPC_URL``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Hashable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar

import yaml

from .constants import POOLS, TOKENS, pool_key
from .domain.bars import BarSettings
from .domain.decimal_context import parse_decimal, plain
from .domain.execution import ExecutionSettings
from .domain.routing import find_route
from .domain.types import Pool, Token

__all__ = [
    "ConfigError",
    "StrategySpec",
    "UniswapConfig",
    "config_snapshot",
    "load_config",
    "parse_config",
]

_REQUIRED_KEYS: Final = frozenset({"chain_id", "quote_token", "tokens", "pools", "strategy"})
_KEYS: Final = _REQUIRED_KEYS | {"bars", "execution", "rpc"}
_STRATEGY_KEYS: Final = frozenset({"name", "params"})
_BARS_INTEGERS: Final = frozenset({"interval_seconds", "twap_window_seconds"})
_BARS_DECIMALS: Final = frozenset({"max_twap_deviation", "max_move"})
_RPC_KEYS: Final = frozenset({"url_env"})
_EXECUTION_DECIMALS: Final = frozenset({"min_trade_value", "max_slippage"})
_EXECUTION_KEYS: Final = _EXECUTION_DECIMALS | {"delay_blocks", "model"}
# ``execution.model``'s keys, and the setting each is read into.
_MODEL_KEYS: Final = MappingProxyType(
    {"slippage": "model_slippage", "gas_units_per_hop": "model_gas_units_per_hop"}
)
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_T = TypeVar("_T")


class ConfigError(ValueError):
    """A configuration that cannot be used, and the sentence says which key."""


def _frozen(value: object) -> object:
    """A read-only copy of a parsed YAML value: its mappings and lists, all the way down."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


@dataclass(frozen=True)
class StrategySpec:
    """Which strategy to build, and the parameters to hand its factory."""

    name: str
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigError(f"strategy.name must be a non-empty string, got {self.name!r}")
        if not isinstance(self.params, Mapping) or not all(
            isinstance(key, str) for key in self.params
        ):
            raise ConfigError(
                f"strategy.params must be a mapping with string keys, got {self.params!r}"
            )
        object.__setattr__(self, "params", _frozen(self.params))


def _repeated(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


@dataclass(frozen=True)
class UniswapConfig:
    """One run's configuration, its names already resolved against :mod:`.constants`.

    Every token and pool must be the chain's entry in those tables, however
    the config was built: the tables are the allowlist, and this is where it
    is enforced.
    """

    chain_id: int
    quote: Token
    tokens: tuple[Token, ...]
    pools: tuple[Pool, ...]
    strategy: StrategySpec
    bars: BarSettings = BarSettings()
    execution: ExecutionSettings = ExecutionSettings()
    # The environment variable that names the endpoint; ``None`` leaves it
    # to the chain reader's default.
    rpc_url_env: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id not in TOKENS
        ):
            raise ConfigError(f"chain_id must be one of {sorted(TOKENS)}, got {self.chain_id!r}")
        if not isinstance(self.tokens, tuple) or not isinstance(self.pools, tuple):
            raise ConfigError("tokens and pools must be tuples")
        if not isinstance(self.strategy, StrategySpec):
            raise ConfigError(f"strategy must be a StrategySpec, got {self.strategy!r}")
        if not isinstance(self.bars, BarSettings):
            raise ConfigError(f"bars must be a BarSettings, got {self.bars!r}")
        if not isinstance(self.execution, ExecutionSettings):
            raise ConfigError(f"execution must be an ExecutionSettings, got {self.execution!r}")
        if self.rpc_url_env is not None and (
            not isinstance(self.rpc_url_env, str) or not _ENV_NAME.fullmatch(self.rpc_url_env)
        ):
            # The value is not quoted: a URL written here by mistake holds the API key.
            raise ConfigError(
                "rpc.url_env must be the name of an environment variable (letters, digits "
                "and underscores), not the URL itself"
            )
        known_tokens = list(TOKENS[self.chain_id].values())
        for token in (self.quote, *self.tokens):
            if token not in known_tokens:
                raise ConfigError(f"{token!r} is not in chain {self.chain_id}'s token table")
        known_pools = list(POOLS[self.chain_id].values())
        for pool in self.pools:
            if pool not in known_pools:
                raise ConfigError(f"{pool!r} is not in chain {self.chain_id}'s pool table")

        symbols = [token.symbol for token in self.tokens]
        if _repeated(symbols):
            raise ConfigError(f"tokens lists {_repeated(symbols)} more than once")
        if self.quote not in self.tokens:
            raise ConfigError(f"quote_token {self.quote.symbol!r} must be one of tokens {symbols}")
        keys = [pool_key(pool) for pool in self.pools]
        if _repeated(keys):
            raise ConfigError(f"pools lists {_repeated(keys)} more than once")
        # A swap names two tokens and no pool, so a pair has one pool.
        pairs = [frozenset({pool.token0.symbol, pool.token1.symbol}) for pool in self.pools]
        for crowded in pairs:
            if pairs.count(crowded) > 1:
                raise ConfigError(f"pools lists more than one pool for the pair {sorted(crowded)}")
        pooled: set[str] = set()
        for pool in self.pools:
            pair = {pool.token0.symbol, pool.token1.symbol}
            if not pair <= set(symbols):
                raise ConfigError(
                    f"pool {pool_key(pool)!r} trades {sorted(pair - set(symbols))}, "
                    f"which tokens {symbols} does not list"
                )
            pooled |= pair
        # A token in no pool can be neither priced nor traded.
        stranded = [symbol for symbol in symbols if symbol not in pooled]
        if stranded:
            raise ConfigError(f"no configured pool trades {stranded}")
        # Every token reached from the quote, by as few pools as that takes:
        # a tree, so one path prices a token and one path swaps any two.
        for token in self.tokens:
            if token != self.quote:
                try:
                    find_route(self.pools, self.quote, token)
                except ValueError as exc:
                    raise ConfigError(f"pools: {exc}") from exc
        if len(self.pools) != len(self.tokens) - 1:
            raise ConfigError(
                f"pools must form a tree over the tokens: {len(self.tokens)} tokens are "
                f"joined by {len(self.tokens) - 1} pools, and {keys} form a loop"
            )


def _names(value: object, key: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list of names, got {value!r}")
    return value


def _resolve(table: Mapping[str, _T], name: object, what: str, chain_id: int) -> _T:
    if not isinstance(name, str) or name not in table:
        raise ConfigError(f"unknown {what} {name!r} on chain {chain_id}; known: {sorted(table)}")
    return table[name]


def _section(
    document: dict[Any, Any], key: str, allowed: frozenset[str], *, within: str = ""
) -> dict[Any, Any]:
    """An optional mapping of the config: empty when left out, refused with a key not allowed.

    ``within`` is the prefix a refusal names the section with, for one inside another.
    """
    section = document.get(key, {})
    if not isinstance(section, dict) or set(section) - allowed:
        raise ConfigError(
            f"{within}{key} must be a mapping with keys from {sorted(allowed)}, got {section!r}"
        )
    return section


def _bar_settings(document: dict[Any, Any]) -> BarSettings:
    section = _section(document, "bars", _BARS_INTEGERS | _BARS_DECIMALS)
    try:
        return BarSettings(
            **{
                key: parse_decimal(value, f"bars.{key}") if key in _BARS_DECIMALS else value
                for key, value in section.items()
            }
        )
    except ValueError as exc:
        raise ConfigError(f"bars: {exc}") from exc


def _execution_settings(document: dict[Any, Any]) -> ExecutionSettings:
    section = _section(document, "execution", _EXECUTION_KEYS)
    model = _section(section, "model", frozenset(_MODEL_KEYS), within="execution.")
    try:
        settings: dict[str, object] = {
            key: parse_decimal(value, f"execution.{key}") if key in _EXECUTION_DECIMALS else value
            for key, value in section.items()
            if key != "model"
        }
        for key, value in model.items():
            name = f"execution.model.{key}"
            settings[_MODEL_KEYS[key]] = parse_decimal(value, name) if key == "slippage" else value
        return ExecutionSettings(**settings)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigError(f"execution: {exc}") from exc


def _jsonable(value: object) -> object:
    """A frozen config value as JSON can write it: sets sorted, decimals as plain text.

    A decimal is written without padding, so ``0.50`` and ``0.5`` are one value.
    """
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, frozenset):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, Decimal):
        return plain(value)
    return value


def config_snapshot(config: UniswapConfig) -> str:
    """Everything in ``config`` that a run's decisions and fills depend on, as one JSON text.

    Two configs with the same snapshot run the same way, so a run keeps the
    snapshot it was started under and is not continued under another. Where
    the node's URL comes from is left out: it changes no decision.
    """
    try:
        return json.dumps(
            {
                "chain_id": config.chain_id,
                "quote_token": config.quote.symbol,
                "tokens": [token.symbol for token in config.tokens],
                "pools": [pool_key(pool) for pool in config.pools],
                "strategy": {
                    "name": config.strategy.name,
                    "params": _jsonable(config.strategy.params),
                },
                "bars": _jsonable(asdict(config.bars)),
                "execution": _jsonable(asdict(config.execution)),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"strategy.params cannot be written down as JSON ({exc})") from exc


def parse_config(document: object) -> UniswapConfig:
    """Check a parsed YAML document and resolve its names into a :class:`UniswapConfig`."""
    if not isinstance(document, dict):
        raise ConfigError("the config must hold a mapping")
    unknown = set(document) - _KEYS
    if unknown:
        raise ConfigError(
            f"unknown config key(s) {sorted(map(str, unknown))}; allowed: {sorted(_KEYS)}"
        )
    missing = _REQUIRED_KEYS - set(document)
    if missing:
        raise ConfigError(f"the config lacks {sorted(missing)}")

    chain_id = document["chain_id"]
    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id not in TOKENS:
        raise ConfigError(f"chain_id must be one of {sorted(TOKENS)}, got {chain_id!r}")
    tokens, pools = TOKENS[chain_id], POOLS[chain_id]

    strategy = document["strategy"]
    if not isinstance(strategy, dict) or "name" not in strategy or set(strategy) - _STRATEGY_KEYS:
        raise ConfigError(
            f"strategy must be a mapping with a name and optional params, got {strategy!r}"
        )
    params = strategy.get("params")

    return UniswapConfig(
        chain_id=chain_id,
        quote=_resolve(tokens, document["quote_token"], "token", chain_id),
        tokens=tuple(
            _resolve(tokens, name, "token", chain_id)
            for name in _names(document["tokens"], "tokens")
        ),
        pools=tuple(
            _resolve(pools, name, "pool", chain_id) for name in _names(document["pools"], "pools")
        ),
        strategy=StrategySpec(name=strategy["name"], params={} if params is None else params),
        bars=_bar_settings(document),
        execution=_execution_settings(document),
        rpc_url_env=_section(document, "rpc", _RPC_KEYS).get("url_env"),
    )


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader``, except that a key written twice in one mapping is an error.

    A merge key (``<<``) is refused as well: what it merges in could repeat
    a key without the repeat being written anywhere.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None, None, "merge keys (<<) are not supported", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            # An unhashable key is the base class's to refuse.
            if isinstance(key, Hashable):
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"the key {key!r} appears more than once", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_config(path: Path) -> UniswapConfig:
    """Read and check the config file at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"config file {str(path)!r} cannot be read ({exc})") from exc
    # The recursion guard spans both steps: an anchor that contains itself
    # loads, and only runs away when its params are frozen.
    try:
        return parse_config(yaml.load(text, Loader=_UniqueKeyLoader))
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigError(f"config file {str(path)!r} cannot be parsed ({exc})") from exc
