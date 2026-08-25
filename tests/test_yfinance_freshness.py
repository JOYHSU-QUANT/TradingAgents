"""yfinance freshness annotations (#30): financial statements carry a data-lag
note when the newest filtered period is far behind the analysis date; the
live-only fundamentals snapshot discloses when the analysis date trails the
wall clock; insider filings flag a long-dead stream. The ``*IsVendorAgnostic``
classes are cross-vendor instead: the same routed tool must disclose — and must
report an empty result — the same way through either vendor, so those drive the
Alpha Vantage path beside the yfinance one. All yfinance access is mocked — no
network."""

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


def _patch_av_request(monkeypatch, body):
    """Serve ``body`` as the Alpha Vantage response, and hand back the module.

    The cross-vendor classes below all drive the real Alpha Vantage getters, so
    the mock shape is defined once here rather than restated at each site.
    """
    import tradingagents.dataflows.alpha_vantage_fundamentals as avf

    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    return avf


def _av_freshness_note(av_out):
    """The freshness note Alpha Vantage attached to a served body, or ``""``."""
    import json

    import tradingagents.dataflows.alpha_vantage_fundamentals as avf

    return json.loads(av_out).get(avf._FRESHNESS_NOTE_KEY, "")


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

    def _both_vendor_notes(self, monkeypatch, period, curr_date):
        """Whether each vendor flagged a lag, for one fiscal period and date."""
        import json

        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement(period))
        yf_out = yfin.get_balance_sheet("AAPL", "quarterly", curr_date)

        body = json.dumps({"quarterlyReports": [{"fiscalDateEnding": period}]})
        avf = _patch_av_request(monkeypatch, body)
        av_out = avf.get_balance_sheet("AAPL", "quarterly", curr_date)

        return "Data lag" in yf_out, "Data lag" in _av_freshness_note(av_out)

    @pytest.mark.parametrize(
        "period,expect_note",
        [
            ("2026-02-19", False),  # exactly 180 days behind — on cadence
            ("2026-02-18", True),  # 181 days — genuinely stalled
        ],
    )
    def test_both_vendors_agree_at_the_bound(self, monkeypatch, period, expect_note):
        yf_noted, av_noted = self._both_vendor_notes(monkeypatch, period, self._CURR_DATE)
        assert yf_noted is expect_note
        assert av_noted is expect_note

    @pytest.mark.parametrize(
        "lag_days,expect_note",
        [
            (180, False),  # exactly at the quarterly bound — on cadence
            (181, True),  # one day beyond — flagged by both vendors
        ],
    )
    def test_both_vendors_agree_at_the_bound_without_a_curr_date(
        self, monkeypatch, lag_days, expect_note
    ):
        # The date-less fallback (#73) judges the newest period against the wall
        # clock instead of curr_date. Only a deeply-expired fixture covered that
        # lane, so the bound itself was unpinned there and either vendor could
        # have drifted to a different one without a test noticing (#90). Dates are
        # now-relative for that reason, unlike the literal pair above.
        period = (datetime.now() - timedelta(days=lag_days)).strftime("%Y-%m-%d")
        yf_noted, av_noted = self._both_vendor_notes(monkeypatch, period, None)
        assert yf_noted is expect_note
        assert av_noted is expect_note


@pytest.mark.unit
class TestUnusableCurrDateIsVendorAgnostic:
    """A curr_date that was supplied but cannot be used must be refused the same
    way through either vendor (#89).

    The three parametrized values are the three that used to be answered
    differently by each vendor; the CHANGELOG entry records what each one did.
    """

    _UNUSABLE = ["", "abc", "2026/08/18"]

    def test_the_refusal_is_the_single_shared_decision(self):
        # Pins that neither vendor holds its own copy of the judgement: both
        # names resolve to the one in utils, so a refinement made there reaches
        # both. It does NOT prove a getter still calls it — the equality
        # assertions below are what pin the answers themselves.
        import tradingagents.agents.utils.market_data_validation_tools as mdv
        import tradingagents.dataflows.alpha_vantage_fundamentals as avf
        import tradingagents.dataflows.alpha_vantage_indicator as avi
        import tradingagents.dataflows.alpha_vantage_news as avn
        import tradingagents.dataflows.alpha_vantage_stock as avs
        import tradingagents.dataflows.yfinance_news as yfnews
        from tradingagents.dataflows import utils

        assert yfin.curr_date_refusal is utils.curr_date_refusal
        assert avf.curr_date_refusal is utils.curr_date_refusal
        # The #111 tail: the news, OHLCV and indicator vendors, and the
        # direct-call verification tool's sentence (#112).
        assert yfnews.curr_date_refusal is utils.curr_date_refusal
        assert yfnews.date_range_refusal is utils.date_range_refusal
        assert avn.curr_date_refusal is utils.curr_date_refusal
        assert avn.date_range_refusal is utils.date_range_refusal
        assert yfin.date_range_refusal is utils.date_range_refusal
        assert avs.date_range_refusal is utils.date_range_refusal
        assert avi.curr_date_refusal is utils.curr_date_refusal
        assert mdv.invalid_curr_date_sentinel is utils.invalid_curr_date_sentinel

    @pytest.mark.parametrize("curr_date", _UNUSABLE)
    @pytest.mark.parametrize(
        "attr,method",
        [
            ("quarterly_balance_sheet", "get_balance_sheet"),
            ("quarterly_cashflow", "get_cashflow"),
            ("quarterly_income_stmt", "get_income_statement"),
        ],
    )
    def test_statements_refuse_in_one_voice(self, monkeypatch, curr_date, attr, method):
        import json

        # A future period: if either vendor were to serve unfiltered, the row it
        # must never leak is exactly the one the missing bound would have removed.
        _patch_ticker(monkeypatch, **{attr: _statement("2099-03-31")})
        yf_out = getattr(yfin, method)("AAPL", "quarterly", curr_date)

        body = json.dumps({"quarterlyReports": [{"fiscalDateEnding": "2099-03-31"}]})
        av_out = getattr(_patch_av_request(monkeypatch, body), method)(
            "AAPL", "quarterly", curr_date
        )

        # Exact equality, not startswith: it pins that the refusal is the WHOLE
        # answer, so neither vendor can append the future row it just refused to
        # bound. A "does not contain 2099-03-31" check could not fail once
        # startswith passed, since the sentence interpolates only curr_date.
        from tradingagents.dataflows.utils import invalid_curr_date_sentinel

        assert yf_out == av_out == invalid_curr_date_sentinel(curr_date, what="fundamentals")
        assert repr(curr_date) in yf_out  # the rejected value, so a retry can fix it

    @pytest.mark.parametrize("curr_date", _UNUSABLE)
    def test_the_overview_refuses_in_one_voice(self, monkeypatch, curr_date):
        import json

        # The live-snapshot disclosure needs a usable analysis date to decide
        # whether this is a backtest at all, so without one yfinance served
        # today's ratios with nothing said about them — the exact failure that
        # disclosure exists to prevent.
        _patch_ticker(monkeypatch, info={"longName": "Apple Inc.", "marketCap": 1_000_000})
        yf_out = yfin.get_fundamentals("AAPL", curr_date)

        av_out = _patch_av_request(monkeypatch, json.dumps({"Symbol": "AAPL"})).get_fundamentals(
            "AAPL", curr_date
        )

        # Exact equality pins that the refusal is the whole answer — the ratios
        # cannot ride along behind it (see the statement test for why a bare
        # "Apple Inc. not in output" check would have no power here).
        from tradingagents.dataflows.utils import invalid_curr_date_sentinel

        assert yf_out == av_out == invalid_curr_date_sentinel(curr_date, what="fundamentals")

    def test_an_omitted_curr_date_still_takes_the_date_less_lane(self, monkeypatch):
        import json

        # The refusal is for a value that was SUPPLIED. None means the model
        # omitted the argument, which keeps the #73 wall-clock fallback on both
        # vendors — refusing that too would delete a lane, not align one.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2025-01-31"))
        yf_out = yfin.get_balance_sheet("AAPL", "quarterly", None)

        body = json.dumps({"quarterlyReports": [{"fiscalDateEnding": "2025-01-31"}]})
        av_out = _patch_av_request(monkeypatch, body).get_balance_sheet("AAPL", "quarterly", None)

        assert "INVALID_CURR_DATE" not in yf_out
        assert "INVALID_CURR_DATE" not in av_out
        assert "Data lag" in yf_out
        assert "Data lag" in av_out

    def test_an_absent_symbol_outranks_an_unusable_date(self, monkeypatch):
        from tradingagents.dataflows.errors import NoMarketDataError

        # "This symbol has nothing" is true regardless of the analysis date, so
        # both vendors answer it first and an unknown ticker reaches the router's
        # no-data lane either way. Judging the date first would make yfinance
        # answer INVALID_CURR_DATE where Alpha Vantage raises.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=pd.DataFrame())
        with pytest.raises(NoMarketDataError):
            yfin.get_balance_sheet("AAPL", "quarterly", "")

        with pytest.raises(NoMarketDataError):
            _patch_av_request(monkeypatch, "{}").get_balance_sheet("AAPL", "quarterly", "")

    def test_undatable_columns_are_reported_as_a_schema_break_not_a_coverage_gap(
        self, monkeypatch, caplog
    ):
        # A frame whose column labels are not dates coerces to NaT and compares
        # False against any cutoff, so it empties for a reason that has nothing
        # to do with the analysis date. Calling that "nothing on or before your
        # date" would describe correct point-in-time behaviour, and the router
        # splices the detail straight into what the agent reads. Alpha Vantage
        # already separates these two, and logs only this one.
        from tradingagents.dataflows.errors import NoMarketDataError

        broken = pd.DataFrame({"foo": [1.0], "bar": [2.0]}, index=["Total Assets"])
        _patch_ticker(monkeypatch, quarterly_balance_sheet=broken)
        with (
            caplog.at_level(logging.WARNING, logger=yfin.__name__),
            pytest.raises(NoMarketDataError) as exc,
        ):
            yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")

        assert "all 2 balance sheet columns carried no usable fiscal period" in str(exc.value)
        assert "on or before" not in str(exc.value)
        assert any("usable fiscal period" in r.message for r in caplog.records)

    def test_a_genuine_coverage_gap_still_names_the_date_and_stays_quiet(self, monkeypatch, caplog):
        # The other side of the split: dated columns that all postdate curr_date
        # are correct point-in-time behaviour on any backtest older than the
        # vendor's window, so they name the date and must not page anyone.
        from tradingagents.dataflows.errors import NoMarketDataError

        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2027-01-31"))
        with (
            caplog.at_level(logging.WARNING, logger=yfin.__name__),
            pytest.raises(NoMarketDataError) as exc,
        ):
            yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")

        assert "no balance sheet data on or before 2026-08-18" in str(exc.value)
        # Nothing from this module at all, not merely nothing carrying the
        # schema-break phrase: a warning added to the coverage lane later must
        # fail this rather than slip past a phrase-scoped check.
        assert not [r for r in caplog.records if r.name == yfin.__name__]

    def test_the_shared_filter_refuses_an_unusable_bound_rather_than_dropping_it(self):
        from tradingagents.dataflows.stockstats_utils import filter_financials_by_date

        # The getters answer the sentinel before reaching here, so this raise is
        # unreachable in production and stands as the contract for a direct
        # caller: a broken point-in-time bound must fail loud, never serve the
        # frame whole — which is what falsiness used to do with "", leaking the
        # unfiltered frame rather than emptying it.
        frame = _statement("2099-03-31")
        with pytest.raises(ValueError, match="look-ahead guard"):
            filter_financials_by_date(frame, "")
        assert filter_financials_by_date(frame, None) is frame


def _tz_statement(*cols, tz="UTC"):
    """Statement frame whose fiscal-period columns carry a timezone."""
    return pd.DataFrame({pd.Timestamp(c, tz=tz): [100.0] for c in cols}, index=["Total Assets"])


@pytest.mark.unit
class TestTzAwareStatementColumnsAreNotAnErrorString:
    """yfinance returns statement columns tz-aware on some builds (#110).

    The look-ahead filter compared them against a naive cutoff, which pandas
    refuses with ``TypeError: Invalid comparison between dtype=datetime64[ns,
    UTC] and Timestamp`` (measured, 2.3.3). That landed in each getter's broad
    except and came back as ``"Error retrieving balance sheet for AAPL: Invalid
    comparison ..."`` — and ``route_to_vendor`` never inspects a returned
    string, so the chain stopped there and the agent analysed the error
    sentence as a balance sheet.
    """

    @pytest.mark.parametrize(
        "attr,method",
        [
            ("quarterly_balance_sheet", "get_balance_sheet"),
            ("quarterly_cashflow", "get_cashflow"),
            ("quarterly_income_stmt", "get_income_statement"),
        ],
    )
    def test_a_tz_aware_frame_is_served_as_a_report(self, monkeypatch, attr, method):
        _patch_ticker(monkeypatch, **{attr: _tz_statement("2026-06-30")})
        out = getattr(yfin, method)("AAPL", "quarterly", "2026-08-18")
        assert "Error retrieving" not in out
        assert "Total Assets" in out

    def test_the_bound_still_applies_to_tz_aware_columns(self, monkeypatch):
        # Not merely "no longer an error string": the look-ahead filter must
        # still do its job on these labels, or the fix would have bought
        # readability by leaking future periods.
        _patch_ticker(
            monkeypatch, quarterly_balance_sheet=_tz_statement("2026-06-30", "2099-03-31")
        )
        out = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "2026-06-30" in out
        assert "2099-03-31" not in out

    def test_a_tz_aware_frame_entirely_past_the_bound_is_still_no_data(self, monkeypatch):
        # The other edge of the same filter: zone-free comparison must not turn
        # a genuine coverage gap into a served report.
        from tradingagents.dataflows.errors import NoMarketDataError

        _patch_ticker(monkeypatch, quarterly_balance_sheet=_tz_statement("2099-03-31"))
        with pytest.raises(NoMarketDataError, match="on or before 2026-08-18"):
            yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")

    @pytest.mark.parametrize(
        "naive_label",
        ["2026-03-31", datetime(2026, 3, 31).date()],
        ids=["string", "date"],
    )
    def test_labels_mixing_a_zone_with_another_naive_type_are_served(
        self, monkeypatch, naive_label
    ):
        # The second measured failure, and the one the first fix missed:
        # coercing these labels as ONE index raises ValueError("Cannot mix
        # tz-aware with tz-naive values") before any comparison, so catching
        # only the comparison's TypeError still left the getter answering
        # "Error retrieving balance sheet for AAPL: Cannot mix tz-aware ..."
        # (measured, pandas 2.3.3). Both labels are usable fiscal periods and
        # both must survive.
        frame = pd.DataFrame(
            {c: [100.0] for c in (pd.Timestamp("2026-06-30", tz="UTC"), naive_label)},
            index=["Total Assets"],
        )
        _patch_ticker(monkeypatch, quarterly_balance_sheet=frame)
        out = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "Error retrieving" not in out
        assert "2026-06-30" in out
        assert "2026-03-31" in out

    def test_a_served_frame_reads_the_same_whether_or_not_the_vendor_sent_a_zone(self, monkeypatch):
        # The rendered CSV header is agent-facing text, so it must not depend on
        # which yfinance build answered. Only the surviving labels are compared,
        # since the two frames differ in what the bound removes.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_tz_statement("2026-06-30"))
        zoned = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2026-06-30"))
        naive = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")

        def _csv_header(report):
            return [line for line in report.splitlines() if line.startswith(",")][0]

        assert _csv_header(zoned) == _csv_header(naive)

    def test_a_future_only_mixed_frame_is_a_coverage_gap_not_a_schema_break(
        self, monkeypatch, caplog
    ):
        # The "did any column carry a fiscal period" measurement read the labels
        # as one index while the filter read them one at a time, so on this
        # frame the measurement raised, fell back to zero, and reported two
        # perfectly usable (merely future) periods as a vendor schema break —
        # the exact confusion that measurement exists to prevent.
        from tradingagents.dataflows.errors import NoMarketDataError

        frame = pd.DataFrame(
            {c: [100.0] for c in (pd.Timestamp("2026-12-31", tz="UTC"), "2026-09-30")},
            index=["Total Assets"],
        )
        _patch_ticker(monkeypatch, quarterly_balance_sheet=frame)
        with (
            caplog.at_level(logging.WARNING, logger=yfin.__name__),
            pytest.raises(NoMarketDataError) as exc,
        ):
            yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "no balance sheet data on or before 2026-08-18" in str(exc.value)
        assert "usable fiscal period" not in str(exc.value)
        # A coverage gap must not page anyone — nothing from this module at all.
        assert not [r for r in caplog.records if r.name == yfin.__name__]

    def test_the_lag_note_survives_mixed_labels_on_the_date_less_lane(self, monkeypatch):
        # The date-less lane (#73) never reaches the filter, so its labels are
        # whatever the vendor sent. Read as one index they either coerced to
        # mixed offsets and failed in max() OUTSIDE the note's guard — returning
        # the getter's "Error retrieving ..." string — or silently dropped the
        # disclosure. Per label, the note is judged against the wall clock as
        # #73 intends.
        frame = pd.DataFrame(
            {c: [100.0] for c in ("2020-03-31", pd.Timestamp("2020-06-30", tz="UTC"))},
            index=["Total Assets"],
        )
        _patch_ticker(monkeypatch, quarterly_balance_sheet=frame)
        out = yfin.get_balance_sheet("AAPL", "quarterly", None)
        assert "Error retrieving" not in out
        note_line = next((line for line in out.splitlines() if "Data lag" in line), "")
        assert "2020-06-30" in note_line  # the newest period, not the older naive one

    def test_tz_naive_columns_are_unchanged(self, monkeypatch):
        # The lane that already worked, pinned so the normalisation cannot have
        # been bought at its expense.
        _patch_ticker(monkeypatch, quarterly_balance_sheet=_statement("2026-06-30", "2099-03-31"))
        out = yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "2026-06-30" in out
        assert "2099-03-31" not in out

    @pytest.mark.parametrize(
        "refusing_label",
        [frozenset({"x"}), iter([1, 2]), {"a": 1}],
        ids=["frozenset-TypeError", "iterator-TypeError", "dict-ValueError"],
    )
    def test_a_label_type_the_parser_refuses_takes_the_typed_lane(
        self, monkeypatch, refusing_label
    ):
        # Driven by REAL refusing labels rather than a patched filter, and by
        # one of each exception family: pandas picks by label type, and a column
        # label need not be hashable to get there. Catching only one family left
        # the other returning an "Error retrieving ..." string the router reads
        # as a successful report — the failure this whole change exists to
        # close. Patching the filter instead missed both, because the getter
        # parses the same labels a second time outside that call.
        from tradingagents.dataflows.errors import NoMarketDataError

        frame = pd.DataFrame([[100.0, 200.0]], index=["Total Assets"])
        frame.columns = pd.Index([pd.Timestamp("2026-06-30"), refusing_label], dtype=object)
        _patch_ticker(monkeypatch, quarterly_balance_sheet=frame)
        with pytest.raises(NoMarketDataError) as exc:
            yfin.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
        assert "could not be read as fiscal periods" in str(exc.value)

    def test_the_same_refusal_on_the_date_less_lane_names_no_bound(self, monkeypatch):
        # The date-less lane (#73) asked for no bound, and this detail is
        # spliced into the router's agent-facing sentinel — naming one there
        # would describe a request the model never made.
        from tradingagents.dataflows.errors import NoMarketDataError

        frame = pd.DataFrame([[100.0, 200.0]], index=["Total Assets"])
        frame.columns = pd.Index([pd.Timestamp("2026-06-30"), {"a": 1}], dtype=object)
        _patch_ticker(monkeypatch, quarterly_balance_sheet=frame)
        with pytest.raises(NoMarketDataError) as exc:
            yfin.get_balance_sheet("AAPL", "quarterly", None)
        assert "could not be read as fiscal periods" in str(exc.value)
        assert "None" not in str(exc.value)

    def test_a_label_that_parses_to_an_index_is_dropped_not_compared(self, monkeypatch):
        # The other arm of the same branch, and the one no end-to-end input
        # reaches: a hashable container label (here a range) coerces to an Index
        # rather than a scalar. Treating it as the parsed value instead of NaT
        # makes the mask comprehension raise ValueError("truth value ... is
        # ambiguous"), which is not the typed lane either.
        from tradingagents.dataflows.stockstats_utils import coerce_period_labels

        periods, dropped = coerce_period_labels([range(3), pd.Timestamp("2026-06-30", tz="UTC")])
        assert pd.isna(periods[0])
        assert periods[1] == pd.Timestamp("2026-06-30")
        assert dropped is True

    def test_a_frame_with_no_zone_reports_none_dropped(self):
        # The flag that decides whether the served labels are rewritten.
        from tradingagents.dataflows.stockstats_utils import coerce_period_labels

        periods, dropped = coerce_period_labels([pd.Timestamp("2026-06-30"), "not a date", None])
        assert periods[0] == pd.Timestamp("2026-06-30")
        assert pd.isna(periods[1]) and pd.isna(periods[2])
        assert dropped is False


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

    def test_the_note_describes_the_newest_filing_of_several(self, monkeypatch):
        # Every other test here uses a single row, so which filing the note
        # picks was unpinned on this path — and this is the caller that hands
        # the shared parser many values rather than a frame's columns.
        df = pd.DataFrame(
            {"Start Date": ["2019-01-01", "2020-06-30", "2019-07-15"], "Shares": [1, 2, 3]}
        )
        _patch_ticker(monkeypatch, insider_transactions=df)
        out = yfin.get_insider_transactions("AAPL")
        note_line = next((line for line in out.splitlines() if "Data lag" in line), "")
        assert "2020-06-30" in note_line

    def test_a_stream_with_no_parseable_date_degrades_to_no_note(self, monkeypatch):
        # A present date column none of whose values parse leaves the parser
        # with nothing to take a newest from. Without the empty check the
        # annotation raises out of max() and the getter's broad except turns it
        # into an "Error retrieving ..." string the router reads as a report —
        # an annotation must degrade to silence, never replace what it decorates.
        df = pd.DataFrame({"Start Date": ["-", "n/a"], "Shares": [1, 2]})
        _patch_ticker(monkeypatch, insider_transactions=df)
        out = yfin.get_insider_transactions("AAPL")
        assert "Error retrieving" not in out
        assert "Data lag" not in out
        assert "Insider Transactions" in out


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

    def test_both_vendors_use_the_same_voice_for_an_empty_stream(self, monkeypatch):
        # An empty stream is normal for insiders; both vendors must say so in
        # the same sentence rather than one answering prose and the other raw
        # empty JSON the agent might hedge over.
        import json

        import tradingagents.dataflows.alpha_vantage_news as avn

        _patch_ticker(monkeypatch, insider_transactions=pd.DataFrame())
        yf_out = yfin.get_insider_transactions("AAPL")

        monkeypatch.setattr(
            avn, "_make_api_request", lambda function_name, params: json.dumps({"data": []})
        )
        av_out = avn.get_insider_transactions("AAPL")

        assert yf_out == av_out == "No insider transactions reported for symbol 'AAPL'"


@pytest.mark.unit
class TestEmptyNewsWindowIsVendorAgnostic:
    """The two routed news tools must answer an empty window in one voice (#90).

    Alpha Vantage filters NEWS_SENTIMENT server-side by ``time_from``/
    ``time_to``, so an empty feed asserts only "nothing in the window you asked
    for" — the same claim the yfinance getter makes when articles exist but none
    fall inside the window. The two vendors reach the sentence from different
    sides on purpose; what has to match is the sentence the agent reads, since
    which vendor served the call is not something the agent can see.
    """

    _STALE = datetime(2020, 1, 1).timestamp()

    def _av_empty_feed(self, monkeypatch):
        import json

        import tradingagents.dataflows.alpha_vantage_news as avn

        monkeypatch.setattr(avn, "_make_api_request", lambda *a, **k: json.dumps({"feed": []}))
        return avn

    def test_ticker_news(self, monkeypatch):
        import tradingagents.dataflows.yfinance_news as yfnews

        monkeypatch.setattr(
            yfnews.yf,
            "Ticker",
            lambda symbol: _FakeTicker(
                get_news=lambda count: [{"title": "Old news", "providerPublishTime": self._STALE}]
            ),
        )
        monkeypatch.setattr(yfnews, "yf_retry", lambda fn: fn())
        yf_out = yfnews.get_news_yfinance("AAPL", "2026-06-01", "2026-06-05")

        av_out = self._av_empty_feed(monkeypatch).get_news("AAPL", "2026-06-01", "2026-06-05")

        assert yf_out == av_out == "No news found for AAPL between 2026-06-01 and 2026-06-05"

    def test_global_news(self, monkeypatch):
        # Both vendors take a 7-day lookback by default, so the window named in
        # the sentence agrees as well as its wording.
        import tradingagents.dataflows.yfinance_news as yfnews

        stale = self._STALE

        class _FakeSearch:
            def __init__(self, **kwargs):
                self.news = [{"title": "Old macro news", "providerPublishTime": stale}]

        monkeypatch.setattr(yfnews.yf, "Search", _FakeSearch)
        monkeypatch.setattr(yfnews, "yf_retry", lambda fn: fn())
        yf_out = yfnews.get_global_news_yfinance("2026-06-08", look_back_days=7)

        av_out = self._av_empty_feed(monkeypatch).get_global_news("2026-06-08", look_back_days=7)

        assert yf_out == av_out == "No global news found between 2026-06-01 and 2026-06-08"
