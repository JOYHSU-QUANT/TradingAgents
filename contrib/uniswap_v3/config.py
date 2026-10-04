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

Tokens and pools are named by their keys in :mod:`.constants`; a config
cannot supply an address, and :class:`UniswapConfig` itself refuses a token
or pool that is not in those tables. Unknown keys are refused rather than
ignored, so a typo cannot silently fall back to a default, and so is a key
written twice, which YAML would otherwise settle in favour of the last. The
strategy's ``params`` are not interpreted here: they belong to the strategy,
which checks them when :func:`~.strategies.registry.build_strategy` builds
it. They are kept as a read-only copy, so a list arrives as a tuple.

A file named ``*.local.yaml`` is gitignored inside this package. The config
holds no secret and names no environment variable: the chain reader
(:mod:`.chain.rpc`) takes its endpoint from ``ETH_RPC_URL`` unless its own
settings name another variable.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeVar

import yaml

from .constants import POOLS, TOKENS, pool_key
from .domain.types import Pool, Token

__all__ = ["ConfigError", "StrategySpec", "UniswapConfig", "load_config", "parse_config"]

_KEYS: Final = frozenset({"chain_id", "quote_token", "tokens", "pools", "strategy"})
_STRATEGY_KEYS: Final = frozenset({"name", "params"})

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


def _names(value: object, key: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list of names, got {value!r}")
    return value


def _resolve(table: Mapping[str, _T], name: object, what: str, chain_id: int) -> _T:
    if not isinstance(name, str) or name not in table:
        raise ConfigError(f"unknown {what} {name!r} on chain {chain_id}; known: {sorted(table)}")
    return table[name]


def parse_config(document: object) -> UniswapConfig:
    """Check a parsed YAML document and resolve its names into a :class:`UniswapConfig`."""
    if not isinstance(document, dict):
        raise ConfigError("the config must hold a mapping")
    unknown = set(document) - _KEYS
    if unknown:
        raise ConfigError(
            f"unknown config key(s) {sorted(map(str, unknown))}; allowed: {sorted(_KEYS)}"
        )
    missing = _KEYS - set(document)
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
