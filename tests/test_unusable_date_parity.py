"""A date argument the model cannot be held to is refused the same way by either
vendor of the four routed news, OHLCV and indicator tools (#111) — the tail of
the parity PR #109 began for the fundamentals getters — and the direct-call
verification tool serves the same sentence (#112).

The tools used to answer ``""``/``"abc"``/``"2026/08/18"`` per vendor:
``get_news`` (yfinance: an error string the router reads as success; Alpha
Vantage: a bare ValueError the router re-raised), ``get_global_news`` (yfinance
with a quiet feed: ``No global news found for abc``, a coverage claim about a
day that was never named), ``get_stock_data`` (yfinance: a bare ValueError;
Alpha Vantage: typed no-data for two of the three and REAL ROWS for the third,
because pandas reads a slash-separated date) and ``get_indicators`` (both: the
raw ``strptime`` message, served by the tool wrapper with no retry
instruction). ``None`` is refused too: none of these tools has a date-less
lane, and on the pre-PR code it was a bare ``TypeError`` from ``strptime``
reachable only by a direct caller (the tool schemas require a string).

The refusal matrix itself — every value, every date parameter, both vendors,
direct and through the router, with every vendor seam raising — is the
table-driven sweep in ``test_date_refusal_coverage`` (#230), and each of the
old answers above is one cell of it. What stays here is the shared sentence's
shape and the two claims the sweep cannot express: a feed that really answers
nothing, and a verdict that outranks the date.
"""

import pytest

import tradingagents.dataflows.alpha_vantage_indicator as avi
import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as yfnews
from tests._date_refusal_table import GOOD, no_network
from tradingagents.dataflows.errors import WiringGapError
from tradingagents.dataflows.utils import (
    date_range_refusal,
    date_refusal,
    invalid_date_sentinel,
)


@pytest.mark.unit
class TestTheSharedSentence:
    def test_the_fundamentals_sentence_is_unchanged_byte_for_byte(self):
        # The STATEMENT getters (balance/cashflow/income, both vendors) pass
        # what="fundamentals", kind="point"; the coverage sweep pins their
        # answers by equality against this template. The overview/prediction
        # lanes moved to kind="disclosure" (#144), whose byte-pin lives in
        # test_optional_date_refusal.
        assert invalid_date_sentinel("abc", what="fundamentals", kind="point") == (
            "INVALID_CURR_DATE: curr_date 'abc' is not a valid yyyy-mm-dd date, so "
            "fundamentals cannot be bounded to a point in time. No data returned; "
            "retry with a valid yyyy-mm-dd date. Do not fabricate values."
        )

    def test_a_window_bound_names_its_argument_and_does_not_claim_a_point(self):
        # A tool with two date arguments must tell the model WHICH to fix, and
        # "bounded to a point in time" is false of a window.
        out = invalid_date_sentinel("abc", what="news", kind="window", param="end_date")
        assert out.startswith("INVALID_END_DATE: end_date 'abc' ")
        assert "so the news window cannot be resolved" in out
        assert "point in time" not in out
        assert "Do not fabricate values" in out

    def test_a_range_names_only_the_first_unusable_argument(self):
        # Start is judged first; a bad end is reported only when start is fine.
        assert date_range_refusal("abc", "", what="x") == invalid_date_sentinel(
            "abc", what="x", kind="window", param="start_date"
        )
        assert date_range_refusal(GOOD, "", what="x") == invalid_date_sentinel(
            "", what="x", kind="window", param="end_date"
        )
        assert date_range_refusal(GOOD, GOOD, what="x") is None

    def test_none_is_a_lane_only_where_the_caller_says_so(self):
        # None is refused by default; the fundamentals getters opt INTO the
        # omitted-argument lane (#73) with omitted_ok=True, so the exception is
        # the one that has to say so. A window has no lane at all.
        assert date_refusal(None, what="x", kind="point", omitted_ok=True) is None
        assert date_refusal(None, what="x", kind="point") is not None
        assert date_range_refusal(None, GOOD, what="x") is not None
        assert date_range_refusal(GOOD, None, what="x") is not None

    def test_the_argument_tags_are_a_closed_set(self):
        # The tags are read by the model, so a new one is a decision made in
        # utils, not minted by whatever name a new call site passes (#84's
        # reasoning for the disposition vocabulary). An unknown name raises at
        # the call instead of inventing INVALID_AS_OF_DATE.
        from tradingagents.dataflows.utils import _DATE_ARGUMENT_TAGS

        assert set(_DATE_ARGUMENT_TAGS) == {"curr_date", "start_date", "end_date"}
        # As a WiringGapError, not the bare KeyError the lookup raises: the
        # miss is ours, and the router reads an untyped failure from a getter
        # as the vendor's library and reports it as text (#219).
        with pytest.raises(WiringGapError):
            invalid_date_sentinel("abc", what="x", kind="point", param="as_of_date")

    def test_the_kind_is_stated_not_inferred_from_the_name(self):
        # A curr_date can be asked to bound a window and a start_date a point;
        # the sentence follows the caller's kind, never the argument's name.
        assert "window cannot be resolved" in invalid_date_sentinel(
            "abc", what="x", kind="window", param="curr_date"
        )
        assert "point in time" in invalid_date_sentinel(
            "abc", what="x", kind="point", param="start_date"
        )

    def test_the_empty_string_is_supplied_and_unusable(self):
        # "" is a value the model sent, not an omission — same verdict as #89,
        # and it is refused even where None would be a lane.
        assert date_refusal("", what="x", kind="point", omitted_ok=True) is not None


@pytest.mark.unit
class TestWhatTheSweepCannotSay:
    @pytest.mark.parametrize("value", ["abc", None])
    def test_a_quiet_feed_no_longer_answers_nothing_that_day(self, monkeypatch, value):
        # The specific leak: yfinance's "No global news found for {curr_date}"
        # early exit ran BEFORE the date was parsed, so an unusable date with
        # an EMPTY search came back as a coverage claim the agent reads as "no
        # news happened on abc". The seam here really does return nothing —
        # against the old ordering that is exactly the input that leaked.
        class _QuietSearch:
            def __init__(self, **kwargs):
                self.news = []

        monkeypatch.setattr(yfnews.yf, "Search", _QuietSearch)
        monkeypatch.setattr(yfnews, "yf_fetch_unhidden", lambda fn, **kw: fn())
        out = yfnews.get_global_news_yfinance(value, look_back_days=7)
        assert out != f"No global news found for {value}"
        assert out.startswith("INVALID_CURR_DATE")

    def test_an_unsupported_indicator_still_outranks_the_date(self, monkeypatch):
        # Both vendors judge the indicator name first, as before: that verdict
        # is true regardless of the date, and the wrapper serves it.
        no_network(monkeypatch)
        with pytest.raises(ValueError, match="not supported"):
            yfin.get_stock_stats_indicators_window("AAPL", "bogus", "abc", 30)
        with pytest.raises(ValueError, match="not supported"):
            avi.get_indicator("AAPL", "bogus", "abc", 30)
