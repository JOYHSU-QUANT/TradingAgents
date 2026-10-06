"""The package's decimal helpers: a sum under the one context."""

from __future__ import annotations

from decimal import Decimal

from contrib.uniswap_v3.domain.decimal_context import decimal_sum


def test_decimal_sum_of_nothing_is_zero_and_a_generator_is_consumed():
    assert decimal_sum([]) == 0
    assert decimal_sum(Decimal(n) for n in range(4)) == 6


def test_decimal_sum_rounds_to_the_contexts_28_digits():
    # 1e27 + 1 + 0.4 has 29 significant digits; the context rounds the last off.
    assert decimal_sum([Decimal("1e27"), Decimal(1), Decimal("0.4")]) == Decimal(
        "1000000000000000000000000001"
    )
    # Exact when it fits: the same digits one place lower.
    assert decimal_sum([Decimal("1e26"), Decimal(1), Decimal("0.4")]) == Decimal(
        "100000000000000000000000001.4"
    )
