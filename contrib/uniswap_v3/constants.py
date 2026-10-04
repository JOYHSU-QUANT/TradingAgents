"""Token and pool addresses, one table per chain ID.

These tables are an allowlist. A config names tokens and pools by the keys
used here and cannot bring an address of its own, so adding a token is one
entry here, with its source, plus one line of config.

How the Ethereum mainnet entries were checked (2026-10-04):

- WETH is the address Uniswap's own deployments page lists:
  https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments
- All three token addresses are valid EIP-55 checksums.
- Each pool address is the CREATE2 address the UniswapV3Factory on that page
  (``0x1F98431c8aD98523631AE4a59f267346ea31F984``) derives from the two token
  addresses and the fee, with v3-core's pool init code hash. A wrong token
  address would not have reproduced the pool address.

Token pages, for the decimals:

- USDC: https://etherscan.io/token/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
- WETH: https://etherscan.io/token/0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
- WBTC: https://etherscan.io/token/0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .domain.types import Pool, Token

__all__ = ["ETHEREUM_MAINNET", "POOLS", "TOKENS", "pool_key"]

ETHEREUM_MAINNET: Final = 1


def pool_key(pool: Pool) -> str:
    """The name a config refers to ``pool`` by: ``USDC/WETH-500``."""
    return f"{pool.token0.symbol}/{pool.token1.symbol}-{pool.fee}"


def _by_symbol(*tokens: Token) -> Mapping[str, Token]:
    return MappingProxyType({token.symbol: token for token in tokens})


def _by_key(*pools: Pool) -> Mapping[str, Pool]:
    return MappingProxyType({pool_key(pool): pool for pool in pools})


_USDC: Final = Token("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)
_WETH: Final = Token("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18)
_WBTC: Final = Token("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8)

TOKENS: Final[Mapping[int, Mapping[str, Token]]] = MappingProxyType(
    {ETHEREUM_MAINNET: _by_symbol(_USDC, _WETH, _WBTC)}
)

# Both are the 0.05% tier (fee 500).
POOLS: Final[Mapping[int, Mapping[str, Pool]]] = MappingProxyType(
    {
        ETHEREUM_MAINNET: _by_key(
            Pool("0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", _USDC, _WETH, 500),
            Pool("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", _WBTC, _WETH, 500),
        )
    }
)
