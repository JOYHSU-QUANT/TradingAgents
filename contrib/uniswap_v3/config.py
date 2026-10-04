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
cannot supply an address. Unknown keys are refused rather than ignored, so a
typo cannot silently fall back to a default. The strategy's ``params`` are
kept as written: they belong to the strategy, which checks them when
:func:`~.strategies.registry.build_strategy` builds it.

A file named ``*.local.yaml`` is gitignored inside this package. The config
holds no secret and names no environment variable yet; the RPC endpoint
arrives with the chain reader.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeVar

import yaml

from .constants import POOLS, TOKENS, pool_key
from .domain.types import Pool, Token

__all__ = ["ConfigError", "StrategySpec", "UniswapConfig", "load_config", "parse_config"]

_KEYS: Final = frozenset({"chain_id", "quote_token", "tokens", "pools", "strategy"})
_STRATEGY_KEYS: Final = frozenset({"name", "params"})

_T = TypeVar("_T")


class ConfigError(ValueError):
    """A configuration that cannot be used, and the sentence says which key."""


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
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


def _repeated(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


@dataclass(frozen=True)
class UniswapConfig:
    """One run's configuration, its names already resolved against :mod:`.constants`."""

    chain_id: int
    quote: Token
    tokens: tuple[Token, ...]
    pools: tuple[Pool, ...]
    strategy: StrategySpec

    def __post_init__(self) -> None:
        symbols = [token.symbol for token in self.tokens]
        if _repeated(symbols):
            raise ConfigError(f"tokens lists {_repeated(symbols)} more than once")
        if self.quote not in self.tokens:
            raise ConfigError(f"quote_token {self.quote.symbol!r} must be one of tokens {symbols}")
        keys = [pool_key(pool) for pool in self.pools]
        if _repeated(keys):
            raise ConfigError(f"pools lists {_repeated(keys)} more than once")
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


def load_config(path: Path) -> UniswapConfig:
    """Read and check the config file at ``path``."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"config file {str(path)!r} cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {str(path)!r} is not YAML ({exc})") from exc
    return parse_config(document)
