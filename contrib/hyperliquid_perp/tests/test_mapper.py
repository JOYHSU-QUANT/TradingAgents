"""Tests for the HL raw-JSON -> clean-schema mapper (pure functions)."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import Candle
from contrib.hyperliquid_perp.exchanges.hyperliquid import mapper
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    MalformedResponseError,
    UnknownCoinError,
)


def test_market_snapshot_picks_correct_coin(meta_and_asset_ctxs):
    snap = mapper.map_market_snapshot(meta_and_asset_ctxs, "ETH")
    assert snap.coin == "ETH"
    # ETH ctx is index 1 — confirm we didn't read BTC's row.
    assert snap.mark_price == Decimal("3400.5")
    assert isinstance(snap.mark_price, Decimal)
    assert isinstance(snap.funding, Decimal)
    assert snap.mid_price == Decimal("3400.7")


def test_market_snapshot_unknown_coin_raises(meta_and_asset_ctxs):
    with pytest.raises(UnknownCoinError):
        mapper.map_market_snapshot(meta_and_asset_ctxs, "DOGE")


def test_market_snapshot_malformed_raises():
    with pytest.raises(MalformedResponseError):
        mapper.map_market_snapshot([{"universe": []}], "BTC")  # missing assetCtxs


def test_market_snapshot_nan_markpx_raises():
    # A required price field that parses as NaN must raise loudly rather than let a
    # non-finite mark price poison every downstream exposure/sizing computation.
    meta = {"universe": [{"name": "BTC"}]}
    ctx = {
        "markPx": "NaN",
        "oraclePx": "60000",
        "prevDayPx": "60000",
        "openInterest": "1",
        "dayNtlVlm": "1",
        "funding": "0.0001",
    }
    with pytest.raises(MalformedResponseError, match="markPx"):
        mapper.map_market_snapshot([meta, [ctx]], "BTC")


def test_market_snapshot_assetctxs_shorter_than_universe_raises():
    # universe lists ETH at index 1 but assetCtxs has only one entry -> the index
    # guard must raise MalformedResponseError, not let an IndexError escape.
    meta = {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
    with pytest.raises(MalformedResponseError):
        mapper.map_market_snapshot([meta, [{"markPx": "60000"}]], "ETH")


def test_candles_sorted_oldest_first_from_unordered_input():
    # Indicators read series.iloc[-1] as "current"; newest-first input would make
    # every indicator compute on the oldest candle unless the sort actually runs.
    raw = [
        {"t": 2000, "T": 2999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"},
        {"t": 1000, "T": 1999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"},
    ]
    assert [c.open_time for c in mapper.map_candles(raw)] == [1000, 2000]


def test_funding_history_sorted_oldest_first_from_unordered_input():
    raw = [
        {"time": 2000, "fundingRate": "0.00002", "premium": "0"},
        {"time": 1000, "fundingRate": "0.00001", "premium": "0"},
    ]
    assert [p.time for p in mapper.map_funding_history(raw)] == [1000, 2000]


def test_candles_parsed_decimal_and_sorted(candle_snapshot):
    candles = mapper.map_candles(candle_snapshot)
    assert len(candles) == len(candle_snapshot)
    assert all(isinstance(c.close, Decimal) for c in candles)
    times = [c.open_time for c in candles]
    assert times == sorted(times)  # oldest first
    assert candles[0].high >= candles[0].low


def test_funding_history_parsed(funding_history):
    points = mapper.map_funding_history(funding_history)
    assert len(points) == len(funding_history)
    assert all(isinstance(p.rate, Decimal) for p in points)
    assert [p.time for p in points] == sorted(p.time for p in points)


def test_candles_missing_field_dropped_with_warning_not_keyerror(caplog):
    # A bar missing a required field ("t") is a transient feed glitch: it is dropped
    # per-bar (never a bare KeyError, never aborting the whole snapshot), and the
    # valid bar survives. Too-few-survivors is handled by the downstream warm-up guard.
    raw = [
        {"t": 1000, "T": 1999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"},
        {"T": 2999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "9"},  # no "t"
    ]
    # max_drop_fraction=1.0 isolates the per-bar drop mechanism from the aggregate
    # drop-fraction guard (a 2-element list with 1 drop is 50%, above the default 10%).
    with caplog.at_level(logging.WARNING):
        candles = mapper.map_candles(raw, max_drop_fraction=1.0)
    assert [c.open_time for c in candles] == [1000]
    assert "dropping malformed candleSnapshot[1]" in caplog.text


def test_funding_history_missing_field_point_dropped_keeping_valid(caplog):
    # A single bad funding point (missing "time") is dropped and warned; the valid
    # point survives — funding is non-critical, so one glitch must not abort the run.
    raw = [
        {"time": 1000, "fundingRate": "0.00001", "premium": "0"},
        {"fundingRate": "0.00002", "premium": "0"},  # no "time"
    ]
    # max_drop_fraction=1.0: isolate the per-point drop mechanism from the aggregate guard.
    with caplog.at_level(logging.WARNING):
        points = mapper.map_funding_history(raw, max_drop_fraction=1.0)
    assert [p.time for p in points] == [1000]
    assert "dropping malformed fundingHistory[1]" in caplog.text


def test_funding_history_all_points_malformed_raises():
    # If *every* point is malformed, that is a systematic feed fault, not a glitch —
    # fail loud rather than silently return an empty history.
    with pytest.raises(MalformedResponseError, match="all 1 fundingHistory"):
        mapper.map_funding_history([{"fundingRate": "0.00001", "premium": "0.0"}])


def test_candles_none_payload_raises_not_empty():
    # A null payload is an anomaly, not "no candles" — it must fail loud so the run
    # aborts rather than reasoning over silently-empty market data.
    with pytest.raises(MalformedResponseError):
        mapper.map_candles(None)


def test_funding_history_none_payload_raises_not_empty():
    with pytest.raises(MalformedResponseError):
        mapper.map_funding_history(None)


def test_candles_non_dict_element_raises_malformed_not_attributeerror():
    # A non-dict element (e.g. None in a partial response) must fail loud as a
    # domain error, not as a bare AttributeError escaping the port boundary.
    raw = [{"t": 1000, "T": 1999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"}, None]
    with pytest.raises(MalformedResponseError, match=r"candleSnapshot\[1\]"):
        mapper.map_candles(raw)


def test_candle_invariant_rejects_inverted_ohlc():
    # The type-level guard: a bar with high < low is structurally illegal and must
    # raise at construction so it can never reach the indicator math.
    with pytest.raises(ValueError, match="OHLC ordering"):
        Candle(
            open_time=1000,
            close_time=1999,
            open=Decimal("5"),
            high=Decimal("1"),
            low=Decimal("9"),
            close=Decimal("5"),
            volume=Decimal("1"),
        )


def test_candles_malformed_ohlc_dropped_with_warning(caplog):
    # One bar with high < low violates Candle's invariant; the mapper drops it and
    # warns rather than aborting the whole snapshot — the valid bar survives.
    raw = [
        {"t": 1000, "T": 1999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"},
        {"t": 2000, "T": 2999, "o": "5", "h": "1", "l": "9", "c": "5", "v": "1"},
    ]
    # max_drop_fraction=1.0: isolate per-bar drop + aggregate-warning behavior from the
    # drop-fraction abort guard (1/2 = 50% would otherwise trip it).
    with caplog.at_level(logging.WARNING):
        candles = mapper.map_candles(raw, max_drop_fraction=1.0)
    assert [c.open_time for c in candles] == [1000]
    assert "dropping malformed candleSnapshot[1]" in caplog.text
    # An aggregate line makes a systematic fault visible beyond the per-bar warnings.
    assert "dropped 1/2 candleSnapshot bars (50%)" in caplog.text


def _ok_candle(t):
    return {"t": t, "T": t + 999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"}


def test_candles_excessive_drop_fraction_raises():
    # Above the default 10% drop ceiling (2 of 10 bad = 20%), the survivor set has too
    # many silent gaps to feed indicators safely — fail loud rather than return a short,
    # non-contiguous window that still clears the downstream warm-up count gate.
    raw = [_ok_candle(i * 1000) for i in range(8)] + [{"o": "1"}, {"o": "1"}]  # 2 missing "t"
    with pytest.raises(MalformedResponseError, match="exceeds 10% threshold"):
        mapper.map_candles(raw)


def test_candles_drop_fraction_at_threshold_survives(caplog):
    # Exactly at the threshold (1 of 10 = 10%, not strictly above) stays a tolerable
    # glitch: drop and warn, do not raise.
    raw = [_ok_candle(i * 1000) for i in range(9)] + [{"o": "1"}]  # 1 missing "t"
    with caplog.at_level(logging.WARNING):
        candles = mapper.map_candles(raw)
    assert len(candles) == 9
    assert "dropped 1/10 candleSnapshot bars (10%)" in caplog.text


def test_candles_drop_fraction_override_tolerates_more():
    # A caller can raise the ceiling (e.g. from config) so a 20% drop is tolerated.
    raw = [_ok_candle(i * 1000) for i in range(8)] + [{"o": "1"}, {"o": "1"}]
    candles = mapper.map_candles(raw, max_drop_fraction=0.5)
    assert len(candles) == 8


def test_funding_excessive_drop_fraction_raises():
    # Mirror of the candle guard: a truncated funding window compresses the z-score's
    # stddev (an extreme rate looks normal), so above the 10% ceiling fail loud.
    raw = [{"time": i * 1000, "fundingRate": "0.00001", "premium": "0"} for i in range(8)]
    raw += [{"fundingRate": "0.00002"}, {"fundingRate": "0.00002"}]  # 2 missing "time"
    with pytest.raises(MalformedResponseError, match="exceeds 10% threshold"):
        mapper.map_funding_history(raw)


def test_candles_all_bars_malformed_raises():
    # If *every* bar is malformed, that is a systematic feed fault, not a glitch —
    # fail loud (parity with map_funding_history) rather than silently return [],
    # which downstream reads as a benign "0 candles available".
    raw = [{"t": 1000, "T": 1999, "o": "5", "h": "1", "l": "9", "c": "5", "v": "1"}]
    with pytest.raises(MalformedResponseError, match="all 1 candleSnapshot bars"):
        mapper.map_candles(raw)


def test_candles_negative_volume_dropped(caplog):
    # Negative volume is impossible real data; the bar is dropped, not trusted.
    raw = [
        {"t": 1000, "T": 1999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "1"},
        {"t": 2000, "T": 2999, "o": "1", "h": "2", "l": "1", "c": "1", "v": "-3"},
    ]
    # max_drop_fraction=1.0: isolate the per-bar drop mechanism from the aggregate guard.
    with caplog.at_level(logging.WARNING):
        candles = mapper.map_candles(raw, max_drop_fraction=1.0)
    assert len(candles) == 1


def test_funding_history_non_dict_element_raises_malformed():
    raw = [{"time": 1000, "fundingRate": "0.00001", "premium": "0"}, "garbage"]
    with pytest.raises(MalformedResponseError, match=r"fundingHistory\[1\]"):
        mapper.map_funding_history(raw)


def test_account_snapshot_missing_margin_summary_raises_naming_it():
    # A missing marginSummary must surface as a marginSummary error, not a
    # misdirecting "missing field 'accountValue'" from the swallowed `or {}`.
    state = {"withdrawable": "5000", "assetPositions": []}
    with pytest.raises(MalformedResponseError, match="marginSummary"):
        mapper.map_account_snapshot(state)


def test_market_snapshot_non_dict_universe_entry_raises():
    # A non-dict universe entry would raise a bare AttributeError on asset.get(...)
    # that escapes the port boundary — it must fail loud as a domain error.
    meta = {"universe": [{"name": "BTC"}, None]}
    with pytest.raises(MalformedResponseError, match=r"universe\[1\]"):
        mapper.map_market_snapshot([meta, [{"markPx": "60000"}, {"markPx": "1"}]], "BTC")


def test_market_snapshot_non_dict_asset_ctx_raises():
    # The matched asset context itself must be a dict — a non-dict at the coin's
    # index must fail loud as a domain error, not a bare AttributeError on ctx.get().
    meta = {"universe": [{"name": "BTC"}]}
    with pytest.raises(MalformedResponseError, match=r"assetCtxs\[0\]"):
        mapper.map_market_snapshot([meta, [None]], "BTC")


def test_account_snapshot_non_dict_position_entry_raises():
    # A truthy non-dict assetPositions entry would AttributeError inside
    # _map_position and, not being an ExchangeError, slip past _load_position's
    # guard — it must raise MalformedResponseError instead.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": ["garbage"],
    }
    with pytest.raises(MalformedResponseError, match=r"assetPositions\[0\]"):
        mapper.map_account_snapshot(state)


def test_account_snapshot_none_position_entry_is_tolerated_as_flat():
    # A None entry stays tolerated (read as flat) — only truthy non-dicts raise —
    # so this guard does not regress the empty/None-position handling.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
        "assetPositions": [None],
    }
    assert mapper.map_account_snapshot(state).positions == ()


def test_sized_position_without_coin_raises():
    # A sized position with no "coin" can't be matched by position_for and would
    # silently read as flat — it must raise instead of being dropped.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {"position": {"szi": "0.1", "entryPx": "61000", "unrealizedPnl": "1.0"}}
        ],
    }
    with pytest.raises(MalformedResponseError):
        mapper.map_account_snapshot(state)


def test_position_with_unparseable_size_raises():
    # A corrupt szi must fail loud, not silently read as flat (which would let the
    # engine open a position on top of a real one it could not see).
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "garbage",
                    "entryPx": "61000",
                    "unrealizedPnl": "1.0",
                }
            }
        ],
    }
    with pytest.raises(MalformedResponseError):
        mapper.map_account_snapshot(state)


def test_account_snapshot_none_payload_raises_not_flat():
    # A null clearinghouseState is an anomaly, not a flat account — it must fail
    # loud (like map_candles), not silently coerce to an empty account.
    with pytest.raises(MalformedResponseError):
        mapper.map_account_snapshot(None)


def test_empty_position_entry_treated_as_flat():
    # An entry with an empty position object carries no coin or size — there is
    # nothing to misread, so it is flat, not a malformed-data abort.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
        "assetPositions": [{"type": "oneWay", "position": {}}],
    }
    acct = mapper.map_account_snapshot(state)
    assert acct.positions == ()


def test_non_dict_inner_position_raises():
    # A present-but-non-dict "position" (e.g. a JSON null swapped for 0, a string)
    # must fail loud rather than be masked as flat by a bare ``or {}``.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [{"type": "oneWay", "position": "garbage"}],
    }
    with pytest.raises(MalformedResponseError, match=r"'position' is str"):
        mapper.map_account_snapshot(state)


def test_none_inner_position_tolerated_as_flat():
    # A genuinely absent (None) "position" stays tolerated as flat — only a
    # present non-dict raises — so the guard does not regress flat handling.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
        "assetPositions": [{"type": "oneWay", "position": None}],
    }
    assert mapper.map_account_snapshot(state).positions == ()


def test_falsy_non_list_asset_positions_raises():
    # A present-but-falsy non-list assetPositions (``false``/``0``) must not be
    # silently coerced to "no positions" by a bare ``or []`` — fail loud.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
        "assetPositions": False,
    }
    with pytest.raises(MalformedResponseError, match=r"assetPositions.*must be a list"):
        mapper.map_account_snapshot(state)


def test_missing_asset_positions_treated_as_flat():
    # An absent assetPositions field is a flat account, not an anomaly.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
    }
    assert mapper.map_account_snapshot(state).positions == ()


def test_account_snapshot_long_position(clearinghouse_state):
    acct = mapper.map_account_snapshot(clearinghouse_state)
    assert acct.account_value == Decimal("10250.50")
    assert acct.withdrawable == Decimal("9638.50")
    pos = acct.position_for("BTC")
    assert pos is not None
    assert pos.size == Decimal("0.05")
    assert pos.is_long and not pos.is_short
    assert pos.entry_price == Decimal("60000.0")
    assert pos.leverage == Decimal("5")


def test_position_helper_and_flat(clearinghouse_state):
    assert mapper.map_position(clearinghouse_state, "BTC") is not None
    # A coin with no open position -> None (flat).
    assert mapper.map_position(clearinghouse_state, "ETH") is None


def test_short_position_is_signed():
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "-0.1",
                    "entryPx": "61000",
                    "leverage": {"value": 3},
                    "unrealizedPnl": "-12.0",
                }
            }
        ],
    }
    pos = mapper.map_position(state, "BTC")
    assert pos.size == Decimal("-0.1")
    assert pos.is_short and not pos.is_long


def test_present_but_garbage_optional_field_is_dropped_and_warns(caplog):
    # A present-but-unparseable optional field (e.g. liquidationPx "N/A") is treated
    # as absent so downstream fallbacks apply, but must WARN — silently dropping a
    # corrupt liquidation price would hide it from the risk layer entirely.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.1",
                    "entryPx": "61000",
                    "leverage": {"value": 3},
                    "unrealizedPnl": "-12.0",
                    "liquidationPx": "N/A",
                }
            }
        ],
    }
    with caplog.at_level(logging.WARNING):
        pos = mapper.map_position(state, "BTC")
    assert pos is not None
    assert pos.liquidation_price is None  # garbage dropped, not raised
    assert any("liquidationPx" in r.message for r in caplog.records)


def test_blank_optional_field_is_absent_without_warning(caplog):
    # An empty-string optional field is a normal "omitted" case, not corruption —
    # it must drop to None silently (no spurious warning noise).
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.1",
                    "entryPx": "61000",
                    "leverage": {"value": 3},
                    "unrealizedPnl": "-12.0",
                    "liquidationPx": "",
                }
            }
        ],
    }
    with caplog.at_level(logging.WARNING):
        pos = mapper.map_position(state, "BTC")
    assert pos.liquidation_price is None
    assert not any("liquidationPx" in r.message for r in caplog.records)


def test_zero_size_position_treated_as_flat():
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "0"},
        "withdrawable": "5000",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.0",
                    "entryPx": "0",
                    "leverage": {"value": 1},
                    "unrealizedPnl": "0",
                }
            }
        ],
    }
    assert mapper.map_position(state, "BTC") is None


def test_non_dict_leverage_treated_as_absent_and_warns(caplog):
    # A truthy non-dict ``leverage`` (e.g. the bare string "cross") must not slip
    # past the guard and AttributeError inside ``.get("value")`` — it is dropped to
    # absent (leverage None) and warned, like every other corrupt optional field.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.1",
                    "entryPx": "61000",
                    "leverage": "cross",
                    "unrealizedPnl": "-12.0",
                }
            }
        ],
    }
    with caplog.at_level(logging.WARNING):
        pos = mapper.map_position(state, "BTC")
    assert pos is not None
    assert pos.leverage is None
    assert any("leverage" in r.message for r in caplog.records)


def test_opt_dec_nan_string_is_dropped_and_warns(caplog):
    # ``Decimal("NaN")`` parses without error but a NaN liquidation price would poison
    # the risk layer — it must drop to None (not propagate) and warn.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.1",
                    "entryPx": "61000",
                    "leverage": {"value": 3},
                    "unrealizedPnl": "-12.0",
                    "liquidationPx": "NaN",
                }
            }
        ],
    }
    with caplog.at_level(logging.WARNING):
        pos = mapper.map_position(state, "BTC")
    assert pos.liquidation_price is None
    assert any("liquidationPx" in r.message for r in caplog.records)


def test_required_field_nan_raises():
    # ``Decimal("Infinity")`` parses cleanly; a required field carrying one is
    # malformed, not numeric — it must fail loud rather than propagate a non-finite.
    state = {
        "marginSummary": {"accountValue": "5000", "totalMarginUsed": "100"},
        "withdrawable": "4900",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.1",
                    "entryPx": "Infinity",
                    "leverage": {"value": 3},
                    "unrealizedPnl": "0",
                }
            }
        ],
    }
    with pytest.raises(MalformedResponseError, match=r"entryPx"):
        mapper.map_position(state, "BTC")
