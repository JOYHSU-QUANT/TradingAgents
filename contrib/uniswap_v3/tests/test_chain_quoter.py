"""Quotes: the recorded block, what is sent to the quoter, and what is refused."""

from __future__ import annotations

from decimal import Decimal

import pytest
from eth_abi import decode

from contrib.uniswap_v3.chain.errors import (
    InsufficientLiquidity,
    MalformedResponse,
    RpcConfigError,
)
from contrib.uniswap_v3.chain.pool_price import read_slot0
from contrib.uniswap_v3.chain.quoter import Quote, quote_exact_input
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, QUOTER_V2, TOKENS
from contrib.uniswap_v3.domain.prices import (
    MAX_SQRT_RATIO,
    MIN_SQRT_RATIO,
    price_from_sqrt_price_x96,
)
from contrib.uniswap_v3.tests.fakes.rpc import (
    ReplayProvider,
    ScriptedProvider,
    answering,
    encoded,
    rpc_over,
)
from contrib.uniswap_v3.tests.fixtures import BLOCK, CASSETTE

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]

_SINGLE_OUT = ["uint256", "uint160", "uint32", "uint256"]
_PATH_OUT = ["uint256", "uint160[]", "uint32[]", "uint256"]


def _single(amount_out: int, gas: int, *, after: int = 2**96) -> ScriptedProvider:
    return answering({"result": encoded(_SINGLE_OUT, [amount_out, after, 1, gas])})


def _path(amount_out: int, gas: int, *, after: tuple[int, int] = (2**96, 2**96)) -> ScriptedProvider:
    return answering({"result": encoded(_PATH_OUT, [amount_out, list(after), [1, 1], gas])})


def _arguments(provider, types: list[str]) -> tuple:
    """The one call's target, block and decoded arguments (the selector dropped)."""
    [(method, params)] = provider.requests
    assert method == "eth_call"
    data = bytes.fromhex(params[0]["data"][2:])
    return params[0]["to"], params[1], decode(types, data[4:])


def test_one_hop_at_the_recorded_block():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1000), block=BLOCK)
    assert quote == Quote(
        token_in=_USDC,
        token_out=_WETH,
        amount_in=Decimal(1000),
        amount_out=Decimal("0.605704440938728572"),
        gas_estimate=82_348,
        block=BLOCK,
    )
    # Against the pool's own price at that block: 1,000 USDC is small for
    # this pool, so the quote costs the 0.05% fee and almost nothing more.
    spot = price_from_sqrt_price_x96(
        _USDC_WETH, read_slot0(rpc, _USDC_WETH, BLOCK).sqrt_price_x96, base=_WETH
    )
    paid = Decimal(1000) / quote.amount_out
    assert Decimal("0.0005") < paid / spot - 1 < Decimal("0.00051")


def test_two_hops_at_the_recorded_block():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH, _WBTC_WETH], Decimal(1000), block=BLOCK)
    assert quote == Quote(
        token_in=_USDC,
        token_out=_WBTC,
        amount_in=Decimal(1000),
        amount_out=Decimal("0.03834903"),
        gas_estimate=159_412,
        block=BLOCK,
    )


def test_one_hop_sends_the_two_tokens_the_raw_amount_and_the_fee():
    provider = _single(amount_out=1_650_000_000, gas=80_000)
    rpc, _ = rpc_over(provider)
    # WETH is the pool's token1: the route is walked from either side.
    quote = quote_exact_input(rpc, _WETH, [_USDC_WETH], Decimal("1.5"), block=7)
    assert (quote.token_out, quote.amount_out, quote.gas_estimate) == (_USDC, Decimal(1650), 80_000)
    assert str(quote.amount_out) == "1650.000000"

    to, block, (params,) = _arguments(provider, ["(address,address,uint256,uint24,uint160)"])
    assert to == QUOTER_V2[ETHEREUM_MAINNET] and block == hex(7)
    assert params == (_WETH.address.lower(), _USDC.address.lower(), 15 * 10**17, 500, 0)


def test_two_hops_send_the_packed_path():
    provider = _path(amount_out=3_800_000, gas=160_000)
    rpc, _ = rpc_over(provider)
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH, _WBTC_WETH], Decimal("1000.5"), block=7)
    assert (quote.token_out, quote.amount_out) == (_WBTC, Decimal("0.038"))

    _, _, (path, amount_in) = _arguments(provider, ["bytes", "uint256"])
    assert amount_in == 1_000_500_000
    fee = (500).to_bytes(3, "big")
    assert path == b"".join(
        [
            bytes.fromhex(_USDC.address[2:]),
            fee,
            bytes.fromhex(_WETH.address[2:]),
            fee,
            bytes.fromhex(_WBTC.address[2:]),
        ]
    )


def test_a_quote_of_nothing_out_is_an_answer():
    rpc, _ = rpc_over(_single(amount_out=0, gas=70_000))
    quote = quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal("0.000001"), block=7)
    assert quote.amount_out == 0


@pytest.mark.parametrize(
    ("token_in", "route", "message"),
    [
        (_USDC, [], "at least one pool"),
        (_USDC, [_WBTC_WETH], "holding USDC, which that pool does not trade"),
        (_WBTC, [_WBTC_WETH, _WBTC_WETH, _USDC_WETH], "holding WBTC, which that pool does not"),
        (_USDC, ["USDC/WETH-500"], "Pool values"),
        (_USDC, [_USDC_WETH, _USDC_WETH], "ends in USDC, the token it started with"),
    ],
)
def test_a_route_the_token_cannot_walk_is_refused_before_any_request(token_in, route, message):
    provider = _single(1, 1)
    rpc, _ = rpc_over(provider)
    with pytest.raises(ValueError, match=message):
        quote_exact_input(rpc, token_in, route, Decimal(1), block=7)
    assert provider.requests == []


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        (Decimal(0), "above zero"),
        (Decimal("-1"), "non-negative Decimal"),
        (Decimal("0.0000001"), "more decimal places than USDC's 6"),
        (Decimal("NaN"), "finite"),
        (1000, "Decimal"),
        (1000.0, "Decimal"),
    ],
)
def test_an_amount_the_chain_cannot_carry_is_refused_before_any_request(amount, message):
    provider = _single(1, 1)
    rpc, _ = rpc_over(provider)
    with pytest.raises(ValueError, match=message):
        quote_exact_input(rpc, _USDC, [_USDC_WETH], amount, block=7)
    assert provider.requests == []


def test_a_chain_without_a_known_quoter_is_refused():
    provider = _single(1, 1)
    rpc, _ = rpc_over(provider, chain_id=5)
    with pytest.raises(RpcConfigError, match="no QuoterV2 address is known for chain 5"):
        quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1), block=7)
    assert provider.requests == []


@pytest.mark.parametrize("after", [MIN_SQRT_RATIO + 1, MAX_SQRT_RATIO - 1, MIN_SQRT_RATIO])
def test_a_pool_swapped_to_the_end_of_its_range_is_not_a_quote(after):
    rpc, _ = rpc_over(_single(amount_out=5 * 10**17, gas=900_000, after=after))
    with pytest.raises(InsufficientLiquidity, match="USDC/WETH .* ran out of liquidity at block 7"):
        quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1000), block=7)

    # On a path, whichever pool it was.
    rpc, _ = rpc_over(_path(amount_out=3_800_000, gas=900_000, after=(2**96, after)))
    with pytest.raises(InsufficientLiquidity, match="WBTC/WETH .* before 1000 USDC was swapped"):
        quote_exact_input(rpc, _USDC, [_USDC_WETH, _WBTC_WETH], Decimal(1000), block=7)


def test_a_price_one_step_inside_the_range_ends_is_a_quote():
    rpc, _ = rpc_over(_single(amount_out=1, gas=70_000, after=MIN_SQRT_RATIO + 2))
    assert quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1), block=7).gas_estimate == 70_000
    rpc, _ = rpc_over(_single(amount_out=1, gas=70_000, after=MAX_SQRT_RATIO - 2))
    assert quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1), block=7).gas_estimate == 70_000


def test_a_gas_estimate_of_zero_is_refused():
    rpc, _ = rpc_over(_single(amount_out=1, gas=0))
    with pytest.raises(MalformedResponse, match="gas estimate of 0"):
        quote_exact_input(rpc, _USDC, [_USDC_WETH], Decimal(1), block=7)
