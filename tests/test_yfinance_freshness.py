"""yfinance freshness annotations (#30): financial statements carry a data-lag
note when the newest filtered period is far behind the analysis date; the
live-only fundamentals snapshot discloses when the analysis date trails the
wall clock; insider filings flag a long-dead stream. All yfinance access is
mocked — no network."""

import logging
from datetime import datetime, timedelta

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
        # Pin the date inside the note line itself — the CSV header also
        # contains it, so a bare substring check could pass vacuously.
        note_line = next((line for line in out.splitlines() if "Data lag" in line), "")
        assert note_line, out
        assert phrase in note_line
        assert "2025-01-31" in note_line

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

    def test_no_curr_date_falls_back_to_the_wall_clock(self, monkeypatch, caplog):
        # The model omitting curr_date used to switch off the note together
        # with the look-ahead filter, silently (#73). The filter genuinely
        # needs a bound; the note only needs a reference date, so it now
        # judges against today — and the degraded mode is logged.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2025-01-31"))
        with caplog.at_level(logging.WARNING, logger=yfin.__name__):
            out = yfin.get_balance_sheet("AAPL", "quarterly", None)
        assert "Data lag" in out
        assert any("without curr_date" in r.message for r in caplog.records)

    def test_no_curr_date_fresh_statement_has_no_note(self, monkeypatch):
        # The wall-clock fallback must not false-alarm on a freshly filed
        # quarter just because the date was omitted.
        recent = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement(recent))
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
class TestStatementBoundIsVendorAgnostic:
    """The same routed statement tool must flag the same gap through either
    vendor: honesty may not depend on which one ``data_vendors`` selected (#58).
    Exercised at the 180-day quarterly boundary, where an off-by-one bound or a
    drifted per-vendor copy shows up immediately."""

    _CURR_DATE = "2026-08-18"

    @pytest.mark.parametrize(
        "period,expect_note",
        [
            ("2026-02-19", False),  # exactly 180 days behind — on cadence
            ("2026-02-18", True),  # 181 days — genuinely stalled
        ],
    )
    def test_both_vendors_agree_at_the_bound(self, monkeypatch, period, expect_note):
        import json

        import tradingagents.dataflows.alpha_vantage_fundamentals as avf

        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement(period))
        yf_out = yfin.get_balance_sheet("AAPL", "quarterly", self._CURR_DATE)

        body = json.dumps({"quarterlyReports": [{"fiscalDateEnding": period}]})
        monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
        av_out = avf.get_balance_sheet("AAPL", "quarterly", self._CURR_DATE)
        av_note = json.loads(av_out).get(avf._FRESHNESS_NOTE_KEY, "")

        assert ("Data lag" in yf_out) is expect_note
        assert ("Data lag" in av_note) is expect_note


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


@pytest.mark.unit
class TestInsiderBoundIsVendorAgnostic:
    """The same routed insider tool must flag the same dead filing stream
    through either vendor (#69). Both import the single wall-clock bound from
    utils; the behavioral check runs both paths at the 90-day boundary, where
    an off-by-one or a drifted local copy shows up immediately."""

    def test_the_bound_is_the_single_shared_definition(self):
        import tradingagents.dataflows.alpha_vantage_news as avn
        from tradingagents.dataflows import utils

        assert utils.MAX_INSIDER_LAG_DAYS == 90
        assert yfin.MAX_INSIDER_LAG_DAYS == utils.MAX_INSIDER_LAG_DAYS
        assert avn.MAX_INSIDER_LAG_DAYS == utils.MAX_INSIDER_LAG_DAYS

    @pytest.mark.parametrize(
        "lag_days,expect_note",
        [
            (90, False),  # exactly at the bound — a sparse but living stream
            (91, True),  # one day beyond — flagged by both vendors
        ],
    )
    def test_both_vendors_agree_at_the_bound(self, monkeypatch, lag_days, expect_note):
        import json

        import tradingagents.dataflows.alpha_vantage_fundamentals as avf
        import tradingagents.dataflows.alpha_vantage_news as avn

        filed = (datetime.now() - timedelta(days=lag_days)).strftime("%Y-%m-%d")

        _patch_ticker(
            monkeypatch,
            insider_transactions=pd.DataFrame({"Start Date": [filed], "Shares": [100]}),
        )
        yf_out = yfin.get_insider_transactions("AAPL")

        body = json.dumps({"data": [{"transaction_date": filed}]})
        monkeypatch.setattr(avn, "_make_api_request", lambda function_name, params: body)
        av_note = json.loads(avn.get_insider_transactions("AAPL")).get(avf._FRESHNESS_NOTE_KEY, "")

        assert ("Data lag" in yf_out) is expect_note
        assert ("Data lag" in av_note) is expect_note
