"""The value types refuse what they should, and compute what they say."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from contrib.uniswap_v3.domain.types import (
    Bar,
    Fill,
    Hold,
    MarketView,
    Pool,
    Portfolio,
    Rejection,
    RunMode,
    SwapIntent,
    TargetWeights,
    Token,
    eth_from_wei,
    tokens_along,
)

LOW = "0x" + "0" * 39 + "1"
HIGH = "0x" + "0" * 39 + "2"
POOL = "0x" + "0" * 39 + "3"
A = Token("A", LOW, 18)
B = Token("B", HIGH, 6)
D = Decimal


def test_the_run_modes_are_the_four_the_plan_names():
    assert {mode.value for mode in RunMode} == {"backtest", "paper", "fork", "live"}


@pytest.mark.parametrize(
    ("symbol", "address", "decimals", "match"),
    [
        ("", LOW, 18, "symbol"),
        (None, LOW, 18, "symbol"),
        ("A", "0x1234", 18, "40 hex digits"),
        ("A", LOW[2:], 18, "40 hex digits"),
        ("A", "0x" + "g" * 40, 18, "40 hex digits"),
        ("A", LOW, -1, "decimals"),
        ("A", LOW, True, "decimals"),
        ("A", LOW, 18.0, "decimals"),
        ("A", LOW, 256, "uint8"),
    ],
)
def test_a_malformed_token_is_refused(symbol, address, decimals, match):
    with pytest.raises(ValueError, match=match):
        Token(symbol, address, decimals)


def test_a_pool_states_its_fee_as_a_rate():
    assert Pool(POOL, A, B, 500).fee_rate == D("0.0005")
    assert Pool(POOL, A, B, 3000).fee_rate == D("0.003")


@pytest.mark.parametrize(
    ("address", "token0", "token1", "fee", "match"),
    [
        (POOL, B, A, 500, "token0 must have the smaller address"),
        (POOL, A, A, 500, "token0 must have the smaller address"),
        ("0x12", A, B, 500, "pool address"),
        (POOL, "A", B, 500, "Token values"),
        (POOL, A, B, 0, "pool fee"),
        (POOL, A, B, 1_000_000, "pool fee"),
        (POOL, A, B, True, "pool fee"),
        (POOL, A, B, 500.0, "pool fee"),
    ],
)
def test_a_malformed_pool_is_refused(address, token0, token1, fee, match):
    with pytest.raises(ValueError, match=match):
        Pool(address, token0, token1, fee)


def test_token_order_is_by_address_value_not_by_letter_case():
    upper = Token("U", "0x" + "0" * 39 + "A", 18)
    lower = Token("L", "0x" + "0" * 39 + "b", 18)
    assert Pool(POOL, upper, lower, 500).token0 is upper
    with pytest.raises(ValueError, match="smaller address"):
        Pool(POOL, lower, upper, 500)


def test_target_weights_copy_their_mapping_and_expose_it_read_only():
    source = {"A": D("0.4"), "B": D("0.6")}
    target = TargetWeights(source)
    source["A"] = D("1")
    assert target.weights == {"A": D("0.4"), "B": D("0.6")}
    with pytest.raises(TypeError):
        target.weights["A"] = D("1")  # type: ignore[index]
    assert target == TargetWeights({"B": D("0.6"), "A": D("0.4")})


@pytest.mark.parametrize(
    ("weights", "match"),
    [
        ({}, "at least one token"),
        ({"A": D("0.4"), "B": D("0.5")}, "sum to exactly 1, got about 0.9"),
        ({"A": D("0.6"), "B": D("0.5")}, "sum to exactly 1, got about 1.1"),
        # Off by less than the context's 28 digits can show: still refused.
        ({"A": D("0.5"), "B": D("0.5000000000000000000000000000001")}, "sum to exactly 1"),
        ({"A": D("0.99999999999999999999999999999999")}, "sum to exactly 1"),
        ({"A": D("0.5"), "B": D("0.5"), "C": D("1E-30")}, "sum to exactly 1"),
        ({"A": D("1.5"), "B": D("-0.5")}, "non-negative Decimal"),
        ({"A": D("-0"), "B": D("1")}, "non-negative Decimal"),
        ({"A": D("1E+1000000")}, "from 1e-77 to below 1e78"),
        ({"A": 1}, "Decimal"),
        ({"A": 1.0}, "Decimal"),
        ({"A": D("NaN")}, "finite"),
        ({"A": D("Infinity")}, "finite"),
        ({"": D("1")}, "weights key"),
        ({1: D("1")}, "weights key"),
        ([("A", D("1"))], "mapping"),
    ],
)
def test_malformed_target_weights_are_refused(weights, match):
    with pytest.raises(ValueError, match=match):
        TargetWeights(weights)


def test_hold_values_are_equal():
    assert Hold() == Hold()


def _bar(time: int = 100, **overrides) -> Bar:
    fields = {"time": time, "close_block": 7, "prices": {"A": D("2")}, "base_fee_wei": 10**9}
    return Bar(**{**fields, **overrides})


def test_a_bar_is_not_suspect_unless_told_and_freezes_its_prices():
    bar = _bar()
    assert bar.suspect is False
    assert _bar(suspect=True).suspect is True
    with pytest.raises(TypeError):
        bar.prices["A"] = D("3")  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"time": -1}, "time"),
        ({"time": 1.5}, "time"),
        ({"close_block": -1}, "close_block"),
        ({"base_fee_wei": True}, "base_fee_wei"),
        ({"prices": {}}, "at least one token"),
        ({"prices": {"A": D("0")}}, "positive Decimal"),
        ({"prices": {"A": D("-1")}}, "positive Decimal"),
        ({"prices": {"A": D("1E-9999999")}}, "from 1e-77 to below 1e78"),
        ({"suspect": 1}, "suspect"),
    ],
)
def test_a_malformed_bar_is_refused(overrides, match):
    with pytest.raises(ValueError, match=match):
        _bar(**overrides)


def test_a_market_view_ends_at_its_latest_bar():
    view = MarketView((_bar(100), _bar(200), _bar(300)))
    assert view.latest.time == 300


@pytest.mark.parametrize(
    ("bars", "match"),
    [
        ((), "non-empty tuple"),
        ([_bar(100)], "non-empty tuple"),
        ((_bar(100), "bar"), "Bar values"),
        ((_bar(200), _bar(100)), "strictly increasing"),
        ((_bar(100), _bar(100)), "strictly increasing"),
        ((_bar(100, close_block=9), _bar(200, close_block=8)), "earlier block"),
    ],
)
def test_a_malformed_market_view_is_refused(bars, match):
    with pytest.raises(ValueError, match=match):
        MarketView(bars)


def test_a_portfolio_values_each_balance_in_the_quote_token():
    portfolio = Portfolio(
        quote="USDC",
        balances={"USDC": D("500"), "WETH": D("0.15"), "WBTC": D("0")},
        prices={"WETH": D("2000"), "WBTC": D("50000")},
    )
    assert portfolio.value_of("USDC") == D("500")
    assert portfolio.value_of("WETH") == D("300")
    assert portfolio.value_of("WBTC") == D("0")
    assert portfolio.total_value == D("800")
    with pytest.raises(ValueError, match="does not hold 'DAI'"):
        portfolio.value_of("DAI")


@pytest.mark.parametrize(
    ("quote", "balances", "prices", "match"),
    [
        ("", {"USDC": D("1")}, {}, "quote"),
        ("USDC", {"WETH": D("1")}, {"WETH": D("2000")}, "must include the quote token"),
        ("USDC", {"USDC": D("1"), "WETH": D("1")}, {}, "exactly the non-quote tokens"),
        (
            "USDC",
            {"USDC": D("1"), "WETH": D("1")},
            {"WETH": D("2000"), "USDC": D("1")},
            "exactly the non-quote tokens",
        ),
        ("USDC", {"USDC": D("1")}, {"WETH": D("2000")}, "exactly the non-quote tokens"),
        ("USDC", {"USDC": D("-1")}, {}, "non-negative Decimal"),
        ("USDC", {"USDC": D("-0")}, {}, "non-negative Decimal"),
        ("USDC", {"USDC": D("9E+999999")}, {}, "from 1e-77 to below 1e78"),
        ("USDC", {"USDC": D("1"), "WETH": D("1")}, {"WETH": D("0")}, "positive Decimal"),
        ("USDC", {"USDC": 1}, {}, "Decimal"),
    ],
)
def test_a_malformed_portfolio_is_refused(quote, balances, prices, match):
    with pytest.raises(ValueError, match=match):
        Portfolio(quote, balances, prices)


# A third token above the other two, and the pool that joins it to B: A to C is two hops.
C = Token("C", "0x" + "0" * 39 + "4", 8)
AB = Pool(POOL, A, B, 500)
BC = Pool("0x" + "0" * 39 + "5", B, C, 3000)
# Sells 18-decimal A for 6-decimal B.
SWAP = SwapIntent(A, (AB,), D("100"), D("0.04"))


def test_a_swap_ends_in_the_token_its_route_hands_on_last():
    assert SWAP.token_out == B
    assert SWAP.tokens == (A, B)
    through = SwapIntent(C, (BC, AB), D("1"), D("0"))
    assert through.tokens == (C, B, A)
    assert through.token_out == A
    assert tokens_along(B, [AB]) == (B, A)


def test_a_swap_may_use_every_decimal_place_its_tokens_have():
    swap = SwapIntent(B, (AB,), D("0.000001"), D("0.000000000000000001"))
    assert swap.amount_in == D("0.000001")
    # Trailing zeros are not decimal places a chain would have to carry.
    assert SwapIntent(B, (AB,), D("1.0000000"), D("0")).amount_in == 1


@pytest.mark.parametrize(
    ("token_in", "route", "amount_in", "min_amount_out", "match"),
    [
        ("A", (AB,), D("1"), D("0"), "token_in must be a Token"),
        (A, [AB], D("1"), D("0"), "route must be a tuple"),
        (A, (), D("1"), D("0"), "at least one pool"),
        (A, ("A/B",), D("1"), D("0"), "Pool values"),
        (A, (BC,), D("1"), D("0"), "holding A, which that pool does not trade"),
        (A, (AB, AB), D("1"), D("0"), "passes through A more than once"),
        (A, (AB,), D("0"), D("0"), "amount_in must be a finite, positive Decimal"),
        (A, (AB,), D("-1"), D("0"), "positive Decimal"),
        (A, (AB,), 100, D("0"), "Decimal"),
        (B, (AB,), D("0.0000001"), D("0"), "amount_in of B has more than 6 decimal places"),
        (A, (AB,), D("1"), D("-1"), "min_amount_out must be a finite, non-negative Decimal"),
        (A, (AB,), D("1"), D("0.0000001"), "min_amount_out of B has more than 6 decimal places"),
        (A, (AB,), D("1"), None, "min_amount_out"),
    ],
)
def test_a_malformed_swap_intent_is_refused(token_in, route, amount_in, min_amount_out, match):
    with pytest.raises(ValueError, match=match):
        SwapIntent(token_in, route, amount_in, min_amount_out)


def test_a_fill_may_cost_no_gas_but_must_deliver_something():
    assert Fill(SWAP, D("0.05"), D("0"), 7).gas_cost_eth == 0
    # Exactly the minimum is enough.
    assert Fill(SWAP, D("0.04"), D("0"), 7).amount_out == D("0.04")


@pytest.mark.parametrize(
    ("swap", "amount_out", "gas_cost_eth", "block", "match"),
    [
        ("swap", D("1"), D("0"), 7, "SwapIntent"),
        (SWAP, D("0"), D("0"), 7, "amount_out"),
        (SWAP, D("0.039999"), D("0"), 7, "below the swap's min_amount_out"),
        (SWAP, D("1.0000001"), D("0"), 7, "amount_out of B has more than 6 decimal places"),
        (SWAP, D("1"), D("-0.001"), 7, "gas_cost_eth"),
        (SWAP, D("1"), D("1E-19"), 7, "gas_cost_eth has more than 18 decimal places"),
        (SWAP, D("1"), D("0"), -1, "block"),
        (SWAP, D("1"), D("0"), 7.0, "block"),
    ],
)
def test_a_malformed_fill_is_refused(swap, amount_out, gas_cost_eth, block, match):
    with pytest.raises(ValueError, match=match):
        Fill(swap, amount_out, gas_cost_eth, block)


@pytest.mark.parametrize(("swap", "reason", "match"), [("swap", "why", "SwapIntent"), (SWAP, " ", "reason")])
def test_a_malformed_rejection_is_refused(swap, reason, match):
    with pytest.raises(ValueError, match=match):
        Rejection(swap, reason)


def test_eth_from_wei_is_whole_eth_to_the_last_wei_whatever_the_ambient_context():
    with localcontext() as ambient:
        ambient.prec = 3
        assert str(eth_from_wei(1_234_567_890_123_456_789)) == "1.234567890123456789"
    # Kept at 18 places, as a ledger's gas balance is.
    assert eth_from_wei(1) == Decimal("1E-18")
    assert eth_from_wei(1).as_tuple().exponent == eth_from_wei(0).as_tuple().exponent == -18
    assert eth_from_wei(0) == 0


@pytest.mark.parametrize("wei", [-1, 2**256, 1.0, "1", True])
def test_eth_from_wei_refuses_what_is_not_a_uint256(wei):
    with pytest.raises(ValueError, match="fits a uint256"):
        eth_from_wei(wei)
