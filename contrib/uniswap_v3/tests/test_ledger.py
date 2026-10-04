"""The ledger: balances that move only by whole sets of fills, and gas from its own pot."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.decimal_context import floor_to_places, plain
from contrib.uniswap_v3.domain.ledger import Ledger, LedgerError
from contrib.uniswap_v3.domain.types import Fill, SwapIntent
from contrib.uniswap_v3.tests.fakes.engine import (
    USDC,
    USDC_WETH,
    WBTC,
    WBTC_WETH,
    WETH,
    ledger as _ledger,
)

D = Decimal


def _fill(token_in, route, amount_in: str, amount_out: str, gas: str = "0.001") -> Fill:
    return Fill(SwapIntent(token_in, route, D(amount_in), D(0)), D(amount_out), D(gas), 7)


def test_a_fill_moves_two_balances_and_takes_its_gas_from_the_gas_balance():
    after = _ledger().apply([_fill(USDC, (USDC_WETH,), "3000", "1.49")])
    assert after.balances == {"USDC": D("7000"), "WETH": D("1.49"), "WBTC": D("0")}
    assert after.gas_eth == D("0.999")


def test_a_two_hop_fill_leaves_the_token_it_passes_through_alone():
    after = _ledger(weth="2").apply([_fill(USDC, (USDC_WETH, WBTC_WETH), "2000", "0.0499")])
    assert after.balances == {"USDC": D("8000"), "WETH": D("2"), "WBTC": D("0.0499")}


def test_gas_is_not_paid_from_weth():
    ledger = _ledger(weth="5", gas="0.0005")
    with pytest.raises(LedgerError, match="the gas balance of 0.0005 ETH does not cover"):
        ledger.apply([_fill(USDC, (USDC_WETH,), "100", "0.05")])


def test_fills_are_applied_together_or_not_at_all():
    ledger = _ledger(gas="0.0015")
    fills = [
        _fill(USDC, (USDC_WETH,), "3000", "1.49"),
        _fill(USDC, (USDC_WETH, WBTC_WETH), "2000", "0.0499"),
    ]
    # The second fill's gas is what the balance no longer covers.
    with pytest.raises(LedgerError, match="USDC for WBTC"):
        ledger.apply(fills)
    assert ledger == _ledger(gas="0.0015")
    assert _ledger(gas="0.002").apply(fills).gas_eth == 0


def test_a_fill_the_balance_does_not_cover_is_refused():
    with pytest.raises(LedgerError, match="holds 10000 USDC, less than the 10000.000001"):
        _ledger().apply([_fill(USDC, (USDC_WETH,), "10000.000001", "5")])
    # The whole balance may go.
    assert _ledger().apply([_fill(USDC, (USDC_WETH,), "10000", "5")]).balances["USDC"] == 0


def test_a_fill_in_a_token_the_ledger_does_not_name_is_refused():
    ledger = Ledger(balances={"USDC": D("100"), "WETH": D("0")}, gas_eth=D("1"))
    with pytest.raises(LedgerError, match="does not hold WBTC"):
        ledger.apply([_fill(USDC, (USDC_WETH, WBTC_WETH), "100", "0.002")])


def test_a_balance_longer_than_the_money_context_is_not_rounded():
    # 31 significant digits: the 28-digit context would drop the last three.
    ledger = _ledger(weth="1234567890123.123456789012345678")
    after = ledger.apply([_fill(WETH, (USDC_WETH,), "0.000000000000000001", "0.000001")])
    assert after.balances["WETH"] == D("1234567890123.123456789012345677")


def test_a_ledger_is_valued_without_its_gas_balance():
    portfolio = _ledger("500", "0.15", gas="3").portfolio(
        "USDC", {"WETH": D("2000"), "WBTC": D("50000")}
    )
    assert portfolio.total_value == D("800")


def test_a_ledger_cannot_be_changed_from_outside():
    balances = {"USDC": D("1")}
    ledger = Ledger(balances=balances, gas_eth=D("0"))
    balances["USDC"] = D("2")
    assert ledger.balances["USDC"] == 1
    with pytest.raises(TypeError):
        ledger.balances["USDC"] = D("2")  # type: ignore[index]


@pytest.mark.parametrize(
    ("balances", "gas_eth", "match"),
    [
        ({}, D("1"), "balances must map"),
        (["USDC"], D("1"), "balances must map"),
        ({"": D("1")}, D("1"), "token symbol"),
        ({"USDC": D("-1")}, D("1"), r"balances\['USDC'\]"),
        ({"USDC": 1}, D("1"), r"balances\['USDC'\]"),
        ({"USDC": D("1E+90")}, D("1"), r"balances\['USDC'\]"),
        ({"USDC": D("1")}, D("-0.1"), "gas_eth"),
        ({"USDC": D("1")}, D("NaN"), "gas_eth"),
    ],
)
def test_a_malformed_ledger_is_refused(balances, gas_eth, match):
    with pytest.raises(ValueError, match=match):
        Ledger(balances=balances, gas_eth=gas_eth)


def test_a_cut_never_rounds_up_and_a_printed_amount_carries_no_padding():
    assert floor_to_places(D("1.9999999"), 6) == D("1.999999")
    assert floor_to_places(D("0.0000009"), 6) == 0
    assert floor_to_places(D("5"), 2).as_tuple().exponent == -2
    # More digits than the money context holds are cut, not rounded.
    assert floor_to_places(D("1234567890123.1234567890123456789"), 18) == D(
        "1234567890123.123456789012345678"
    )
    assert plain(D("0.001500000000000000")) == "0.0015"
    assert plain(D("0E-8")) == "0"
    assert plain(D("1E+2")) == "100"


def test_wbtc_can_be_sold_as_well():
    after = _ledger("0", "0", "0.5").apply([_fill(WBTC, (WBTC_WETH,), "0.5", "10")])
    assert after.balances == {"USDC": D("0"), "WETH": D("10"), "WBTC": D("0")}
