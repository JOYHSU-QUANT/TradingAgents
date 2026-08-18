"""yfinance freshness annotations (#30): financial statements carry a data-lag
note when the newest filtered period is far behind the analysis date; the
live-only fundamentals snapshot discloses when the analysis date trails the
wall clock; insider filings flag a long-dead stream. All yfinance access is
mocked — no network."""

from datetime import datetime

import pandas as pd
import pytest

import tradingagents.dataflows.y_finance as yfin


class _FakeTicker:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def _patch_ticker(monkeypatch, **attrs):
    monkeypatch.setattr(yfin.yf, "Ticker", lambda symbol: _FakeTicker(**attrs))
    monkeypatch.setattr(yfin, "yf_retry", lambda fn: fn())


def _statement(*cols):
    """One-row statement frame with the given fiscal-period end columns."""
    return pd.DataFrame({pd.Timestamp(c): [100.0] for c in cols}, index=["Total Assets"])


@pytest.mark.unit
class TestStatementLagNote:
    @pytest.mark.parametrize(
        "method,attr,phrase",
        [
            (yfin.get_balance_sheet, "quarterly_balance_sheet", "balance sheet period"),
            (yfin.get_cashflow, "quarterly_cashflow", "cash flow period"),
            (yfin.get_income_statement, "quarterly_income_stmt", "income statement period"),
        ],
    )
    def test_stale_statement_carries_note(self, monkeypatch, method, attr, phrase):
        # Newest period 2025-01-31 vs analysis date 2026-08-18 (> 180 days).
        _patch_ticker(monkeypatch, **{attr: _statement("2025-01-31")})
        out = method("AAPL", "quarterly", "2026-08-18")
        assert "Data lag" in out
        assert phrase in out
        assert "2025-01-31" in out

    def test_normal_cadence_has_no_note(self, monkeypatch):
        # 49 days behind is a freshly filed quarter, not a stall.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2026-06-30"))
        out = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "Data lag" not in out

    def test_annual_bound_tolerates_a_year_old_statement(self, monkeypatch):
        # An annual statement is ~a year old by definition; the quarterly
        # bound would flag every annual call as stale (#30 review round).
        _patch_ticker(monkeypatch, balance_sheet=_statement("2025-09-27"))
        out = yfin.get_balance_sheet("AAPL", "annual", "2026-08-18")
        assert "Data lag" not in out

    def test_annual_statement_still_notes_when_genuinely_dead(self, monkeypatch):
        # Beyond a year plus a filing window even an annual filer is stalled.
        _patch_ticker(monkeypatch, balance_sheet=_statement("2024-06-30"))
        out = yfin.get_balance_sheet("AAPL", "annual", "2026-08-18")
        assert "Data lag" in out

    def test_note_reflects_newest_surviving_period(self, monkeypatch):
        # The look-ahead filter drops the future column first; the note must
        # describe the newest column the agent actually sees.
        _patch_ticker(
            monkeypatch,
            quarterly_balance_sheet=_statement("2025-01-31", "2027-01-31"),
        )
        out = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "Data lag" in out
        assert "2025-01-31" in out

    def test_no_curr_date_skips_note(self, monkeypatch):
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2025-01-31"))
        out = yfin.get_balance_sheet("AAPL", "quarterly", None)
        assert "Data lag" not in out


@pytest.mark.unit
class TestFundamentalsLiveSnapshotNote:
    _INFO = {"longName": "Apple Inc.", "marketCap": 1_000_000}

    def test_backtest_date_discloses_live_values(self, monkeypatch):
        _patch_ticker(monkeypatch, info=self._INFO)
        out = yfin.get_fundamentals("AAPL", "2020-01-01")
        assert "live values" in out
        assert "Apple Inc." in out  # data still rendered

    def test_current_date_has_no_note(self, monkeypatch):
        _patch_ticker(monkeypatch, info=self._INFO)
        out = yfin.get_fundamentals("AAPL", datetime.now().strftime("%Y-%m-%d"))
        assert "live values" not in out

    def test_no_curr_date_keeps_legacy_output(self, monkeypatch):
        _patch_ticker(monkeypatch, info=self._INFO)
        out = yfin.get_fundamentals("AAPL")
        assert "live values" not in out


@pytest.mark.unit
class TestInsiderLagNote:
    def test_dead_filing_stream_carries_note(self, monkeypatch):
        df = pd.DataFrame({"Start Date": ["2020-01-01"], "Shares": [100]})
        _patch_ticker(monkeypatch, insider_transactions=df)
        out = yfin.get_insider_transactions("AAPL")
        assert "Data lag" in out
        assert "insider filing" in out

    def test_recent_filing_has_no_note(self, monkeypatch):
        recent = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame({"Start Date": [recent], "Shares": [100]})
        _patch_ticker(monkeypatch, insider_transactions=df)
        out = yfin.get_insider_transactions("AAPL")
        assert "Data lag" not in out

    def test_missing_date_column_degrades_to_no_note(self, monkeypatch):
        df = pd.DataFrame({"Shares": [100]})
        _patch_ticker(monkeypatch, insider_transactions=df)
        out = yfin.get_insider_transactions("AAPL")
        assert "Data lag" not in out
        assert "Insider Transactions" in out  # still renders
