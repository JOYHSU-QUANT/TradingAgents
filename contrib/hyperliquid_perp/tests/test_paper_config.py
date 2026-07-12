"""Tests for the typed ``paper_trading:`` config block (phase2-execution §5.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.config import load_config
from contrib.hyperliquid_perp.paper.config import InitialPosition, PaperTradingConfig


def test_defaults_match_spec():
    cfg = PaperTradingConfig.from_dict(None)
    assert cfg.account.initial_balance_usdc == Decimal("1000")
    assert cfg.account.initial_positions == ()
    assert cfg.execution.taker_fee_rate == Decimal("0.00045")
    assert cfg.execution.min_notional_usdc == Decimal("10")
    assert cfg.execution.market_monitor.interval_seconds == 30
    assert cfg.execution.market_monitor.request_timeout_seconds == 5
    assert cfg.execution.fill_model.slippage_bps == Decimal("5")


def test_overrides_are_parsed():
    cfg = PaperTradingConfig.from_dict(
        {
            "account": {
                "initial_balance_usdc": 5000,
                "initial_positions": [{"coin": "BTC", "size": "0.01", "entry_price": 60000}],
            },
            "execution": {
                "taker_fee_rate": "0.0003",
                "market_monitor": {"interval_seconds": 15},
                "fill_model": {"slippage_bps": 8},
            },
        }
    )
    assert cfg.account.initial_balance_usdc == Decimal("5000")
    assert cfg.account.initial_positions == (
        InitialPosition(coin="BTC", size=Decimal("0.01"), entry_price=Decimal("60000")),
    )
    assert cfg.execution.taker_fee_rate == Decimal("0.0003")
    assert cfg.execution.market_monitor.interval_seconds == 15
    assert cfg.execution.market_monitor.request_timeout_seconds == 5  # default kept
    assert cfg.execution.fill_model.slippage_bps == Decimal("8")


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown config key"):
        PaperTradingConfig.from_dict({"acount": {}})
    with pytest.raises(ValueError, match="unknown config key"):
        PaperTradingConfig.from_dict({"execution": {"taker_fee": 0.001}})


def test_invalid_values_rejected():
    with pytest.raises(ValueError, match="initial_balance_usdc must be > 0"):
        PaperTradingConfig.from_dict({"account": {"initial_balance_usdc": 0}})
    with pytest.raises(ValueError, match="taker_fee_rate must be >= 0"):
        PaperTradingConfig.from_dict({"execution": {"taker_fee_rate": -0.1}})
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        PaperTradingConfig.from_dict({"execution": {"market_monitor": {"interval_seconds": 0}}})


def test_market_monitor_interval_rejects_above_twap_slice_cadence():
    # The TWAP executor fills at most one slice per tick on a 30s slice grid
    # with a 1h plan deadline — an interval above the slice cadence would
    # silently under-fill full-size plans, so config rejects it loudly.
    with pytest.raises(ValueError, match="interval_seconds must be <= 30"):
        PaperTradingConfig.from_dict({"execution": {"market_monitor": {"interval_seconds": 31}}})
    # The boundary itself (== the slice cadence) stays legal.
    cfg = PaperTradingConfig.from_dict({"execution": {"market_monitor": {"interval_seconds": 30}}})
    assert cfg.execution.market_monitor.interval_seconds == 30


def test_initial_position_validation():
    with pytest.raises(ValueError, match="size' must be non-zero"):
        InitialPosition.from_dict({"coin": "BTC", "size": 0, "entry_price": 60000})
    with pytest.raises(ValueError, match="entry_price' must be > 0"):
        InitialPosition.from_dict({"coin": "BTC", "size": "0.01", "entry_price": 0})
    with pytest.raises(ValueError, match="missing key"):
        InitialPosition.from_dict({"coin": "BTC", "size": "0.01"})
    with pytest.raises(ValueError, match="unknown initial position key"):
        InitialPosition.from_dict({"coin": "BTC", "size": "0.01", "entry_price": 1, "x": 1})
    # str() would render YAML `coin: true` as the seed symbol "True" — the
    # non-empty check is an open pattern, so the type itself must fail loud.
    with pytest.raises(ValueError, match="expected a string"):
        InitialPosition.from_dict({"coin": True, "size": "0.01", "entry_price": 60000})


def test_duplicate_initial_position_coin_rejected():
    # A copy-paste duplicate must fail as a named config error here, not as an
    # opaque run_seed_positions PRIMARY KEY violation at run creation.
    with pytest.raises(ValueError, match="duplicate initial position for coin 'BTC'"):
        PaperTradingConfig.from_dict(
            {
                "account": {
                    "initial_positions": [
                        {"coin": "BTC", "size": "0.01", "entry_price": 60000},
                        {"coin": "BTC", "size": "0.02", "entry_price": 61000},
                    ]
                }
            }
        )


def test_non_mapping_block_rejected():
    with pytest.raises(ValueError, match="expected a mapping"):
        PaperTradingConfig.from_dict({"account": 5})


def test_example_yaml_paper_block_parses():
    # The committed example config must round-trip through the typed parser.
    raw = load_config()
    cfg = PaperTradingConfig.from_dict(raw.get("paper_trading"))
    assert cfg.execution.taker_fee_rate == Decimal("0.00045")
    assert cfg.account.initial_balance_usdc == Decimal("1000")
