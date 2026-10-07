"""The package's decimal helpers: a sum under the one context, whatever the ambient one. The shared estimators and the text formats live here too.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from contrib.uniswap_v3.domain.decimal_context import decimal_sum

# 1e27 + 1 + 0.4 has 29 significant digits; the package's context rounds the last off.
WIDE = [Decimal("1e27"), Decimal(1), Decimal("0.4")]
WIDE_SUM = Decimal("1000000000000000000000000001")


def test_decimal_sum_of_nothing_is_zero_and_a_generator_is_consumed():
    assert decimal_sum([]) == 0
    assert decimal_sum(Decimal(n) for n in range(4)) == 6


def test_decimal_sum_rounds_to_the_contexts_28_digits():
    assert decimal_sum(WIDE) == WIDE_SUM
    # Exact when it fits: the same digits one place lower.
    assert decimal_sum([Decimal("1e26"), Decimal(1), Decimal("0.4")]) == Decimal(
        "100000000000000000000000001.4"
    )


@pytest.mark.parametrize("prec", [5, 60])
def test_decimal_sum_ignores_the_ambient_context(prec):
    with localcontext() as ambient:
        ambient.prec = prec
        ambient.rounding = ROUND_DOWN
        assert sum(WIDE, Decimal(0)) != WIDE_SUM  # the ambient context would say otherwise
        assert decimal_sum(WIDE) == WIDE_SUM


def test_the_estimators_refuse_too_few_values():
    from contrib.uniswap_v3.domain.decimal_context import log_returns, mean, sample_volatility

    with pytest.raises(ValueError, match="at least one value"):
        mean([])
    with pytest.raises(ValueError, match="at least two returns, got 1"):
        sample_volatility([Decimal("0.1")], Decimal(1))
    assert log_returns([Decimal("100")]) == []
    assert mean([Decimal("1"), Decimal("3")]) == Decimal("2")
    # Two equal returns: no deviation, whatever the annualiser.
    assert sample_volatility([Decimal("0.1"), Decimal("0.1")], Decimal(19)) == Decimal("0")
