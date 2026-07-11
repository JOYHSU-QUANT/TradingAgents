"""Tests for the ``live:`` config block (phase3-spec §3–§5).

The validation matrix: legal §4 configs round-trip into typed values; every
spec-defined contradiction (mainnet_live, mode/network mismatch, ceiling
violation, gate contradictions) is a named ValueError at construction; and the
§5 cap math matches the spec's worked example.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from contrib.hyperliquid_perp.config import load_config
from contrib.hyperliquid_perp.domains.perp.config_coercion import bool_from_yaml
from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskConfig
from contrib.hyperliquid_perp.live.config import (
    EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
    ExecutionMode,
    ExecutionStyle,
    LiveConfig,
    LiveSafetyConfig,
    NotionalCaps,
    RefreshFailedPolicy,
    ShutdownPolicy,
    TpFailureMode,
    compute_notional_caps,
    validate_live_risk_consistency,
)


def _live_block(**overrides) -> dict:
    """A minimal legal testnet_live block; overrides merge shallowly."""
    block = {
        "mode": "testnet_live",
        "network": "testnet",
        "allow_real_orders": False,
    }
    block.update(overrides)
    return block


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


def test_defaults_are_the_safest_state():
    # mode and network are required; every other default is the safest
    # expressible state.
    cfg = LiveConfig.from_dict({"mode": "paper", "network": "testnet"})
    assert cfg.mode is ExecutionMode.PAPER
    assert cfg.allow_real_orders is False
    assert cfg.allow_manage_external_orders is False
    assert cfg.require_agent_wallet is True
    assert cfg.kill_switch.enabled is True


@pytest.mark.parametrize("block", [None, {}, {"network": "testnet"}, {"mode": None}])
def test_missing_mode_is_a_named_error(block):
    # A default the live subcommand always rejects would just be a worse error
    # message — an absent (or blank) mode must say "required" instead.
    with pytest.raises(ValueError, match="live.mode is required"):
        LiveConfig.from_dict(block)


@pytest.mark.parametrize("block", [{"mode": "testnet_live"}, {"mode": "paper", "network": None}])
def test_missing_network_is_a_named_error(block):
    # A guessed network would blame the operator for a value they never wrote
    # when it contradicts the mode's §3.1 pin — required, like mode.
    with pytest.raises(ValueError, match="live.network is required"):
        LiveConfig.from_dict(block)


def test_full_spec_example_round_trips():
    # The §4 example block, verbatim keys — every sub-block parses typed.
    cfg = LiveConfig.from_dict(
        {
            "mode": "testnet_live",
            "network": "testnet",
            "allow_real_orders": False,
            "allow_manage_external_orders": False,
            "order_owner_prefix": "hta",
            "require_agent_wallet": True,
            "safety": {
                "single_symbol_only": True,
                "allowed_symbols": ["BTC"],
                "leverage": 1,
                "margin_mode": "cross",
                "max_target_margin_pct": 60,
                "max_notional_usdc": 100,
                "absolute_notional_ceiling": 500,
                "max_open_orders": 5,
                "max_daily_loss_pct": 2,
                "max_consecutive_loss_count": 3,
            },
            "execution": {
                "default_style": "sliced_twap",
                "max_slippage_pct": 0.005,
                "plan_duration_minutes": 60,
                "slice_interval_seconds": 30,
            },
            "websocket": {"disconnect_safe_mode_after_seconds": 300},
            "protection": {
                "sl_repair_max_attempts": 3,
                "sl_repair_retry_delay_seconds": 5,
                "tp_failure_mode": "degraded_protection",
            },
            "kill_switch": {
                "enabled": True,
                "schedule_cancel_seconds": 120,
                "refresh_interval_seconds": 30,
                "on_refresh_failed": "safe_mode",
                "on_shutdown": "cancel_bot_owned_open_orders",
                "emergency_close_on_shutdown": False,
            },
        }
    )
    assert cfg.mode is ExecutionMode.TESTNET_LIVE
    assert cfg.safety.max_notional_usdc == D(100)
    assert cfg.safety.absolute_notional_ceiling == D(500)
    assert cfg.execution.max_slippage_pct == D("0.005")
    assert cfg.kill_switch.schedule_cancel_seconds == 120
    # The single-member policy vocabularies normalise to enums so PR 2/5
    # dispatch sites get exhaustiveness protection.
    assert cfg.execution.default_style is ExecutionStyle.SLICED_TWAP
    assert cfg.protection.tp_failure_mode is TpFailureMode.DEGRADED_PROTECTION
    assert cfg.kill_switch.on_refresh_failed is RefreshFailedPolicy.SAFE_MODE
    assert cfg.kill_switch.on_shutdown is ShutdownPolicy.CANCEL_BOT_OWNED_OPEN_ORDERS


def test_mainnet_tiny_requires_and_accepts_mainnet():
    cfg = LiveConfig.from_dict(_live_block(mode="mainnet_tiny", network="mainnet"))
    assert cfg.mode is ExecutionMode.MAINNET_TINY


def test_symbols_are_uppercased():
    cfg = LiveConfig.from_dict(_live_block(safety={"allowed_symbols": ["btc"]}))
    assert cfg.safety.allowed_symbols == ("BTC",)


def test_network_is_normalised():
    cfg = LiveConfig.from_dict(_live_block(network="  TESTNET  "))
    assert cfg.network == "testnet"


# ---------------------------------------------------------------------------
# mode / network / gate contradictions
# ---------------------------------------------------------------------------


def test_mainnet_live_is_rejected():
    # §22: defined in the vocabulary, disabled in Phase 3 v1 — even asking for
    # it must refuse startup, not silently run.
    with pytest.raises(ValueError, match="mainnet_live.*not enabled"):
        LiveConfig.from_dict(_live_block(mode="mainnet_live", network="mainnet"))


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="live.mode must be one of"):
        LiveConfig.from_dict(_live_block(mode="live"))


def test_unknown_mode_message_does_not_advertise_mainnet_live():
    # The typo message must not send the operator to a value the very next
    # check hard-rejects — mainnet_live appears only with its "not enabled".
    with pytest.raises(ValueError, match="not enabled") as excinfo:
        LiveConfig.from_dict(_live_block(mode="testnet"))
    assert "paper, testnet_live, mainnet_tiny" in str(excinfo.value)


@pytest.mark.parametrize(
    ("safety", "match"),
    [
        ({"max_notional_usdc": 101}, "mainnet_tiny cap"),
        ({"max_target_margin_pct": 61}, "mainnet_tiny cap"),
    ],
)
def test_mainnet_tiny_looser_than_spec_caps_is_rejected(safety, match):
    # §21.1/§24.2: the hard config gate — mainnet_tiny with non-tiny caps is a
    # named startup failure, not an operator promise.
    with pytest.raises(ValueError, match=match):
        LiveConfig.from_dict(_live_block(mode="mainnet_tiny", network="mainnet", safety=safety))


def test_mainnet_tiny_spec_caps_and_tighter_are_accepted():
    cfg = LiveConfig.from_dict(
        _live_block(
            mode="mainnet_tiny",
            network="mainnet",
            safety={"max_notional_usdc": 100, "max_target_margin_pct": 60},
        )
    )
    assert cfg.mode is ExecutionMode.MAINNET_TINY
    tighter = LiveConfig.from_dict(
        _live_block(
            mode="mainnet_tiny",
            network="mainnet",
            safety={"max_notional_usdc": 50, "max_target_margin_pct": 30},
        )
    )
    assert tighter.safety.max_notional_usdc == D(50)


def test_testnet_live_is_not_bound_by_mainnet_tiny_caps():
    # The §21.1 caps define mainnet_tiny; testnet drills may size differently.
    cfg = LiveConfig.from_dict(
        _live_block(safety={"max_notional_usdc": 450, "max_target_margin_pct": 100})
    )
    assert cfg.safety.max_notional_usdc == D(450)


@pytest.mark.parametrize(
    ("mode", "network"),
    [("testnet_live", "mainnet"), ("mainnet_tiny", "testnet")],
)
def test_mode_network_mismatch_is_rejected(mode, network):
    # §3.1: each live mode is pinned to its network — the classic
    # "wrong network by accident" must die at config load.
    with pytest.raises(ValueError, match="requires live.network"):
        LiveConfig.from_dict(_live_block(mode=mode, network=network))


def test_unknown_network_is_rejected():
    with pytest.raises(ValueError, match="live.network must be one of"):
        LiveConfig.from_dict(_live_block(network="prod"))


def test_paper_mode_with_real_orders_is_rejected():
    with pytest.raises(ValueError, match="paper"):
        LiveConfig.from_dict(_live_block(mode="paper", allow_real_orders=True))


def test_manage_external_orders_is_rejected():
    # §25 #8: bot-owned orders only in v1.
    with pytest.raises(ValueError, match="allow_manage_external_orders"):
        LiveConfig.from_dict(_live_block(allow_manage_external_orders=True))


def test_real_orders_with_kill_switch_disabled_is_rejected():
    # §4.1 lists "kill switch active" as a real-order precondition; enabled
    # orders + disabled switch is a config that could never trade — fail loud.
    with pytest.raises(ValueError, match="kill_switch"):
        LiveConfig.from_dict(_live_block(allow_real_orders=True, kill_switch={"enabled": False}))


def test_real_orders_with_kill_switch_enabled_is_accepted():
    cfg = LiveConfig.from_dict(_live_block(allow_real_orders=True))
    assert cfg.allow_real_orders is True


@pytest.mark.parametrize("prefix", ["", "with_underscore", "spa ce", "x" * 17])
def test_bad_order_owner_prefix_is_rejected(prefix):
    # The prefix becomes a "_"-separated cloid_logical segment (§8.2).
    with pytest.raises(ValueError, match="order_owner_prefix"):
        LiveConfig.from_dict(_live_block(order_owner_prefix=prefix))


def test_unknown_live_key_is_rejected():
    with pytest.raises(ValueError, match="unknown config key"):
        LiveConfig.from_dict(_live_block(allow_reel_orders=True))


def test_gate_bools_are_strict():
    # A quoted "false" in YAML is a truthy string; for allow_real_orders that
    # inversion is real money — anything non-bool must fail.
    with pytest.raises(ValueError, match="true/false"):
        LiveConfig.from_dict(_live_block(allow_real_orders="false"))


def test_bool_from_yaml_accepts_only_bools():
    assert bool_from_yaml(True) is True
    assert bool_from_yaml(False) is False
    for bad in ("true", 1, 0, None, []):
        with pytest.raises(ValueError, match="true/false"):
            bool_from_yaml(bad)


# ---------------------------------------------------------------------------
# safety block: the §5 ceiling check and hard limits
# ---------------------------------------------------------------------------


def test_max_notional_below_exchange_minimum_is_rejected():
    # §5 rule 4's config-only slice: effective_notional_cap <= max_notional_usdc
    # for ANY equity, so a max below the exchange minimum can never trade —
    # named at construction, not after three network calls.
    with pytest.raises(ValueError, match="exchange minimum order value"):
        LiveSafetyConfig.from_dict({"max_notional_usdc": 9})


def test_max_notional_equal_to_exchange_minimum_is_allowed():
    safety = LiveSafetyConfig.from_dict({"max_notional_usdc": 10})
    assert safety.max_notional_usdc == EXCHANGE_MIN_ORDER_NOTIONAL_USDC


def test_notional_above_ceiling_is_rejected_not_clamped():
    # §5 rule 5: startup fail, never clamp.
    with pytest.raises(ValueError, match="absolute_notional_ceiling"):
        LiveSafetyConfig.from_dict({"max_notional_usdc": 501, "absolute_notional_ceiling": 500})


def test_notional_equal_to_ceiling_is_allowed():
    safety = LiveSafetyConfig.from_dict(
        {"max_notional_usdc": 500, "absolute_notional_ceiling": 500}
    )
    assert safety.max_notional_usdc == D(500)


def test_leverage_other_than_one_is_rejected():
    # §25 #6: leverage > 1 is out of scope — live money never runs unvalidated math.
    with pytest.raises(ValueError, match="1x only"):
        LiveSafetyConfig.from_dict({"leverage": 2})


def test_isolated_margin_is_rejected():
    with pytest.raises(ValueError, match="cross"):
        LiveSafetyConfig.from_dict({"margin_mode": "isolated"})


def test_single_symbol_only_with_two_symbols_is_rejected():
    with pytest.raises(ValueError, match="single_symbol_only"):
        LiveSafetyConfig.from_dict({"allowed_symbols": ["BTC", "ETH"]})


def test_single_symbol_only_false_is_rejected():
    # §25 #4: multi-symbol portfolio execution is out of scope for v1 — the
    # switch gets the same hard treatment as leverage>1/isolated, so a
    # 2-symbol config cannot sail through PR 1 into a single-symbol PR 5.
    with pytest.raises(ValueError, match="single_symbol_only must be true"):
        LiveSafetyConfig.from_dict({"single_symbol_only": False, "allowed_symbols": ["BTC", "ETH"]})


def test_duplicate_symbols_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        LiveSafetyConfig.from_dict({"allowed_symbols": ["BTC", "btc"]})


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_target_margin_pct": 0},
        {"max_target_margin_pct": 101},
        {"max_notional_usdc": 0},
        {"absolute_notional_ceiling": 0},
        {"max_open_orders": 0},
        {"max_daily_loss_pct": 0},
        {"max_daily_loss_pct": 101},
        {"max_consecutive_loss_count": 0},
        {"allowed_symbols": []},
    ],
)
def test_out_of_range_safety_values_are_rejected(overrides):
    with pytest.raises(ValueError):
        LiveSafetyConfig.from_dict(overrides)


# ---------------------------------------------------------------------------
# execution / websocket / protection / kill_switch sub-blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "overrides", "match"),
    [
        ("execution", {"default_style": "native_twap"}, "sliced_twap"),
        ("execution", {"max_slippage_pct": 0}, "fraction"),
        ("execution", {"max_slippage_pct": 1}, "fraction"),
        ("execution", {"plan_duration_minutes": 0}, "plan_duration_minutes"),
        ("execution", {"slice_interval_seconds": 0}, "slice_interval_seconds"),
        (
            "execution",
            {"plan_duration_minutes": 1, "slice_interval_seconds": 61},
            "no slice would ever fire",
        ),
        ("websocket", {"disconnect_safe_mode_after_seconds": 0}, "disconnect"),
        ("protection", {"sl_repair_max_attempts": 0}, "sl_repair_max_attempts"),
        ("protection", {"tp_failure_mode": "abort"}, "degraded_protection"),
        ("kill_switch", {"schedule_cancel_seconds": 0}, "schedule_cancel_seconds"),
        (
            "kill_switch",
            {"schedule_cancel_seconds": 30, "refresh_interval_seconds": 30},
            "fires between refreshes",
        ),
        ("kill_switch", {"on_refresh_failed": "ignore"}, "safe_mode"),
        ("kill_switch", {"on_shutdown": "leave_orders"}, "cancel_bot_owned_open_orders"),
    ],
)
def test_bad_sub_block_values_are_rejected(block, overrides, match):
    with pytest.raises(ValueError, match=match):
        LiveConfig.from_dict(_live_block(**{block: overrides}))


# ---------------------------------------------------------------------------
# §5 cap math
# ---------------------------------------------------------------------------


def test_caps_above_threshold_equity_are_bound_by_max_notional():
    # §5 worked example: equity >= 166.67 → the 100 USDC cap binds.
    caps = compute_notional_caps(D(1000), LiveSafetyConfig())
    assert caps.pct_cap_notional == D(600)
    assert caps.effective_notional_cap == D(100)
    assert not caps.below_exchange_minimum


def test_caps_below_threshold_equity_are_bound_by_pct():
    # §5 rule 1: equity below the threshold → pct cap binds below 100 USDC.
    caps = compute_notional_caps(D(100), LiveSafetyConfig())
    assert caps.pct_cap_notional == D(60)
    assert caps.effective_notional_cap == D(60)


def test_caps_below_exchange_minimum_flag():
    # §5 rule 4: a cap below the exchange min order value can never trade.
    caps = compute_notional_caps(D(10), LiveSafetyConfig())
    assert caps.effective_notional_cap == D(6)
    assert caps.effective_notional_cap < EXCHANGE_MIN_ORDER_NOTIONAL_USDC
    assert caps.below_exchange_minimum


def test_notional_caps_reject_incoherent_pairs():
    # The §5 rule-3 identity (effective = min(pct, max_notional) => effective
    # <= pct, both >= 0) is enforced at construction, so a hand-built instance
    # in a later PR can never report an impossible cap pair.
    with pytest.raises(ValueError, match="exceeds"):
        NotionalCaps(pct_cap_notional=D(5), effective_notional_cap=D(100))
    with pytest.raises(ValueError, match=">= 0"):
        NotionalCaps(pct_cap_notional=D(-5), effective_notional_cap=D(-10))


# ---------------------------------------------------------------------------
# risk: <-> live.safety consistency (§5/§10 — one sizing regime)
# ---------------------------------------------------------------------------


def _live_cfg(**safety) -> LiveConfig:
    return LiveConfig.from_dict(_live_block(safety=safety) if safety else _live_block())


def test_matching_blocks_pass_consistency():
    validate_live_risk_consistency(_live_cfg(), RiskConfig())


def test_tighter_live_cap_is_allowed():
    # Layered defense: the live hard cap may be tighter than the AI gate's cap
    # (the §10.1 checks reject loudly at execution time).
    validate_live_risk_consistency(
        _live_cfg(max_target_margin_pct=40),
        RiskConfig(max_target_margin_pct=60),
    )


def test_looser_live_cap_is_rejected():
    # Headroom the gate can never approve means the operator almost certainly
    # edited the wrong block.
    with pytest.raises(ValueError, match="exceeds risk.max_target_margin_pct"):
        validate_live_risk_consistency(
            _live_cfg(max_target_margin_pct=80),
            RiskConfig(max_target_margin_pct=60),
        )


def test_leverage_mismatch_is_rejected():
    with pytest.raises(ValueError, match="risk.leverage"):
        validate_live_risk_consistency(_live_cfg(), RiskConfig(leverage=D(2)))


# ---------------------------------------------------------------------------
# load_config integration: the live: top-level key
# ---------------------------------------------------------------------------


def test_load_config_accepts_live_block(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text("live:\n  mode: testnet_live\n  network: testnet\n", encoding="utf-8")
    config = load_config(path)
    assert LiveConfig.from_dict(config["live"]).mode is ExecutionMode.TESTNET_LIVE


def test_load_config_rejects_scalar_live_block(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text("live: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'live' must be a mapping"):
        load_config(path)
