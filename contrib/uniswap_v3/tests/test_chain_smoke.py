"""Against a real node. Not run by CI, and skipped without ``ETH_RPC_URL``.

::

    python -m dotenv run -- pytest -m smoke contrib/uniswap_v3/tests/test_chain_smoke.py

The node must be an archive node: one test reads block 18,000,000.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from contrib.uniswap_v3.chain.blocks import ChainBlockLocator
from contrib.uniswap_v3.chain.gas import ChainGasOracle
from contrib.uniswap_v3.chain.pool_price import read_slot0, read_twap_tick
from contrib.uniswap_v3.chain.quoter import quote_exact_input
from contrib.uniswap_v3.chain.rpc import DEFAULT_URL_ENV, Rpc, connect
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.prices import price_from_sqrt_price_x96, price_from_tick
from contrib.uniswap_v3.tests.fixtures import BAR_TIME, BLOCK

pytestmark = pytest.mark.smoke

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]


@pytest.fixture(scope="module")
def rpc() -> Rpc:
    if not os.environ.get(DEFAULT_URL_ENV):
        pytest.skip(f"{DEFAULT_URL_ENV} is not set")
    return connect(ETHEREUM_MAINNET)


def test_the_current_prices_and_a_quote_agree(rpc, capsys):
    head = rpc.latest_header().number
    usdc_weth = read_slot0(rpc, _USDC_WETH, head)
    wbtc_weth = read_slot0(rpc, _WBTC_WETH, head)
    eth = price_from_sqrt_price_x96(_USDC_WETH, usdc_weth.sqrt_price_x96, base=_WETH)
    btc_in_eth = price_from_sqrt_price_x96(_WBTC_WETH, wbtc_weth.sqrt_price_x96, base=_WBTC)
    twap = price_from_tick(_USDC_WETH, read_twap_tick(rpc, _USDC_WETH, head), base=_WETH)
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1000), block=head)
    two_hops = quote_exact_input(rpc, _USDC, [_USDC_WETH, _WBTC_WETH], Decimal(1000), block=head)
    base_fee = ChainGasOracle(rpc).base_fee_wei(head)
    with capsys.disabled():
        print(
            f"\nblock {head}: ETH {eth:.2f} USDC (30m TWAP {twap:.2f}), BTC {btc_in_eth * eth:.2f} "
            f"USDC, 1000 USDC -> {quote.amount_out} WETH ({quote.gas_estimate} gas) or "
            f"{two_hops.amount_out} WBTC ({two_hops.gas_estimate} gas), "
            f"base fee {base_fee / 10**9:.3f} gwei"
        )

    # Wide enough to hold for years, tight enough to catch an inverted pair
    # or a decimals mistake.
    assert Decimal(100) < eth < Decimal(100_000)
    assert Decimal(1) < btc_in_eth < Decimal(1_000)
    assert abs(twap / eth - 1) < Decimal("0.05")
    # The quote pays the pool's price plus its fee, and little more.
    assert Decimal(0) < Decimal(1000) / quote.amount_out / eth - 1 < Decimal("0.01")
    assert Decimal(0) < Decimal(1000) / two_hops.amount_out / (btc_in_eth * eth) - 1 < Decimal("0.02")
    assert 0 < base_fee < 10**13


def test_a_historical_block_reads_the_same_as_the_recording(rpc):
    state = read_slot0(rpc, _USDC_WETH, BLOCK)
    assert state.sqrt_price_x96 == 1950377547303575102371563899111474
    assert state.tick == 202234
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1000), block=BLOCK)
    assert quote.amount_out == Decimal("0.605704440938728572")
    assert ChainBlockLocator(rpc).first_block_at_or_after(BAR_TIME) == 18_251_965
