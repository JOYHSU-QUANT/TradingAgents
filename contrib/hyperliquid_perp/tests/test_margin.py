"""Tests for the maintenance-margin tier model and its mapper extraction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    MalformedResponseError,
    UnknownCoinError,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.mapper import map_margin_schedule


def _multi_tier() -> MarginSchedule:
    # rates: 0.01, 0.02, 0.05 ; deductions: 0, 100, 1600 (continuity).
    return MarginSchedule(
        tiers=(
            MarginTier(Decimal(0), Decimal(50)),
            MarginTier(Decimal(10_000), Decimal(25)),
            MarginTier(Decimal(50_000), Decimal(10)),
        )
    )


def test_single_tier_rate_and_zero_deduction():
    sched = MarginSchedule(tiers=(MarginTier(Decimal(0), Decimal(50)),))
    assert sched.maintenance_margin_rate(Decimal(1000)) == Decimal("0.01")
    assert sched.maintenance_deduction(Decimal(1000)) == Decimal(0)
    assert sched.maintenance_margin(Decimal(3000)) == Decimal("30")


def test_multi_tier_deductions_are_derived_for_continuity():
    sched = _multi_tier()
    got = [sched.maintenance_deduction(n) for n in (Decimal(0), Decimal(10_000), Decimal(50_000))]
    assert got == [Decimal(0), Decimal(100), Decimal(1600)]


def test_maintenance_margin_is_continuous_across_boundaries():
    sched = _multi_tier()
    # Evaluated exactly at each boundary the two adjacent tiers must agree.
    assert sched.maintenance_margin(Decimal(10_000)) == Decimal(100)  # 10000*0.01
    assert sched.maintenance_margin(Decimal(50_000)) == Decimal(900)  # 50000*0.02 - 100


def test_tier_selection_by_notional():
    sched = _multi_tier()
    assert sched.tier_for_notional(Decimal(9_999)).max_leverage == Decimal(50)
    assert sched.tier_for_notional(Decimal(10_000)).max_leverage == Decimal(25)  # boundary -> upper
    assert sched.tier_for_notional(Decimal(60_000)).max_leverage == Decimal(10)


def test_first_tier_must_start_at_zero():
    with pytest.raises(ValueError, match="first margin tier must start at lower_bound 0"):
        MarginSchedule(tiers=(MarginTier(Decimal(100), Decimal(50)),))


def test_tiers_must_ascend_and_not_raise_leverage():
    with pytest.raises(ValueError, match="strictly ascending"):
        MarginSchedule(
            tiers=(MarginTier(Decimal(0), Decimal(50)), MarginTier(Decimal(0), Decimal(25)))
        )
    with pytest.raises(ValueError, match="must not raise max_leverage"):
        MarginSchedule(
            tiers=(MarginTier(Decimal(0), Decimal(25)), MarginTier(Decimal(100), Decimal(50)))
        )


def test_empty_schedule_rejected():
    with pytest.raises(ValueError, match="at least one tier"):
        MarginSchedule(tiers=())


def test_tier_validation():
    with pytest.raises(ValueError, match="max_leverage must be > 0"):
        MarginTier(Decimal(0), Decimal(0))
    with pytest.raises(ValueError, match="lower_bound must be >= 0"):
        MarginTier(Decimal(-1), Decimal(50))


# --------------------------------------------------------------------------
# mapper: map_margin_schedule
# --------------------------------------------------------------------------


def test_map_schedule_falls_back_to_max_leverage(meta_and_asset_ctxs):
    sched = map_margin_schedule(meta_and_asset_ctxs, "BTC")
    assert len(sched.tiers) == 1
    assert sched.tiers[0].max_leverage == Decimal(50)
    assert sched.maintenance_margin_rate(Decimal(1000)) == Decimal("0.01")


def test_map_schedule_uses_explicit_margin_table():
    meta = {
        "universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "marginTableId": 7}],
        "marginTables": [
            [
                7,
                {
                    "marginTiers": [
                        {"lowerBound": "0", "maxLeverage": 50},
                        {"lowerBound": "10000", "maxLeverage": 25},
                    ]
                },
            ]
        ],
    }
    sched = map_margin_schedule([meta, []], "BTC")
    assert len(sched.tiers) == 2
    assert sched.tiers[1].lower_bound == Decimal(10_000)
    assert sched.tiers[1].max_leverage == Decimal(25)


def test_map_schedule_unresolvable_table_id_raises():
    # marginTables is present but this asset's id is missing -> fail loud rather
    # than silently under-state margin with the maxLeverage single-tier fallback.
    meta = {
        "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 99}],
        "marginTables": [[7, {"marginTiers": [{"lowerBound": "0", "maxLeverage": 25}]}]],
    }
    with pytest.raises(MalformedResponseError, match="not found in marginTables"):
        map_margin_schedule([meta, []], "BTC")


def test_margin_math_is_immune_to_ambient_decimal_context():
    # Tier rate/deduction/margin feed the §12.2 reproducibility snapshot fields;
    # a consumer shrinking the global precision must not perturb them.
    import decimal

    tiers = (MarginTier(Decimal(0), Decimal(7)), MarginTier(Decimal(10_000), Decimal(3)))
    baseline = MarginSchedule(tiers=tiers)
    base_rate = baseline.maintenance_margin_rate(Decimal("20000"))
    base_margin = baseline.maintenance_margin(Decimal("20000"))
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 4
        perturbed = MarginSchedule(tiers=tiers)  # deductions baked at construction
        assert perturbed.maintenance_margin_rate(Decimal("20000")) == base_rate
        assert perturbed.maintenance_margin(Decimal("20000")) == base_margin
        assert perturbed._deductions == baseline._deductions
    finally:
        decimal.getcontext().prec = original


def test_map_schedule_corrupt_tier_value_raises():
    # A present, non-empty marginTiers list whose entry carries a bad value
    # (non-positive maxLeverage) must fail loud with an indexed message, not
    # silently drop the tier and under-state maintenance margin.
    meta = {
        "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 7}],
        "marginTables": [
            [
                7,
                {
                    "marginTiers": [
                        {"lowerBound": "0", "maxLeverage": 50},
                        {"lowerBound": "10000", "maxLeverage": 0},  # corrupt
                    ]
                },
            ]
        ],
    }
    with pytest.raises(MalformedResponseError, match=r"marginTiers\[1\]"):
        map_margin_schedule([meta, []], "BTC")


def test_map_schedule_non_dict_tier_raises():
    # A non-dict item inside an otherwise-present marginTiers list is corrupt
    # exchange data — fail loud with the tier index, don't coerce or skip it.
    meta = {
        "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 7}],
        "marginTables": [[7, {"marginTiers": [{"lowerBound": "0", "maxLeverage": 50}, "garbage"]}]],
    }
    with pytest.raises(MalformedResponseError, match=r"marginTiers\[1\] is str"):
        map_margin_schedule([meta, []], "BTC")


def test_map_schedule_resolved_table_with_unusable_tiers_raises():
    # The table id resolves but its marginTiers are empty -> fail loud; a silent
    # fallback to the single maxLeverage tier would under-state maintenance
    # margin (non-conservative) for the asset.
    meta = {
        "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 7}],
        "marginTables": [[7, {"marginTiers": []}]],
    }
    with pytest.raises(MalformedResponseError, match="has no usable marginTiers"):
        map_margin_schedule([meta, []], "BTC")


def test_map_schedule_no_table_id_falls_back():
    # No marginTableId at all -> the single-tier maxLeverage fallback is correct.
    meta = {"universe": [{"name": "BTC", "maxLeverage": 40}]}
    sched = map_margin_schedule([meta, []], "BTC")
    assert len(sched.tiers) == 1
    assert sched.tiers[0].max_leverage == Decimal(40)


def test_map_schedule_table_id_without_tables_raises():
    # The entry references a tier table but meta carries no marginTables list
    # (absent, or a non-list shape) -> the id is unresolvable; silently falling
    # back to the single maxLeverage tier would under-state maintenance margin.
    entry = {"name": "BTC", "maxLeverage": 40, "marginTableId": 7}
    with pytest.raises(MalformedResponseError, match="no marginTables list"):
        map_margin_schedule([{"universe": [entry]}, []], "BTC")
    with pytest.raises(MalformedResponseError, match="no marginTables list"):
        map_margin_schedule([{"universe": [entry], "marginTables": {"7": {}}}, []], "BTC")


def test_tier_details_matches_individual_lookups():
    sched = _multi_tier()
    for n in (Decimal(0), Decimal(9_999), Decimal(10_000), Decimal(60_000)):
        index, tier, deduction, margin = sched.tier_details(n)
        assert tier is sched.tier_for_notional(n)
        assert sched.tiers[index] is tier
        assert deduction == sched.maintenance_deduction(n)
        assert margin == sched.maintenance_margin(n)


def test_map_schedule_unknown_coin(meta_and_asset_ctxs):
    with pytest.raises(UnknownCoinError):
        map_margin_schedule(meta_and_asset_ctxs, "DOGE")


def test_map_schedule_bad_shape():
    with pytest.raises(MalformedResponseError):
        map_margin_schedule([], "BTC")
