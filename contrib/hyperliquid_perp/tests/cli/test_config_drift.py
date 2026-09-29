"""Tests for the resume-time config drift report."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.cli import _config_drift_report, _run_config_subset


def _subset_json(config: dict, coin: str) -> str:
    """Exactly what run creation persists as config_json."""
    return json.dumps(_run_config_subset(config, coin), ensure_ascii=False, default=str)


def test_config_drift_no_stored_record_returns_none():
    # A pre-drift-check store has no genesis record: nothing to compare.
    assert _config_drift_report(None, {"risk": {"leverage": 5}}, "BTC") is None


def test_config_drift_coin_mismatch_is_hard_error():
    stored = _subset_json({}, "ETH")
    kind, msg = _config_drift_report(stored, {}, "BTC")
    assert kind == "coin"
    assert "'ETH'" in msg and "'BTC'" in msg


def test_config_drift_params_lists_drifted_keys_sorted():
    stored = _subset_json(
        {"risk": {"leverage": 5}, "decision": {"min_confidence": 0.5}, "paper_trading": None},
        "BTC",
    )
    current = {
        "risk": {"leverage": 3},
        "decision": {"min_confidence": 0.9},
        "paper_trading": None,
    }
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "decision, risk" in msg  # sorted; unchanged paper_trading absent
    assert "paper_trading" not in msg


def test_config_drift_identical_config_with_decimal_returns_none():
    # Decimals stringify via default=str at creation; the comparison round-trips
    # today's config the same way, so an unchanged Decimal is not false drift.
    config = {
        "risk": {"leverage": Decimal("5")},
        "decision": None,
        "paper_trading": {"fee_rate": Decimal("0.00045")},
    }
    stored = _subset_json(config, "BTC")
    assert _config_drift_report(stored, config, "BTC") is None


def test_config_drift_paper_trading_account_change_is_inert():
    # account (initial balance / seeds) is genesis-only: a resume-time edit
    # changes nothing, so it must not trip the "behaviour changes" warning.
    genesis = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 1000},
            "execution": {"fill_model": {"slippage_bps": 5}},
        }
    }
    stored = _subset_json(genesis, "BTC")
    edited_account = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 2000},
            "execution": {"fill_model": {"slippage_bps": 5}},
        }
    }
    assert _config_drift_report(stored, edited_account, "BTC") is None
    # ...while an execution edit (which DOES apply on resume) still warns.
    edited_execution = {
        "paper_trading": {
            "account": {"initial_balance_usdc": 1000},
            "execution": {"fill_model": {"slippage_bps": 9}},
        }
    }
    kind, msg = _config_drift_report(stored, edited_execution, "BTC")
    assert kind == "params"
    assert "paper_trading" in msg


def test_config_drift_covers_engine_market_data_and_indicators():
    # A model/analyst swap, a candle-window change, or an indicator-set change
    # redefines every subsequent decision — all three warn like risk drift.
    genesis = {
        "engine": {"deep_think_llm": "model-a"},
        "market_data": {"candle_interval": "4h", "candle_lookback": 200},
        "indicators": ["atr_14"],
    }
    stored = _subset_json(genesis, "BTC")
    current = {
        "engine": {"deep_think_llm": "model-b"},
        "market_data": {"candle_interval": "1h", "candle_lookback": 200},
        "indicators": ["atr_14", "rsi_14"],
    }
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "engine" in msg and "market_data" in msg and "indicators" in msg


_MD_GENESIS = {"candle_interval": "4h", "candle_lookback": 200}


def test_config_drift_ignores_a_key_added_at_its_inert_default():
    # Issue #98, the reproduction: a genesis written before
    # volume_profile_window_candles existed, and a config re-copied from the
    # newer example.yaml that carries the line at 0 (= off, the default).
    # Nothing about the run changed, so no drift — and no breadcrumb for the
    # review to read as a regime break.
    stored = _subset_json({"market_data": _MD_GENESIS}, "BTC")
    current = {"market_data": {**_MD_GENESIS, "volume_profile_window_candles": 0}}
    assert _config_drift_report(stored, current, "BTC") is None


def test_config_drift_still_reports_a_key_added_at_a_live_value():
    # The same key switched ON is a real regime break: the prompt grows a
    # section. Stripping must be on the VALUE being inert, not on the key
    # being new.
    stored = _subset_json({"market_data": _MD_GENESIS}, "BTC")
    current = {"market_data": {**_MD_GENESIS, "volume_profile_window_candles": 30}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "market_data" in msg


def test_config_drift_covers_the_macro_trend_key_without_being_told_about_it():
    # The drift report compares the PARSED block, not the raw YAML, so a new
    # ``MarketDataConfig`` field joins the comparison by existing — there is no
    # second list to remember to update. Both directions, because only the
    # pair says anything: the inert default must not raise a breadcrumb the
    # review would read as a regime break, and the key switched ON must,
    # because the prompt then grows a whole section.
    stored = _subset_json({"market_data": _MD_GENESIS}, "BTC")
    assert (
        _config_drift_report(
            stored, {"market_data": {**_MD_GENESIS, "macro_trend_daily_lookback": 0}}, "BTC"
        )
        is None
    )
    kind, msg = _config_drift_report(
        stored, {"market_data": {**_MD_GENESIS, "macro_trend_daily_lookback": 260}}, "BTC"
    )
    assert kind == "params"
    assert "market_data" in msg


def test_config_drift_ignores_a_default_valued_key_removed_from_the_yaml():
    # Symmetric: deleting the `: 0` line is the same non-event as adding it.
    stored = _subset_json(
        {"market_data": {**_MD_GENESIS, "volume_profile_window_candles": 0}}, "BTC"
    )
    assert _config_drift_report(stored, {"market_data": _MD_GENESIS}, "BTC") is None


def test_config_drift_treats_a_null_block_as_all_defaults():
    # The genesis YAML had no market_data: block at all (recorded as null,
    # since the key IS in the subset); the operator now adds the block holding
    # one default. Same semantics as an empty block — still no drift. A live
    # value in that same new block still is.
    stored = json.dumps({"market_data": None, "coin": "BTC"})
    assert (
        _config_drift_report(stored, {"market_data": {"volume_profile_window_candles": 0}}, "BTC")
        is None
    )
    kind, msg = _config_drift_report(
        stored, {"market_data": {"volume_profile_window_candles": 30}}, "BTC"
    )
    assert kind == "params" and "market_data" in msg


@pytest.mark.parametrize(
    ("block", "genesis", "inert", "live"),
    [
        # The RUNBOOK's own example of a key that joined a block later.
        (
            "decision",
            {"min_confidence": 0.5},
            {"resize_min_confidence": 0.7},
            {"resize_min_confidence": 0.9},
        ),
        ("risk", {"leverage": 5}, {"max_target_margin_pct": 60}, {"max_target_margin_pct": 40}),
        # Inert-ness is judged by the block's PARSER, not by ==: the YAML
        # string "30" is the int default 30 to int_from_yaml.
        (
            "market_data",
            {"candle_interval": "4h"},
            {"funding_zscore_window_days": "30"},
            {"funding_zscore_window_days": 14},
        ),
    ],
)
def test_config_drift_inert_default_stripping_covers_every_parsed_block(
    block, genesis, inert, live
):
    # One mechanism for every block that has a typed parser — not a special
    # case for the key that surfaced the bug (issue #98 acceptance).
    stored = _subset_json({block: genesis}, "BTC")
    assert _config_drift_report(stored, {block: {**genesis, **inert}}, "BTC") is None
    kind, msg = _config_drift_report(stored, {block: {**genesis, **live}}, "BTC")
    assert kind == "params"
    assert block in msg


def test_config_drift_inert_stripping_applies_to_the_resume_effective_execution_block():
    # paper_trading is compared on its `execution` projection, so the parser
    # that vouches for a default there is PaperExecutionConfig's — including
    # a whole default-valued sub-block (fill_model at slippage 5).
    genesis = {"paper_trading": {"execution": {"taker_fee_rate": 0.00045}}}
    stored = _subset_json(genesis, "BTC")
    inert = {
        "paper_trading": {
            "execution": {
                "taker_fee_rate": 0.00045,
                "min_notional_usdc": 10,
                "fill_model": {"slippage_bps": 5},
            }
        }
    }
    assert _config_drift_report(stored, inert, "BTC") is None
    live = {"paper_trading": {"execution": {"taker_fee_rate": 0.00045, "min_notional_usdc": 25}}}
    kind, msg = _config_drift_report(stored, live, "BTC")
    assert kind == "params" and "paper_trading" in msg


def test_config_drift_does_not_vouch_for_blocks_without_a_parser():
    # engine: has no typed defaults (its keys are `or` fallbacks), so nothing
    # can certify a new key there as inert — the whole-block comparison
    # stays, and a new key reads as drift rather than being guessed away.
    stored = _subset_json({"engine": {"deep_think_llm": "model-a"}}, "BTC")
    current = {"engine": {"deep_think_llm": "model-a", "structured_output": False}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params" and "engine" in msg


def test_config_drift_parsed_comparison_reaches_nested_defaults_and_types():
    # The comparison is on the PARSED block, so it is not one level deep and
    # not type-literal: a default-valued key added inside a sub-block both
    # sides carry, and a key spelled "30" at genesis and 30 today, both parse
    # to the same object. The raw `!=` these replace reported both as drift.
    nested_genesis = {"paper_trading": {"execution": {"fill_model": {}}}}
    nested_current = {"paper_trading": {"execution": {"fill_model": {"slippage_bps": 5}}}}
    assert _config_drift_report(_subset_json(nested_genesis, "BTC"), nested_current, "BTC") is None
    typed_genesis = {"market_data": {"funding_zscore_window_days": "30"}}
    typed_current = {"market_data": {"funding_zscore_window_days": 30}}
    assert _config_drift_report(_subset_json(typed_genesis, "BTC"), typed_current, "BTC") is None


def test_config_drift_falls_back_to_the_raw_comparison_when_a_side_does_not_parse():
    # A genesis carrying a key today's parser refuses (renamed, retired) can't
    # be judged "same" by that parser; the raw comparison keeps the drift
    # visible instead of hiding it behind the exception — and, symmetrically,
    # keeps an unparseable-but-identical pair quiet (the Decimal test above).
    stored = _subset_json({"risk": {"leverage": 5, "retired_key": 1}}, "BTC")
    kind, msg = _config_drift_report(stored, {"risk": {"leverage": 5}}, "BTC")
    assert kind == "params" and "risk" in msg


def test_config_drift_never_aborts_on_a_value_the_parser_chokes_on_arithmetically():
    # Regression pin for the ArithmeticError clause of _same_effective_block.
    # It was added when a YAML `.nan` survived decimal_from_yaml and detonated
    # in RiskConfig.__post_init__'s `<= 0` check as decimal.InvalidOperation;
    # since issue #128 the coercion refuses `.nan` as a ValueError, so that
    # input no longer exercises the clause. A parser is any callable and a
    # future dataclass invariant can still raise arithmetically — the fallback
    # must keep yielding a verdict (raw comparison) rather than a traceback
    # before the protection loop is armed.
    from decimal import InvalidOperation

    from contrib.hyperliquid_perp.cli._drift import _same_effective_block

    def exploding_parser(block):
        raise InvalidOperation("range check on a non-finite value")

    assert _same_effective_block(exploding_parser, {"leverage": 2}, {"leverage": 2}) is True
    assert _same_effective_block(exploding_parser, {"leverage": 2}, {"leverage": 3}) is False


def test_config_drift_on_a_nan_stored_value_is_a_named_verdict():
    # The end-to-end shape the clause above used to be the only guard for: a
    # genesis carrying `.nan` (written by a build before #128) resumed under a
    # finite config must report drift, never abort. Today the parser refuses
    # the stored side as a ValueError and the raw comparison decides.
    stored = _subset_json({"risk": {"leverage": float("nan")}}, "BTC")
    kind, msg = _config_drift_report(stored, {"risk": {"leverage": 2}}, "BTC")
    assert kind == "params" and "risk" in msg


def _live_subset_json(config: dict, coin: str) -> str:
    """Exactly what LIVE run creation persists: the shared subset plus `live:`."""
    subset = _run_config_subset(config, coin)
    subset["live"] = config.get("live")
    return json.dumps(subset, ensure_ascii=False, default=str)


def test_config_drift_live_network_change_is_hard_error():
    # live.network is run IDENTITY, like coin (decided 2026-07-17): resuming a
    # testnet-created run against a mainnet config would arm the wallet-wide
    # kill switch on the mainnet wallet and reconcile a testnet ledger against
    # the mainnet exchange — every leg mismatching, with nothing naming why.
    genesis = {"live": {"network": "testnet", "allow_real_orders": True}}
    stored = _live_subset_json(genesis, "BTC")
    current = {"live": {"network": "mainnet", "allow_real_orders": True}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "network"
    assert "'testnet'" in msg and "'mainnet'" in msg


def test_config_drift_live_network_case_change_is_not_drift():
    # LiveConfig reads network case-insensitively, so `TestNet` must not
    # false-flag a hard error against a stored `testnet`.
    stored = _live_subset_json({"live": {"network": "testnet"}}, "BTC")
    assert _config_drift_report(stored, {"live": {"network": "TestNet"}}, "BTC") is None


def test_config_drift_non_network_live_change_warns():
    # Safety caps / kill-switch timings redefine behaviour from here on: the
    # operator may intend it, so warn rather than refuse.
    genesis = {"live": {"network": "testnet", "safety": {"max_notional_usdc": 100}}}
    stored = _live_subset_json(genesis, "BTC")
    current = {"live": {"network": "testnet", "safety": {"max_notional_usdc": 500}}}
    kind, msg = _config_drift_report(stored, current, "BTC")
    assert kind == "params"
    assert "live" in msg


def test_config_drift_paper_genesis_never_reaches_the_live_checks():
    # A paper run's genesis never stores `live:`, but the same YAML may well
    # carry one — absence in the record means "not part of this run's identity",
    # so neither the network hard-fail nor the live warning may fire.
    stored = _subset_json({"risk": {"leverage": 5}}, "BTC")
    current = {"risk": {"leverage": 5}, "live": {"network": "mainnet"}}
    assert _config_drift_report(stored, current, "BTC") is None


def test_config_drift_pre_upgrade_record_skips_later_keys():
    # A genesis record written before engine/market_data/indicators joined the
    # subset lacks those keys entirely; absence means "unknown", not "was
    # empty" — the comparison skips them instead of false-flagging every old
    # run whose config carries the blocks today.
    stored = json.dumps(
        {"risk": {"leverage": 5}, "decision": None, "paper_trading": None, "coin": "BTC"}
    )
    current = {
        "risk": {"leverage": 5},
        "engine": {"deep_think_llm": "model-a"},
        "market_data": {"candle_interval": "4h"},
        "indicators": ["atr_14"],
    }
    assert _config_drift_report(stored, current, "BTC") is None
