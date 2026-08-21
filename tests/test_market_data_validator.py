"""Tests for the deterministic market-data verification snapshot (#830/#881)."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.market_data_validator as validator
from tradingagents.agents.utils.market_data_validation_tools import (
    get_verified_market_snapshot,
)
from tradingagents.dataflows.errors import NoMarketDataError


def _sample_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", "2026-05-20")
    closes = [100 + i for i in range(len(dates))]
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": [c - 0.5 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": closes,
            "Volume": [1_000_000 + i for i in range(len(dates))],
        }
    )


@pytest.mark.unit
class TestVerifiedSnapshot:
    def test_excludes_future_rows(self, monkeypatch):
        data = pd.concat(
            [
                _sample_ohlcv(),
                pd.DataFrame(
                    {
                        "Date": [pd.Timestamp("2026-06-01")],
                        "Open": [999.0],
                        "High": [999.0],
                        "Low": [999.0],
                        "Close": [999.0],
                        "Volume": [999],
                    }
                ),
            ],
            ignore_index=True,
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: data)

        snap = validator.build_verified_market_snapshot("COF", "2026-05-13")
        assert "Verified market data snapshot for COF" in snap
        assert "Requested analysis date: 2026-05-13" in snap
        assert "Latest trading row used: 2026-05-13" in snap
        assert "999.00" not in snap  # future row excluded
        assert "boll_lb" in snap  # indicators present

    def test_uses_previous_trading_day_when_date_is_weekend(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        # 2026-05-16 is a Saturday; latest row should be Fri 2026-05-15
        snap = validator.build_verified_market_snapshot("COF", "2026-05-16")
        assert "Latest trading row used: 2026-05-15" in snap
        assert "Recent verified closes" in snap

    def test_raises_classified_error_when_no_rows_on_or_before_date(self, monkeypatch):
        # NoMarketDataError, not a bare ValueError, so callers can map it to
        # the no-data sentinel via the VendorError taxonomy (#32).
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        with pytest.raises(NoMarketDataError):
            validator.build_verified_market_snapshot("COF", "2020-01-01")

    def test_raises_classified_error_on_empty_data(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: pd.DataFrame())
        with pytest.raises(NoMarketDataError):
            validator.build_verified_market_snapshot("COF", "2026-05-13")

    def test_look_back_window_capped_at_30(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20", look_back_days=999)
        # last-N closes table has at most 30 data rows
        close_rows = [ln for ln in snap.splitlines() if ln.startswith("| 2026-")]
        assert 0 < len(close_rows) <= 30


@pytest.mark.unit
class TestTool:
    def test_tool_delegates_to_builder(self, monkeypatch):
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        out = get_verified_market_snapshot.invoke({"symbol": "COF", "curr_date": "2026-05-20"})
        assert "Verified market data snapshot for COF" in out

    def test_tool_returns_no_data_sentinel_on_vendor_error(self, monkeypatch):
        # This tool bypasses route_to_vendor, so the wrapper itself must turn
        # the VendorError taxonomy into the instructive sentinel — a raise
        # would surface only as a generic ToolNode error string (#32).
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: pd.DataFrame())
        out = get_verified_market_snapshot.invoke({"symbol": "COF", "curr_date": "2026-05-20"})
        assert out.startswith("NO_DATA_AVAILABLE")
        assert "do not estimate or fabricate" in out.lower()

    def test_tool_reports_a_rate_limit_as_transient_not_as_no_data(self, monkeypatch):
        # A throttle must not be flattened into the permanent-sounding no-data
        # verdict: this tool is the agents' source of truth, and "verified data
        # is unavailable for this symbol" over a 429 would have the analyst
        # reporting a coverage fact for a condition that clears in minutes (#67).
        from tradingagents.dataflows.errors import VendorRateLimitError

        def _throttled(s, d):
            raise VendorRateLimitError("Yahoo Finance rate limited the request")

        monkeypatch.setattr(validator, "load_ohlcv", _throttled)
        out = get_verified_market_snapshot.invoke({"symbol": "COF", "curr_date": "2026-05-20"})
        assert out.startswith("DATA_UNAVAILABLE")
        assert "transient" in out
        assert not out.startswith("NO_DATA_AVAILABLE")
        assert "report that verified data is unavailable" not in out

    def test_tool_rejects_unparseable_curr_date_with_sentinel(self, monkeypatch):
        # A bad LLM-supplied date raises a bare ValueError deep in load_ohlcv
        # (outside the VendorError taxonomy), so the wrapper answers with the
        # INVALID_CURR_DATE sentinel before any data work starts.
        def _must_not_be_called(s, d):
            raise AssertionError("load_ohlcv must not be called for a bad date")

        monkeypatch.setattr(validator, "load_ohlcv", _must_not_be_called)
        out = get_verified_market_snapshot.invoke({"symbol": "COF", "curr_date": "not-a-date"})
        assert out.startswith("INVALID_CURR_DATE")

    def test_tool_turns_stale_data_raise_into_sentinel(self, monkeypatch):
        # load_ohlcv's own NoMarketDataError (e.g. the stale-frame guard) must
        # take the same sentinel path, not escape the tool.

        def _stale(s, d):
            raise NoMarketDataError(s, detail="latest row is stale")

        monkeypatch.setattr(validator, "load_ohlcv", _stale)
        out = get_verified_market_snapshot.invoke({"symbol": "COF", "curr_date": "2026-05-20"})
        assert out.startswith("NO_DATA_AVAILABLE")
        assert "stale" in out
