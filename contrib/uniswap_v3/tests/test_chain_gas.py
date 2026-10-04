"""The base fee, off a block header."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.chain.errors import MalformedResponse
from contrib.uniswap_v3.chain.gas import ChainGasOracle
from contrib.uniswap_v3.ports import GasOracle
from contrib.uniswap_v3.tests.fakes.rpc import ReplayProvider, answering, block_result, rpc_over
from contrib.uniswap_v3.tests.fixtures import BLOCK, CASSETTE


def test_the_base_fee_of_the_recorded_block():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    oracle = ChainGasOracle(rpc)
    assert isinstance(oracle, GasOracle)
    # About 21.7 gwei.
    assert oracle.base_fee_wei(BLOCK) == 21_721_091_641


def test_a_block_before_london_has_no_base_fee_to_give():
    rpc, _ = rpc_over(answering({"result": block_result(7, 1_500_000_000, base_fee=None)}))
    with pytest.raises(MalformedResponse, match="block 7 has no base fee"):
        ChainGasOracle(rpc).base_fee_wei(7)
