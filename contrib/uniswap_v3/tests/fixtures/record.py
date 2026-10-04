"""Re-record ``mainnet.json`` from a real archive node.

::

    python -m contrib.uniswap_v3.tests.fixtures.record

Needs ``ETH_RPC_URL`` in the environment. It runs every read the replayed
tests make, through the package's own functions, and writes what passed over
the wire. The block search starts from the chain's head, so a new recording
visits other blocks than the last one did and the pinned answer stays the
same.
"""

from __future__ import annotations

from decimal import Decimal

from contrib.uniswap_v3.chain.blocks import ChainBlockLocator
from contrib.uniswap_v3.chain.gas import ChainGasOracle
from contrib.uniswap_v3.chain.pool_price import read_slot0, read_twap_tick
from contrib.uniswap_v3.chain.quoter import quote_exact_input
from contrib.uniswap_v3.chain.rpc import Rpc, http_provider
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.tests.fakes.rpc import RecordingProvider
from contrib.uniswap_v3.tests.fixtures import BAR_TIME, BLOCK, CASSETTE


def main() -> None:
    recorder = RecordingProvider(http_provider())
    rpc = Rpc(recorder, ETHEREUM_MAINNET)
    usdc = TOKENS[ETHEREUM_MAINNET]["USDC"]
    usdc_weth = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
    wbtc_weth = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]

    rpc.verify_chain()
    print("block at", BAR_TIME, "=", ChainBlockLocator(rpc).first_block_at_or_after(BAR_TIME))
    for pool in (usdc_weth, wbtc_weth):
        print(read_slot0(rpc, pool, BLOCK), read_twap_tick(rpc, pool, BLOCK))
    print(quote_exact_input(rpc, usdc, [usdc_weth], Decimal(1000), block=BLOCK))
    print(quote_exact_input(rpc, usdc, [usdc_weth, wbtc_weth], Decimal(1000), block=BLOCK))
    print("base fee", ChainGasOracle(rpc).base_fee_wei(BLOCK))
    recorder.write(CASSETTE)


if __name__ == "__main__":
    main()
