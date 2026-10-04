"""The address tables: pinned entry by entry, and consistent with their own keys.

The pins are the point. An address edited by accident, or a token added
without a decision, fails here by name.
"""

from __future__ import annotations

import pytest
from web3 import Web3

from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, QUOTER_V2, TOKENS, pool_key


def test_the_mainnet_tokens_are_the_three_decided():
    assert {
        symbol: (token.address, token.decimals)
        for symbol, token in TOKENS[ETHEREUM_MAINNET].items()
    } == {
        "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        "WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
        "WBTC": ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
    }


def test_the_mainnet_pools_are_the_two_005_percent_pools():
    assert {
        key: (pool.address, pool.token0.symbol, pool.token1.symbol, pool.fee)
        for key, pool in POOLS[ETHEREUM_MAINNET].items()
    } == {
        "USDC/WETH-500": ("0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", "USDC", "WETH", 500),
        "WBTC/WETH-500": ("0x4585FE77225b41b697C938B018E2Ac67Ac5a20c0", "WBTC", "WETH", 500),
    }


def test_the_mainnet_quoter_is_the_one_uniswap_lists():
    assert dict(QUOTER_V2) == {ETHEREUM_MAINNET: "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"}


def test_every_address_is_a_valid_eip55_checksum():
    # web3 refuses to call an address that is not, so a typo that changed
    # the case of one letter would only surface at the first request.
    addresses = [
        *QUOTER_V2.values(),
        *(token.address for tokens in TOKENS.values() for token in tokens.values()),
        *(pool.address for pools in POOLS.values() for pool in pools.values()),
    ]
    assert len(addresses) == 6
    assert [address for address in addresses if not Web3.is_checksum_address(address)] == []


def test_every_chain_has_all_three_tables_keyed_by_their_own_names():
    assert set(TOKENS) == set(POOLS) == set(QUOTER_V2)
    for chain_id, tokens in TOKENS.items():
        assert all(symbol == token.symbol for symbol, token in tokens.items())
        for key, pool in POOLS[chain_id].items():
            assert key == pool_key(pool)
            # The very objects the token table holds, not look-alikes.
            assert tokens[pool.token0.symbol] is pool.token0
            assert tokens[pool.token1.symbol] is pool.token1


def test_the_tables_are_read_only():
    with pytest.raises(TypeError):
        TOKENS[5] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        TOKENS[ETHEREUM_MAINNET]["DAI"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        POOLS[ETHEREUM_MAINNET]["DAI/WETH-500"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        QUOTER_V2[5] = "0x"  # type: ignore[index]
