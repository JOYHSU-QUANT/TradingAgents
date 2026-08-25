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
reachable only by a direct caller (the tool schemas require a string). All
network access
is mocked to fail loudly, so every test here also pins that the refusal happens
before any vendor is asked.
"""

import contextlib
import copy

import pytest

import tests.test_yfinance_freshness as freshness
import tradingagents.dataflows.alpha_vantage_indicator as avi
import tradingagents.dataflows.alpha_vantage_news as avn
import tradingagents.dataflows.alpha_vantage_stock as avs
import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as yfnews
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.utils import (
    date_range_refusal,
    date_refusal,
    invalid_date_sentinel,
)

# The canonical "inputs the vendors used to disagree on" list is the #89 one
# (read as a module attribute, not imported by name, so pytest does not collect
# that class a second time here); these tools add None, which the fundamentals
# getters keep as a lane (#73).
_UNUSABLE = [*freshness.TestUnusableCurrDateIsVendorAgnostic._UNUSABLE, None]
_GOOD = "2026-06-05"


class _VendorReached(Exception):
    """Raised by every mocked network seam: reaching it is the failure."""


def _no_network(monkeypatch):
    """Every seam a getter under test could reach the vendor through.

    Returns the list the seams append to before raising: some getters swallow
    the raise (a broad except, or the indicator path's per-date error prints),
    so "was the vendor asked?" is read from this list, not from the outcome.
    """
    reached = []

    def _reached(*a, **k):
        reached.append(a)
        raise _VendorReached("the vendor was asked before the date was judged")

    monkeypatch.setattr(yfnews.yf, "Ticker", _reached)
    monkeypatch.setattr(yfnews.yf, "Search", _reached)
    monkeypatch.setattr(yfin.yf, "Ticker", _reached)
    monkeypatch.setattr(yfin, "_get_stock_stats_bulk", _reached)
    monkeypatch.setattr(avn, "_make_api_request", _reached)
    monkeypatch.setattr(avs, "_make_api_request", _reached)
    monkeypatch.setattr(avi, "_make_api_request", _reached)
    # yf_retry wraps the call; make it transparent so the seam above is what fires.
    monkeypatch.setattr(yfnews, "yf_retry", lambda fn: fn())
    monkeypatch.setattr(yfin, "yf_retry", lambda fn: fn())
    return reached


def _asked(reached, call, *args):
    """Whether ``call(*args)`` reached a vendor seam, however the getter reported it."""
    del reached[:]
    with contextlib.suppress(_VendorReached):
        call(*args)
    return bool(reached)


# (yfinance getter, Alpha Vantage getter, what) for the two window-bounded tools.
_WINDOW_TOOLS = [
    pytest.param(yfnews.get_news_yfinance, avn.get_news, "news", id="get_news"),
    pytest.param(yfin.get_YFin_data_online, avs.get_stock, "stock price data", id="get_stock_data"),
]

# The same for the two curr_date tools; each vendor called as (curr_date).
_POINT_TOOLS = [
    pytest.param(
        lambda d: yfnews.get_global_news_yfinance(d, look_back_days=7),
        lambda d: avn.get_global_news(d, look_back_days=7),
        "global news",
        id="get_global_news",
    ),
    pytest.param(
        lambda d: yfin.get_stock_stats_indicators_window("AAPL", "rsi", d, 30),
        lambda d: avi.get_indicator("AAPL", "rsi", d, 30),
        "indicator values",
        id="get_indicators",
    ),
]


@pytest.mark.unit
class TestTheSharedSentence:
    def test_the_fundamentals_sentence_is_unchanged_byte_for_byte(self):
        # The four fundamentals getters now pass what="fundamentals"; PR #109's
        # cross-vendor tests pin their answers by equality against this, so
        # the parameterisation must not have moved a character of it.
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
        assert date_range_refusal(_GOOD, "", what="x") == invalid_date_sentinel(
            "", what="x", kind="window", param="end_date"
        )
        assert date_range_refusal(_GOOD, _GOOD, what="x") is None

    def test_none_is_a_lane_only_where_the_caller_says_so(self):
        # None is refused by default; the fundamentals getters opt INTO the
        # omitted-argument lane (#73) with omitted_ok=True, so the exception is
        # the one that has to say so. A window has no lane at all.
        assert date_refusal(None, what="x", kind="point", omitted_ok=True) is None
        assert date_refusal(None, what="x", kind="point") is not None
        assert date_range_refusal(None, _GOOD, what="x") is not None
        assert date_range_refusal(_GOOD, None, what="x") is not None

    def test_the_argument_tags_are_a_closed_set(self):
        # The tags are read by the model, so a new one is a decision made in
        # utils, not minted by whatever name a new call site passes (#84's
        # reasoning for the disposition vocabulary). An unknown name raises at
        # the call instead of inventing INVALID_AS_OF_DATE.
        from tradingagents.dataflows.utils import _DATE_ARGUMENT_TAGS

        assert set(_DATE_ARGUMENT_TAGS) == {"curr_date", "start_date", "end_date"}
        with pytest.raises(KeyError):
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
class TestWindowToolsRefuseInOneVoice:
    @pytest.mark.parametrize("value", _UNUSABLE)
    @pytest.mark.parametrize("param", ["start_date", "end_date"])
    @pytest.mark.parametrize("yf_getter,av_getter,what", _WINDOW_TOOLS)
    def test_either_date(self, monkeypatch, value, param, yf_getter, av_getter, what):
        _no_network(monkeypatch)
        args = {"start_date": _GOOD, "end_date": _GOOD, param: value}

        yf_out = yf_getter("AAPL", args["start_date"], args["end_date"])
        av_out = av_getter("AAPL", args["start_date"], args["end_date"])

        # Whole-answer equality: the refusal IS the answer, nothing rides behind
        # it, and neither vendor was reached (the seams raise if they were).
        assert (
            yf_out == av_out == invalid_date_sentinel(value, what=what, kind="window", param=param)
        )
        assert repr(value) in av_out

    @pytest.mark.parametrize("yf_getter,av_getter,what", _WINDOW_TOOLS)
    def test_a_usable_range_still_reaches_both_vendors(
        self, monkeypatch, yf_getter, av_getter, what
    ):
        # The gate must let a good date through — otherwise a passing refusal
        # test could be a gate that refuses everything.
        reached = _no_network(monkeypatch)
        for getter in (yf_getter, av_getter):
            assert _asked(reached, getter, "AAPL", _GOOD, _GOOD), getter

    def test_the_yfinance_news_error_string_lane_is_closed(self, monkeypatch):
        # Before: parsed inside the broad except, so the answer began "Error
        # fetching news" — a string the router serves as a successful report.
        _no_network(monkeypatch)
        out = yfnews.get_news_yfinance("AAPL", "abc", _GOOD)
        assert not out.startswith("Error fetching news")
        assert out.startswith("INVALID_START_DATE")

    def test_alpha_vantage_no_longer_serves_rows_for_a_slash_date(self, monkeypatch):
        # The one silent case: pandas parses "2026/08/18", so the range filter
        # accepted it and real OHLCV came back as if the model had sent the
        # ISO date. The seam raising proves no request was made.
        _no_network(monkeypatch)
        out = avs.get_stock("AAPL", _GOOD, "2026/08/18")
        assert out.startswith("INVALID_END_DATE")
        assert "timestamp,open" not in out

    def test_yfinance_no_longer_fetches_an_unbounded_window_for_none(self, monkeypatch):
        # The strptime this PR deleted was also the None guard (a bare
        # TypeError, on the pre-PR code). Without a gate in its place, None
        # reaches ticker.history(start=None), which yfinance answers with its
        # default trailing month — today's bars under a header naming the
        # requested historical end_date (measured on an intermediate draft of
        # this change). The refusal is what stands between the two.
        _no_network(monkeypatch)
        out = yfin.get_YFin_data_online("AAPL", None, "2020-06-05")
        assert out == invalid_date_sentinel(
            None, what="stock price data", kind="window", param="start_date"
        )

    def test_a_non_zero_padded_date_is_still_usable(self, monkeypatch):
        # strptime accepts "2026-6-5"; the refusal must not be stricter than
        # the parser the getters go on to use (#89 kept this too).
        reached = _no_network(monkeypatch)
        assert _asked(reached, avn.get_news, "AAPL", "2026-6-5", _GOOD)


@pytest.mark.unit
class TestPointToolsRefuseInOneVoice:
    @pytest.mark.parametrize("value", _UNUSABLE)
    @pytest.mark.parametrize("yf_call,av_call,what", _POINT_TOOLS)
    def test_curr_date(self, monkeypatch, value, yf_call, av_call, what):
        _no_network(monkeypatch)
        assert (
            yf_call(value)
            == av_call(value)
            == invalid_date_sentinel(value, what=what, kind="point")
        )

    @pytest.mark.parametrize("yf_call,av_call,what", _POINT_TOOLS)
    def test_a_usable_date_still_reaches_both_vendors(self, monkeypatch, yf_call, av_call, what):
        reached = _no_network(monkeypatch)
        for call in (yf_call, av_call):
            assert _asked(reached, call, _GOOD), call

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
        monkeypatch.setattr(yfnews, "yf_retry", lambda fn: fn())
        out = yfnews.get_global_news_yfinance(value, look_back_days=7)
        assert out != f"No global news found for {value}"
        assert out.startswith("INVALID_CURR_DATE")

    def test_indicators_no_longer_answer_the_raw_strptime_message(self, monkeypatch):
        # Before: both vendors raised strptime's ValueError and the tool wrapper
        # served its message — the same on both, but no tag and no retry
        # instruction, unlike the sibling tools called in the same turn.
        _no_network(monkeypatch)
        out = avi.get_indicator("AAPL", "rsi", "2026/08/18", 30)
        assert "does not match format" not in out
        assert out.startswith("INVALID_CURR_DATE")

    def test_an_unsupported_indicator_still_outranks_the_date(self, monkeypatch):
        # Both vendors judge the indicator name first, as before: that verdict
        # is true regardless of the date, and the wrapper serves it.
        _no_network(monkeypatch)
        with pytest.raises(ValueError, match="not supported"):
            yfin.get_stock_stats_indicators_window("AAPL", "bogus", "abc", 30)
        with pytest.raises(ValueError, match="not supported"):
            avi.get_indicator("AAPL", "bogus", "abc", 30)


@pytest.mark.unit
class TestThroughTheRouter:
    """None of these categories is optional, so a raise leaving a getter used to
    be ``raise first_error`` — a crash of the ToolNode-wrapped run. A returned
    refusal is served as the tool's answer instead. The chain is pinned to
    yfinance alone so the test exercises the vendor whose bare raise this was,
    rather than whichever vendor the default chain happens to try first."""

    @pytest.fixture(autouse=True)
    def _yfinance_only(self, monkeypatch):
        _no_network(monkeypatch)
        cfg = copy.deepcopy(default_config.DEFAULT_CONFIG)
        cfg["data_vendors"] = dict.fromkeys(cfg["data_vendors"], "yfinance")
        monkeypatch.setattr(config_module, "_config", cfg)

    def test_stock_data(self):
        out = interface.route_to_vendor("get_stock_data", "AAPL", "abc", _GOOD)
        assert out == invalid_date_sentinel(
            "abc", what="stock price data", kind="window", param="start_date"
        )

    def test_news(self):
        out = interface.route_to_vendor("get_news", "AAPL", _GOOD, "abc")
        assert out == invalid_date_sentinel("abc", what="news", kind="window", param="end_date")

    def test_global_news(self):
        out = interface.route_to_vendor("get_global_news", "abc", None, None)
        assert out == invalid_date_sentinel("abc", what="global news", kind="point")

    def test_indicators(self):
        out = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "abc", 30)
        assert out == invalid_date_sentinel("abc", what="indicator values", kind="point")
