"""OHLCV cleaning must not fabricate values (#38): rows missing any OHLC
field are dropped (never forward/back-filled), rows with impossible OHLC
ordering or non-positive prices are dropped, and the "verified" snapshot
wording is backed by those checks."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.market_data_validator as validator
from tradingagents.dataflows.stockstats_utils import _clean_dataframe


def _row(date, o, h, low, c, v=100):
    return {"Date": date, "Open": o, "High": h, "Low": low, "Close": c, "Volume": v}


def _clean(rows):
    return _clean_dataframe(pd.DataFrame(rows))


@pytest.mark.unit
class TestNoFabricatedValues:
    def test_incomplete_row_is_dropped_not_filled(self):
        out = _clean(
            [
                _row("2026-05-01", 10.0, 11.0, 9.0, 10.5),
                _row("2026-05-02", None, 11.5, 9.5, 11.0),  # missing Open
                _row("2026-05-03", 11.0, 12.0, 10.0, 11.5),
            ]
        )
        kept = out["Date"].dt.strftime("%Y-%m-%d").tolist()
        assert kept == ["2026-05-01", "2026-05-03"]
        # The old behaviour forward-filled Open=10.0 into the gap row; no
        # value in the surviving frame may be fabricated.
        assert (out["Open"] == 10.0).sum() == 1

    def test_missing_volume_is_kept_as_nan_not_fabricated(self):
        out = _clean(
            [
                _row("2026-05-01", 10.0, 11.0, 9.0, 10.5, v=100),
                _row("2026-05-02", 10.5, 11.5, 9.5, 11.0, v=None),
            ]
        )
        assert len(out) == 2  # volume is not an OHLC completeness requirement
        assert out["Volume"].isna().sum() == 1  # stays NaN -> renders as N/A

    def test_disordered_ohlc_row_is_dropped(self):
        out = _clean(
            [
                _row("2026-05-01", 10.0, 11.0, 9.0, 10.5),
                _row("2026-05-02", 10.5, 10.6, 9.5, 11.0),  # High < Close
                _row("2026-05-03", 11.0, 12.0, 11.5, 11.8),  # Low > Open
            ]
        )
        kept = out["Date"].dt.strftime("%Y-%m-%d").tolist()
        assert kept == ["2026-05-01"]

    def test_non_positive_price_row_is_dropped(self):
        out = _clean(
            [
                _row("2026-05-01", 10.0, 11.0, 9.0, 10.5),
                _row("2026-05-02", 10.5, 11.5, 0.0, 11.0),  # zero Low
                _row("2026-05-03", 11.0, 12.0, -1.0, 11.5),  # negative Low
            ]
        )
        kept = out["Date"].dt.strftime("%Y-%m-%d").tolist()
        assert kept == ["2026-05-01"]

    def test_clean_frame_passes_through_unchanged(self):
        rows = [
            _row("2026-05-01", 10.0, 11.0, 9.0, 10.5),
            _row("2026-05-02", 10.5, 11.5, 9.5, 11.0),
        ]
        out = _clean(rows)
        assert len(out) == 2


@pytest.mark.unit
class TestVerifiedWordingIsBacked:
    def test_snapshot_states_what_verified_means(self, monkeypatch):
        dates = pd.bdate_range("2026-04-01", "2026-05-20")
        closes = [100.0 + i for i in range(len(dates))]
        frame = pd.DataFrame(
            {
                "Date": dates,
                "Open": [c - 0.5 for c in closes],
                "High": [c + 1.0 for c in closes],
                "Low": [c - 1.0 for c in closes],
                "Close": closes,
                "Volume": [1_000_000] * len(dates),
            }
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: frame)
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20")
        assert "Verified means:" in snap
        assert "no forward-filled placeholder values" in snap

    def test_fabricated_row_never_reaches_the_snapshot(self, monkeypatch):
        # End-to-end through the real cleaner: the latest row misses Open, so
        # the snapshot must fall back to the previous complete row instead of
        # rendering a filled-in value.
        raw = pd.DataFrame(
            [
                _row("2026-05-18", 10.0, 11.0, 9.0, 10.5),
                _row("2026-05-19", None, 12.0, 10.0, 11.5),
            ]
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _clean_dataframe(raw))
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20")
        assert "Latest trading row used: 2026-05-18" in snap
        assert "11.50" not in snap  # the incomplete row's Close is gone


@pytest.mark.unit
class TestGuardCoverageEdges:
    def test_positivity_checked_even_when_an_ohlc_column_is_missing(self):
        # The ordering check needs all four columns, but positivity must
        # still run on whichever OHLC columns exist (review round 1).
        df = pd.DataFrame(
            [
                {"Date": "2026-05-01", "Open": 10.0, "High": 11.0, "Close": 10.5},
                {"Date": "2026-05-02", "Open": -1.0, "High": 11.5, "Close": 11.0},
            ]
        )
        out = _clean_dataframe(df)
        kept = out["Date"].dt.strftime("%Y-%m-%d").tolist()
        assert kept == ["2026-05-01"]


@pytest.mark.unit
class TestLoadOhlcvEmptyAfterCleaning:
    def test_all_rows_dropped_raises_classified_error(self, tmp_path):
        # Every cached row is missing OHLC fields, so cleaning empties the
        # frame. load_ohlcv must raise the classified error instead of
        # returning an empty frame that downstream date loops would mislabel
        # as "not a trading day" (review round 1 Critical).
        import copy

        import tradingagents.dataflows.config as config_module
        import tradingagents.default_config as default_config
        from tradingagents.dataflows import stockstats_utils as su
        from tradingagents.dataflows.config import set_config
        from tradingagents.dataflows.errors import NoMarketDataError

        today = pd.Timestamp.today()
        start_str = (today - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
        end_str = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cache = tmp_path / f"NVDA-YFin-data-{start_str}-{end_str}.csv"
        pd.DataFrame(
            {
                "Date": [(today - pd.Timedelta(days=1)).strftime("%Y-%m-%d")],
                "Open": [None],
                "High": [None],
                "Low": [None],
                "Close": [100.0],
                "Volume": [1],
            }
        ).to_csv(cache, index=False)

        set_config({"data_cache_dir": str(tmp_path)})
        try:
            with pytest.raises(NoMarketDataError, match="integrity cleaning"):
                su.load_ohlcv("NVDA", today.strftime("%Y-%m-%d"))
        finally:
            config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
