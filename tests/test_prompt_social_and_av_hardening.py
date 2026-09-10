"""The social sources and the Alpha Vantage indicator getter cannot forge
report structure.

The third and last batch of #233, and the one where the payload is genuinely
anyone's to write: StockTwits messages and Reddit posts are authored by whoever
posted them, and neither source is a routed tool. The sentiment analyst fetches
both directly and pastes the two blocks into its own SYSTEM message, beside the
"## Data sources" headings that message writes for itself — so an unflattened
title or body opens a heading in the highest-privilege text of the run rather
than in a tool result.

Two subjects, as in the first two batches: a value the CALLER supplied and a
getter quotes back takes ``utils.echo_argument``; a VENDOR's own text about to
be rendered takes ``utils.sanitize_untrusted``. The Alpha Vantage indicator
values add the third rule this series arrived at in PR #252 — a cell the report
presents as a NUMBER is checked for its shape before and after flattening,
because flattening "4.1|" into "4.1" turns a value the reader would have
questioned into a reading the vendor never sent.

The whole-report shape assertion is imported rather than copied: two suites
proving "nothing was forged" against two definitions of forged is how the
second batch first passed against unguarded code.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timedelta
from unittest import mock

import pytest

import tradingagents.dataflows.alpha_vantage_indicator as avi
import tradingagents.dataflows.reddit as rdt
import tradingagents.dataflows.stocktwits as stw
import tradingagents.dataflows.y_finance as yfn
from tests.test_prompt_vendor_field_hardening import (
    FORGED,
    SURVIVES,
    _assert_report_shape_unchanged,
)

CLEAN_TEXT = "earnings beat, stock pops"

# An edge-marker argument, for the keep_edges promise: a ticker named as
# unusable must not come back stripped into the spelling that IS usable.
EDGED_TICKER = "_NVDA_"


# ---------------------------------------------------------------------------
# StockTwits
# ---------------------------------------------------------------------------


def _day(days_back: int) -> str:
    """The freshness lane is anchored on the wall clock, as the source is."""
    return (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _resp(payload):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    return _Resp()


def _message(**overrides):
    msg = {
        "created_at": "2026-05-20T14:30:00Z",
        "user": {"username": "trader1"},
        "entities": {"sentiment": {"basic": "Bullish"}},
        "body": CLEAN_TEXT,
    }
    msg.update(overrides)
    return msg


def _stocktwits(payload, ticker="NVDA", **kwargs):
    with mock.patch.object(stw, "urlopen", return_value=_resp(payload)):
        return stw.fetch_stocktwits_messages(ticker, **kwargs)


def _stream(*messages):
    return {"messages": list(messages)}


@pytest.mark.unit
class TestStockTwitsMessageFields:
    """Every field of a message is its author's text, and every one of them
    lands on a line of a block the analyst reads as its own system prompt."""

    @pytest.mark.parametrize("field", ["body", "created_at"])
    def test_a_forged_message_field_cannot_forge_structure(self, field):
        payload = FORGED if field == "body" else f"2026-05-20T{FORGED}"
        forged = _stocktwits(_stream(_message(**{field: payload})))
        clean = _stocktwits(_stream(_message()))
        # A forged stamp is REPLACED (see the stamp tests below), so only the
        # body promises its words survive.
        _assert_report_shape_unchanged(forged, clean, survives=field == "body")

    def test_a_forged_handle_cannot_forge_structure(self):
        forged = _stocktwits(_stream(_message(user={"username": FORGED})))
        clean = _stocktwits(_stream(_message()))
        _assert_report_shape_unchanged(forged, clean)

    def test_a_handle_with_nothing_left_to_show_takes_the_unknown_marker(self):
        # "##" flattens to nothing. An empty handle must not render as a bare
        # "@", which reads as a real account whose name failed to load.
        out = _stocktwits(_stream(_message(user={"username": "##"})))
        assert "[unknown user]" in out
        assert "@ " not in out

    def test_a_body_is_bounded_even_when_the_vendor_sends_a_wall(self):
        out = _stocktwits(_stream(_message(body="x" * 5000)))
        line = [ln for ln in out.splitlines() if ln.startswith("[")][0]
        assert len(line) < stw.MAX_BODY_CHARS + 120
        assert line.endswith("...")

    def test_clean_messages_still_render_byte_for_byte(self):
        out = _stocktwits(_stream(_message()))
        assert f"[2026-05-20T14:30:00Z · @trader1 · Bullish] {CLEAN_TEXT}" in out


@pytest.mark.unit
class TestStockTwitsStamp:
    """The stamp is read twice — once to render it, once to decide whether the
    stream has stalled — and #233 is the rule that those two must be one
    value. A stamp flattened into a plausible date would sit beside a message
    the freshness check had skipped."""

    @pytest.mark.parametrize("stamp", ["2026-05-2#0T14:30:00Z", "yesterday", 20260520, None])
    def test_a_stamp_whose_date_cannot_be_read_is_named_not_repaired(self, stamp):
        out = _stocktwits(_stream(_message(created_at=stamp)))
        assert stw.TIME_UNKNOWN in out
        assert "2026-05-2" not in out

    def test_the_day_the_note_names_is_one_a_rendered_message_shows(self):
        stale = f"{_day(30)}T14:30:00Z"
        out = _stocktwits(_stream(_message(created_at=stale)), curr_date=_day(0))
        assert out.startswith("_Data lag")
        assert _day(30) in out.splitlines()[0]
        assert f"[{stale} ·" in out

    def test_an_unreadable_stamp_is_not_counted_as_the_newest_message(self):
        # The stalled stream must still be disclosed when a second message
        # carries an unreadable stamp: a value the render refused must not be
        # able to answer "how fresh is this stream" either.
        today = _day(0)
        out = _stocktwits(
            _stream(
                _message(created_at=f"{_day(30)}T14:30:00Z"),
                # Today's date with a marker inside it: unreadable raw, and
                # still unreadable after flattening turns the "#" into a space.
                _message(created_at=f"{today[:9]}#{today[9:]}T14:30:00Z"),
            ),
            curr_date=today,
        )
        assert out.startswith("_Data lag")
        assert _day(30) in out.splitlines()[0]


@pytest.mark.unit
class TestStockTwitsArgumentEcho:
    """Three sentences quote the ticker back at its author."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"messages": []},
            {"messages": "not-a-list"},
            {"symbol": {"symbol": "AAPL"}, "messages": [_message()]},
        ],
        ids=["no-messages", "unexpected-shape", "symbol-mismatch"],
    )
    def test_a_forged_ticker_cannot_forge_structure(self, payload):
        forged = _stocktwits(payload, ticker=FORGED)
        clean = _stocktwits(payload, ticker="NVDA")
        _assert_report_shape_unchanged(forged, clean)

    @pytest.mark.parametrize(
        "payload",
        [
            {"messages": []},
            {"messages": "not-a-list"},
            {"symbol": {"symbol": "AAPL"}, "messages": [_message()]},
        ],
        ids=["no-messages", "unexpected-shape", "symbol-mismatch"],
    )
    def test_an_edge_marker_ticker_does_not_come_back_stripped(self, payload):
        out = _stocktwits(payload, ticker=EDGED_TICKER)
        assert "NVDA" in out
        assert "$NVDA>" not in out and "requested NVDA," not in out

    def test_a_forged_symbol_echo_from_the_vendor_cannot_forge_structure(self):
        # The mismatch sentence names BOTH spellings, and the second one is
        # the vendor's, not the caller's.
        forged = _stocktwits({"symbol": {"symbol": FORGED}, "messages": [_message()]})
        clean = _stocktwits({"symbol": {"symbol": "AAPL"}, "messages": [_message()]})
        _assert_report_shape_unchanged(forged, clean)

    def test_a_clean_ticker_still_reads_byte_for_byte(self):
        assert _stocktwits({"messages": []}) == "<no StockTwits messages found for $NVDA>"


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

_POST_EPOCH = calendar.timegm(datetime(2026, 5, 20, 14, 30).timetuple())


def _post(**overrides):
    post = {
        "title": f"NVDA {CLEAN_TEXT}",
        "score": None,
        "num_comments": None,
        "created_utc": _POST_EPOCH,
        "selftext": "the datacenter unit carried the quarter",
        "source": "rss",
    }
    post.update(overrides)
    return post


def _reddit(*per_sub, ticker="NVDA"):
    """The block for one list of posts per subreddit, in order.

    Two subreddits, one of them empty, is the only shape that renders the
    per-subreddit empty sentence: with every subreddit empty the function
    answers the nothing-anywhere sentence instead, and the two quote the
    ticker back from different places.
    """
    subs = tuple(f"sub{i}" for i in range(len(per_sub)))
    with mock.patch.object(rdt, "_fetch_subreddit", side_effect=[list(p) for p in per_sub]):
        return rdt.fetch_reddit_posts(ticker, subreddits=subs, inter_request_delay=0)


# One case per sentence that quotes the ticker back.
_SUB_CASES = [([],), ([_post()],), ([], [_post()])]
_SUB_IDS = ["nothing-anywhere", "with-posts", "one-empty-subreddit"]


@pytest.mark.unit
class TestRedditPostFields:
    """Both free-text fields are the poster's own words. The title had no
    length bound at all before #233, so one post could bury the block."""

    @pytest.mark.parametrize("field", ["title", "selftext"])
    def test_a_forged_post_field_cannot_forge_structure(self, field):
        forged = _reddit([_post(**{field: FORGED})])
        clean = _reddit([_post()])
        _assert_report_shape_unchanged(forged, clean)

    def test_a_title_is_bounded_even_when_the_vendor_sends_a_wall(self):
        out = _reddit([_post(title="x" * 5000)])
        title_line = [ln for ln in out.splitlines() if ln.startswith("  [")][0]
        assert len(title_line) < 300
        assert title_line.endswith("...")

    def test_a_title_with_nothing_left_to_show_is_named_not_left_blank(self):
        out = _reddit([_post(title="###")])
        assert rdt.TITLE_UNAVAILABLE in out

    def test_an_empty_excerpt_still_omits_the_excerpt_line(self):
        out = _reddit([_post(selftext="")])
        assert "body excerpt" not in out

    def test_clean_posts_still_render_byte_for_byte(self):
        out = _reddit([_post()])
        assert f"[2026-05-20] NVDA {CLEAN_TEXT}" in out
        assert "body excerpt: the datacenter unit carried the quarter" in out


@pytest.mark.unit
class TestRedditArgumentEcho:
    """Three sentences quote the ticker back: the per-subreddit header, the
    per-subreddit empty answer, and the nothing-anywhere answer."""

    @pytest.mark.parametrize("case", _SUB_CASES, ids=_SUB_IDS)
    def test_a_forged_ticker_cannot_forge_structure(self, case):
        _assert_report_shape_unchanged(_reddit(*case, ticker=FORGED), _reddit(*case))

    @pytest.mark.parametrize("case", _SUB_CASES, ids=_SUB_IDS)
    def test_an_edge_marker_ticker_does_not_come_back_stripped(self, case):
        out = _reddit(*case, ticker=EDGED_TICKER)
        assert "NVDA" in out
        assert "mentioning NVDA " not in out

    def test_a_clean_ticker_still_reads_byte_for_byte(self):
        assert _reddit([]) == "<no Reddit posts found mentioning NVDA across r/sub0 in the past 7 days>"
        assert "sub0: <no posts found mentioning NVDA in the past 7 days>" in _reddit([], [_post()])


# ---------------------------------------------------------------------------
# Alpha Vantage indicator values
# ---------------------------------------------------------------------------

_CLEAN_CSV = "time,RSI\n2026-05-29,55.0\n2026-05-30,56.0"


def _av(csv, indicator="rsi", curr_date="2026-06-01", look_back_days=30):
    with mock.patch.object(avi, "_make_api_request", return_value=csv):
        return avi.get_indicator("AAPL", indicator, curr_date, look_back_days)


def _value_lines(report: str) -> list[str]:
    return [ln for ln in report.splitlines() if re.match(r"^\d{4}-\d{2}-\d{2}: ", ln)]


@pytest.mark.unit
class TestAlphaVantageIndicatorValues:
    """The CSV cell is a raw vendor string presented to the analyst as a
    number. Nothing between the response and the report coerces it."""

    @pytest.mark.parametrize(
        "value",
        ["4.1|", "nan", "inf", "", "n/a", "55.0#", FORGED, str(10**400)],
    )
    def test_a_cell_that_is_not_a_number_is_dropped_rather_than_repaired(self, value):
        out = _av(f"time,RSI\n2026-05-29,{value}\n2026-05-30,56.0")
        assert _value_lines(out) == ["2026-05-30: 56.0"]
        assert "2026-05-29" not in out
        # The repair is the failure this guard exists for: a flattened "4.1|"
        # renders as a reading, and nothing downstream can tell it from one.
        assert "4.1" not in out
        assert SURVIVES not in out

    def test_a_dropped_row_is_disclosed_rather_than_left_as_a_gap(self):
        out = _av("time,RSI\n2026-05-28,4.1|\n2026-05-29,x\n2026-05-30,56.0")
        assert "Alpha Vantage served 2 row(s) in this window whose value could not be read" in out

    def test_a_dated_row_missing_its_value_column_is_a_row_the_window_lost(self):
        out = _av("time,RSI\n2026-05-29\n2026-05-30,56.0")
        assert "1 row(s) in this window whose value could not be read" in out
        assert _value_lines(out) == ["2026-05-30: 56.0"]

    def test_an_unreadable_date_is_counted_but_not_charged_to_the_window(self):
        # This request sends no outputsize, so the CSV is the whole history: a
        # row nothing can date must be named as such rather than counted as one
        # the window lost.
        out = _av("time,RSI\n2026-05-2#9,55.0\n2026-05-30,56.0")
        assert "1 row(s) whose date could not be read at all" in out
        assert "in this window" not in out
        assert _value_lines(out) == ["2026-05-30: 56.0"]

    @pytest.mark.parametrize("row", ["2020-01-01,55.0", "2020-01-01,4.1|", "2020-01-01"])
    def test_a_row_outside_the_window_is_never_called_unusable(self, row):
        # A short window is the caller's doing. History this call did not ask
        # for cannot make the window look damaged, whatever shape it is in.
        out = _av(f"time,RSI\n{row}\n2026-05-30,56.0")
        assert "Alpha Vantage served" not in out
        assert _value_lines(out) == ["2026-05-30: 56.0"]

    def test_every_row_in_the_window_unusable_does_not_blame_the_window(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError) as info:
            _av("time,RSI\n2026-05-29,4.1|\n2026-05-30,nan")
        assert "no usable rsi rows between" in str(info.value)
        assert "2 row(s) in this window whose value could not be read" in str(info.value)

    def test_an_undatable_row_does_not_turn_an_empty_window_into_a_damaged_one(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError) as info:
            _av("time,RSI\n2020-01-01,55.0\n2026-05-2#9,55.0")
        detail = str(info.value)
        assert "1 row(s) whose date could not be read at all" in detail
        assert "in this window" not in detail

    def test_an_empty_window_still_says_so_in_its_own_words(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError) as info:
            _av("time,RSI\n2020-01-01,55.0")
        assert "no rsi rows between" in str(info.value)

    def test_clean_values_still_render_byte_for_byte(self):
        out = _av(_CLEAN_CSV)
        assert _value_lines(out) == ["2026-05-29: 55.0", "2026-05-30: 56.0"]
        assert "Alpha Vantage served" not in out


# ---------------------------------------------------------------------------
# The indicator heading, on both vendors of the one routed tool
# ---------------------------------------------------------------------------


def _av_with_menu_entry(indicator, csv=_CLEAN_CSV):
    """Report for an indicator the menu accepts, whatever its spelling.

    The membership check bounds the name to this module's table today, so the
    heading guard is only reachable through the table. Patching it is the
    honest way to test the guard: it proves the heading cannot forge whatever
    passes the check, without claiming the shipped menu holds such a name.
    """
    with (
        mock.patch.object(
            avi, "_SUPPORTED_INDICATORS", {**avi._SUPPORTED_INDICATORS, indicator: ("RSI", "close")}
        ),
        mock.patch.object(
            avi,
            "_INDICATOR_REQUESTS",
            {**avi._INDICATOR_REQUESTS, indicator: avi._INDICATOR_REQUESTS["rsi"]},
        ),
        mock.patch.object(avi, "_CSV_COLUMN_MAP", {**avi._CSV_COLUMN_MAP, indicator: "RSI"}),
        mock.patch.object(
            avi,
            "_INDICATOR_DESCRIPTIONS",
            {**avi._INDICATOR_DESCRIPTIONS, indicator: "a description"},
        ),
        mock.patch.object(avi, "_make_api_request", return_value=csv),
    ):
        return avi.get_indicator("AAPL", indicator, "2026-06-01", 30)


def _yfinance_with_menu_entry(indicator):
    with (
        mock.patch.object(
            yfn, "INDICATOR_DESCRIPTIONS", {**yfn.INDICATOR_DESCRIPTIONS, indicator: "a desc"}
        ),
        mock.patch.object(yfn, "_get_stock_stats_bulk", return_value={"2026-06-01": "55.0"}),
    ):
        return yfn.get_stock_stats_indicators_window("AAPL", indicator, "2026-06-01", 2)


@pytest.mark.unit
class TestIndicatorHeadingOnBothVendors:
    """One routed tool, two vendors. #219 is the rule that they must not end
    alike on a clean spelling and differently on a hostile one, so the two
    headings take the guard together."""

    @pytest.mark.parametrize(
        "report",
        [_av_with_menu_entry, _yfinance_with_menu_entry],
        ids=["alpha-vantage", "yfinance"],
    )
    def test_a_forged_indicator_cannot_forge_a_second_heading(self, report):
        forged, clean = report(FORGED), report("rsi")
        assert sum(1 for ln in forged.splitlines() if ln.lstrip().startswith("#")) == sum(
            1 for ln in clean.splitlines() if ln.lstrip().startswith("#")
        )
        assert "|" not in forged.splitlines()[0]

    @pytest.mark.parametrize(
        "report",
        [_av_with_menu_entry, _yfinance_with_menu_entry],
        ids=["alpha-vantage", "yfinance"],
    )
    def test_an_edge_marker_indicator_does_not_come_back_stripped(self, report):
        heading = report("_rsi_").splitlines()[0]
        assert "_rsi_" not in heading
        assert "## RSI values" not in heading and "## rsi values" not in heading

    @pytest.mark.parametrize(
        "report,expected",
        [
            (_av_with_menu_entry, "## RSI values from"),
            (_yfinance_with_menu_entry, "## rsi values from"),
        ],
        ids=["alpha-vantage", "yfinance"],
    )
    def test_a_clean_indicator_still_reads_byte_for_byte(self, report, expected):
        assert report("rsi").startswith(expected)
