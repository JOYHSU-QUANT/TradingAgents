"""The Google News vendor behind ``get_global_news`` (#394).

What the yfinance path could not do is pinned here as behaviour: every
configured query is searched and represented, the window is look-ahead
safe, and failures take the router's taxonomy so the optional news category
ends in its sentinel rather than a cycle abort.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from tests.conftest import fake_response
from tradingagents.dataflows import google_news, interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)

GOOD = "2026-11-04"


def _item(title, publisher="PBS", when="Sun, 01 Nov 2026 16:20:20 GMT", source=True):
    src = f"<source url='https://x'>{publisher}</source>" if source else ""
    pub = f"<pubDate>{when}</pubDate>" if when else ""
    return (
        f"<item><title>{title}</title>{src}{pub}"
        "<link>https://news.google.com/rss/articles/x</link></item>"
    )


def _feed(*items):
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        + "".join(items)
        + "</channel></rss>"
    ).encode()


def _serve(monkeypatch, feeds_by_query=None, default=b""):
    """Arm ``_request`` to answer each query from ``feeds_by_query`` (or
    ``default``), recording every (query, window) it was asked."""
    asked = []

    def _request(query, start_date, end_date):
        asked.append((query, start_date, end_date))
        return (feeds_by_query or {}).get(query, default)

    monkeypatch.setattr(google_news, "_request", _request)
    return asked


@pytest.mark.unit
class TestParse:
    def test_title_suffix_source_and_date_are_read(self):
        rows = google_news._parse_feed(_feed(_item("Fed holds rates - PBS")))
        assert rows == [
            {
                "title": "Fed holds rates",
                "publisher": "PBS",
                "pub_date": datetime(2026, 11, 1, 16, 20, 20, tzinfo=timezone.utc),
            }
        ]

    def test_the_suffix_is_only_removed_when_it_names_the_source(self):
        rows = google_news._parse_feed(_feed(_item("Fed holds rates - Reuters", publisher="PBS")))
        assert rows[0]["title"] == "Fed holds rates - Reuters"

    def test_a_title_that_is_just_the_publisher_keeps_its_text(self):
        # No `` - `` separator, so nothing is a suffix: shown as is.
        rows = google_news._parse_feed(_feed(_item("PBS", publisher="PBS")))
        assert rows[0]["title"] == "PBS"

    def test_a_feed_whose_items_all_lack_a_title_is_warned_about(self, caplog):
        with caplog.at_level("WARNING", logger="tradingagents.dataflows.google_news"):
            rows = google_news._parse_feed(_feed(_item(""), _item("   ")))
        assert rows == []
        assert "2 items and none had a readable title" in caplog.text

    def test_an_empty_feed_is_not_warned_about_by_the_parser(self, caplog):
        with caplog.at_level("WARNING", logger="tradingagents.dataflows.google_news"):
            assert google_news._parse_feed(_feed()) == []
        assert caplog.text == ""

    def test_untitled_items_are_skipped_and_missing_fields_are_marked(self):
        rows = google_news._parse_feed(
            _feed(_item("", source=False), _item("Only title", source=False, when=None))
        )
        assert rows == [
            {
                "title": "Only title",
                "publisher": google_news.SOURCE_UNAVAILABLE,
                "pub_date": None,
            }
        ]

    @pytest.mark.parametrize(
        "when",
        [
            "yesterday-ish",
            # A zone offset the calendar cannot hold: OverflowError, not ValueError.
            "Sun, 01 Nov 2026 16:20:20 +99999999999999999999",
            # A year ``to_utc`` cannot shift into UTC.
            "Fri, 31 Dec 9999 23:00:00 -0500",
        ],
    )
    def test_an_unreadable_date_is_undated_not_a_crash(self, when):
        rows = google_news._parse_feed(_feed(_item("T", when=when)))
        assert rows[0]["pub_date"] is None

    def test_a_non_utc_date_is_normalised_to_utc(self):
        rows = google_news._parse_feed(_feed(_item("T", when="Thu, 05 Nov 2026 02:00:00 +0900")))
        assert rows[0]["pub_date"] == datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)

    def test_vendor_text_is_flattened_and_capped(self):
        hostile = "## Heading\n| cell |" + "x" * 400
        rows = google_news._parse_feed(_feed(_item(hostile, publisher="a\nb")))
        assert "\n" not in rows[0]["title"] and "|" not in rows[0]["title"]
        assert len(rows[0]["title"]) <= 203 and rows[0]["title"].endswith("...")
        assert rows[0]["publisher"] == "a b"

    def test_the_suffix_is_removed_before_the_cap(self):
        # Cut first, a long title would keep a truncated tail of the suffix
        # and the publisher would show twice.
        rows = google_news._parse_feed(
            _feed(_item("x" * 210 + " - Bloomberg", publisher="Bloomberg"))
        )
        assert rows[0]["title"] == "x" * 200 + "..."
        assert rows[0]["publisher"] == "Bloomberg"

    @pytest.mark.parametrize("body", [b"<html>consent</html><", b"<rss><nochannel/></rss>"])
    def test_a_body_that_is_not_a_feed_is_an_outage(self, body):
        with pytest.raises(google_news.GoogleNewsUnavailableError) as info:
            google_news._parse_feed(body)
        assert isinstance(info.value, VendorUnavailableError)


@pytest.mark.unit
class TestInterleave:
    def _rows(self, *titles):
        return [{"title": t, "publisher": "p", "pub_date": None} for t in titles]

    def test_every_query_is_represented_in_turn(self):
        chosen = google_news._interleave(
            [self._rows("a1", "a2", "a3"), self._rows("b1", "b2"), self._rows("c1")], limit=5
        )
        assert [r["title"] for r in chosen] == ["a1", "b1", "c1", "a2", "b2"]

    def test_the_same_headline_is_shown_once_case_insensitively(self):
        chosen = google_news._interleave(
            [self._rows("Binance hack", "x"), self._rows("binance HACK", "y")], limit=10
        )
        assert [r["title"] for r in chosen] == ["Binance hack", "x", "y"]

    def test_the_limit_bounds_the_total_not_each_query(self):
        chosen = google_news._interleave([self._rows("a1", "a2"), self._rows("b1", "b2")], limit=3)
        assert len(chosen) == 3

    def test_no_rows_is_no_choice(self):
        assert google_news._interleave([[], []], limit=3) == []
        assert google_news._interleave([], limit=3) == []


@pytest.mark.unit
class TestGetGlobalNews:
    def test_one_feed_per_configured_query_over_the_window(self, monkeypatch):
        set_config({"global_news_queries": ["q one", "q two"], "global_news_lookback_days": 3})
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        out = google_news.get_global_news(GOOD)
        # The operator days are padded one each side of the UTC window
        # (Google reads them as Pacific days); ``in_window`` trims them back.
        assert asked == [
            ("q one", "2026-10-31", "2026-11-06"),
            ("q two", "2026-10-31", "2026-11-06"),
        ]
        assert out.startswith("## Global Market News, from 2026-11-01 to 2026-11-04:")
        assert "### T (source: PBS)\nPublished: 2026-11-01\n" in out

    def test_the_later_queries_reach_the_report(self, monkeypatch):
        # The property #394 is about: with the limit filled by the first
        # query alone, the yfinance loop stopped; here every query is in.
        set_config({"global_news_queries": ["a", "b", "c"], "global_news_article_limit": 3})
        _serve(
            monkeypatch,
            {
                "a": _feed(_item("A1"), _item("A2"), _item("A3"), _item("A4")),
                "b": _feed(_item("B1")),
                "c": _feed(_item("C1")),
            },
        )
        out = google_news.get_global_news(GOOD)
        assert [line for line in out.splitlines() if line.startswith("### ")] == [
            "### A1 (source: PBS)",
            "### B1 (source: PBS)",
            "### C1 (source: PBS)",
        ]

    def test_feed_order_is_kept_within_a_query(self, monkeypatch):
        # Google ranks a search feed by relevance; an older story that ranks
        # first stays first, it is not re-sorted behind a newer reprint.
        set_config({"global_news_queries": ["a"]})
        _serve(
            monkeypatch,
            {
                "a": _feed(
                    _item("Old but first", when="Thu, 29 Oct 2026 10:00:00 GMT"),
                    _item("Newer", when="Tue, 03 Nov 2026 10:00:00 GMT"),
                )
            },
        )
        out = google_news.get_global_news(GOOD)
        assert out.index("Old but first") < out.index("Newer")

    def test_items_after_the_as_of_date_are_dropped(self, monkeypatch):
        # The query carries ``before:``; should a feed ignore it, the window
        # — not the feed — is still the as-of bound.
        set_config({"global_news_queries": ["a"], "global_news_lookback_days": 7})
        _serve(
            monkeypatch,
            {
                "a": _feed(
                    _item("In", when="Sat, 31 Oct 2026 12:00:00 GMT"),
                    _item("Future", when="Fri, 06 Nov 2026 12:00:00 GMT"),
                )
            },
        )
        out = google_news.get_global_news("2026-11-01")
        assert "### In " in out and "Future" not in out

    def test_the_as_of_day_is_in_and_the_next_utc_day_is_out(self, monkeypatch):
        # The feed's operators are Pacific days and padded; the UTC bound is
        # the client filter's: the as-of day's last second is in, the next
        # day's first second is out.
        set_config({"global_news_queries": ["a"], "global_news_lookback_days": 6})
        _serve(
            monkeypatch,
            {
                "a": _feed(
                    _item("Last second", when="Sun, 01 Nov 2026 23:59:59 GMT"),
                    _item("Next day", when="Mon, 02 Nov 2026 00:00:00 GMT"),
                    _item("Start day", when="Mon, 26 Oct 2026 00:00:00 GMT"),
                    _item("Before start", when="Sun, 25 Oct 2026 23:59:59 GMT"),
                )
            },
        )
        out = google_news.get_global_news("2026-11-01")
        assert "### Last second" in out and "### Start day" in out
        assert "Next day" not in out and "Before start" not in out

    def test_items_before_the_window_are_dropped(self, monkeypatch):
        set_config({"global_news_queries": ["a"], "global_news_lookback_days": 2})
        _serve(monkeypatch, {"a": _feed(_item("Stale", when="Sun, 25 Oct 2026 12:00:00 GMT"))})
        assert (
            google_news.get_global_news(GOOD)
            == "No global news found between 2026-11-02 and 2026-11-04"
        )

    def test_an_undated_item_is_kept_only_when_the_window_reaches_today(self, monkeypatch):
        set_config({"global_news_queries": ["a"]})
        _serve(monkeypatch, {"a": _feed(_item("Undated", when=None))})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert "### Undated" in google_news.get_global_news(today)
        past = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        assert google_news.get_global_news(past).startswith("No global news found between")

    def test_empty_feeds_say_so_for_the_day_and_in_the_log(self, monkeypatch, caplog):
        set_config({"global_news_queries": ["a", "b"]})
        _serve(monkeypatch, default=_feed())
        with caplog.at_level("INFO", logger="tradingagents.dataflows.google_news"):
            assert google_news.get_global_news(GOOD) == f"No global news found for {GOOD}"
        assert "'a': 0 fetched, 0 in window; 'b': 0 fetched, 0 in window" in caplog.text
        assert "answered no items for any of the 2 queries" in caplog.text

    def test_each_query_s_counts_are_logged(self, monkeypatch, caplog):
        set_config({"global_news_queries": ["a", "b"], "global_news_lookback_days": 2})
        _serve(
            monkeypatch,
            {
                "a": _feed(_item("In"), _item("Old", when="Sun, 25 Oct 2026 12:00:00 GMT")),
                "b": _feed(),
            },
        )
        with caplog.at_level("INFO", logger="tradingagents.dataflows.google_news"):
            google_news.get_global_news("2026-11-02")
        assert "'a': 2 fetched, 1 in window; 'b': 0 fetched, 0 in window" in caplog.text
        assert "answered no items" not in caplog.text

    def test_a_limit_below_the_query_count_is_warned_about(self, monkeypatch, caplog):
        set_config({"global_news_queries": ["a", "b", "c"], "global_news_article_limit": 2})
        _serve(monkeypatch, default=_feed(_item("T")))
        with caplog.at_level("WARNING", logger="tradingagents.dataflows.google_news"):
            google_news.get_global_news(GOOD)
        assert "limit 2 is below the 3 configured queries" in caplog.text

    def test_an_as_of_day_the_calendar_cannot_pad_is_this_vendor_s_refusal(self, monkeypatch):
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        with pytest.raises(google_news.GoogleNewsError, match="too late"):
            google_news.get_global_news("9999-12-31")
        assert asked == []

    def test_the_call_s_own_window_and_limit_win_over_the_config(self, monkeypatch):
        set_config(
            {
                "global_news_queries": ["a"],
                "global_news_lookback_days": 7,
                "global_news_article_limit": 10,
            }
        )
        asked = _serve(monkeypatch, {"a": _feed(_item("1"), _item("2"), _item("3"))})
        out = google_news.get_global_news(GOOD, look_back_days=5, limit=2)
        assert asked == [("a", "2026-10-29", "2026-11-06")]
        assert out.count("### ") == 2

    def test_an_outage_on_any_query_is_the_verdict_on_the_call(self, monkeypatch):
        set_config({"global_news_queries": ["a", "b"]})
        asked = []

        def _request(query, start_date, end_date):
            asked.append(query)
            if query == "b":
                raise google_news.GoogleNewsUnavailableError("down")
            return _feed(_item("A1"))

        monkeypatch.setattr(google_news, "_request", _request)
        with pytest.raises(google_news.GoogleNewsUnavailableError):
            google_news.get_global_news(GOOD)
        assert asked == ["a", "b"]

    @pytest.mark.parametrize(
        "config, guard",
        [
            ({"global_news_queries": []}, "global news configuration"),
            ({"global_news_queries": ["ok", "  "]}, "global news configuration"),
            ({"global_news_queries": ("a",)}, "global news configuration"),
            ({"global_news_lookback_days": 0}, "global news lookback window"),
            ({"global_news_article_limit": "ten"}, "global news article limit"),
        ],
    )
    def test_a_configured_value_of_the_wrong_shape_names_its_guard(
        self, monkeypatch, config, guard
    ):
        set_config(config)
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        with pytest.raises(WiringGapError, match=guard):
            google_news.get_global_news(GOOD)
        assert asked == []

    def test_a_window_the_calendar_cannot_hold_is_this_vendor_s_refusal(self, monkeypatch):
        # The family's window arithmetic (#380): typed, not a bare OverflowError.
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        with pytest.raises(google_news.GoogleNewsError, match="too large"):
            google_news.get_global_news(GOOD, look_back_days=10**9)
        assert asked == []

    def test_a_limit_below_one_is_one(self, monkeypatch):
        set_config({"global_news_queries": ["a"]})
        _serve(monkeypatch, {"a": _feed(_item("1"), _item("2"))})
        assert google_news.get_global_news(GOOD, limit=0).count("### ") == 1

    def test_queries_are_stripped_before_the_search(self, monkeypatch):
        set_config({"global_news_queries": ["  spaced  "]})
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        google_news.get_global_news(GOOD)
        assert asked == [("spaced", "2026-10-27", "2026-11-06")]


@pytest.mark.unit
class TestRequestBoundary:
    def _get(self, monkeypatch, response=None, side_effect=None):
        if response is not None:
            response.close = mock.Mock()
        getter = mock.Mock(return_value=response, side_effect=side_effect)
        monkeypatch.setattr(google_news.requests, "get", getter)
        return getter

    def _ok(self, body, chunk=64 * 1024):
        response = fake_response(200)
        response.iter_content = lambda chunk_size: (
            body[i : i + chunk] for i in range(0, len(body), chunk)
        )
        return response

    def test_the_feed_is_asked_for_the_query_over_the_window_in_the_us_edition(self, monkeypatch):
        getter = self._get(monkeypatch, self._ok(b"<rss/>"))
        assert google_news._request("tariffs OR sanctions", "2026-10-28", "2026-11-05") == b"<rss/>"
        kwargs = getter.call_args.kwargs
        assert getter.call_args.args == (google_news.FEED_URL,)
        assert kwargs["params"] == {
            "q": "tariffs OR sanctions after:2026-10-28 before:2026-11-05",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        assert kwargs["timeout"] == google_news.REQUEST_TIMEOUT
        assert kwargs["headers"]["User-Agent"].startswith("tradingagents/")
        assert kwargs["stream"] is True

    def test_a_429_is_the_latched_rate_limit_type(self, monkeypatch):
        self._get(monkeypatch, fake_response(429))
        with pytest.raises(google_news.GoogleNewsRateLimitError) as info:
            google_news._request("q", "2026-10-28", "2026-11-05")
        assert isinstance(info.value, VendorRateLimitError) and info.value.latches_vendor

    def test_a_5xx_is_the_outage_type_and_the_connection_is_released(self, monkeypatch):
        response = fake_response(503)
        self._get(monkeypatch, response)
        with pytest.raises(VendorUnavailableError):
            google_news._request("q", "2026-10-28", "2026-11-05")
        response.close.assert_called_once_with()

    def test_an_unreached_vendor_is_the_outage_type(self, monkeypatch):
        self._get(monkeypatch, side_effect=requests.ConnectionError("refused"))
        with pytest.raises(google_news.GoogleNewsUnavailableError) as info:
            google_news._request("q", "2026-10-28", "2026-11-05")
        assert "ConnectionError" in str(info.value)

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refusal_of_this_client_is_the_outage_type(self, monkeypatch, status):
        # Google refusing the server's address is the realistic failure from
        # a datacenter IP (#395 at StockTwits): the router reads a raw 401/403
        # as the vendor being down, and this boundary says the same.
        self._get(monkeypatch, fake_response(status))
        with pytest.raises(google_news.GoogleNewsUnavailableError) as info:
            google_news._request("q", "2026-10-28", "2026-11-05")
        assert str(status) in str(info.value)

    def test_a_4xx_answer_about_the_request_stays_the_module_type(self, monkeypatch):
        self._get(monkeypatch, fake_response(404))
        with pytest.raises(google_news.GoogleNewsError) as info:
            google_news._request("q", "2026-10-28", "2026-11-05")
        assert not isinstance(info.value, VendorUnavailableError)
        assert "404" in str(info.value)

    def test_a_transport_failure_mid_stream_is_the_outage_type(self, monkeypatch):
        # The read loop is inside the boundary: a body that breaks halfway is
        # the vendor unreached, not an untyped error past the ``except``.
        def _chunks(chunk_size):
            yield b"<rss>"
            raise requests.ConnectionError("reset mid-body")

        response = fake_response(200)
        response.iter_content = _chunks
        self._get(monkeypatch, response)
        with pytest.raises(google_news.GoogleNewsUnavailableError):
            google_news._request("q", "2026-10-28", "2026-11-05")
        response.close.assert_called_once_with()

    def test_an_oversized_body_is_refused_while_streaming(self, monkeypatch):
        # Read in chunks under the cap: the refusal comes before the whole
        # body is in memory, not after ``.content`` has buffered it.
        chunks_read = []

        def _chunks(chunk_size):
            for i in range(200):
                chunks_read.append(i)
                yield b"x" * (1024 * 1024)

        response = fake_response(200)
        response.iter_content = _chunks
        self._get(monkeypatch, response)
        with pytest.raises(google_news.GoogleNewsError, match="exceeded"):
            google_news._request("q", "2026-10-28", "2026-11-05")
        assert len(chunks_read) == 6


@pytest.mark.unit
class TestRouting:
    def test_global_news_is_routed_to_google_news_and_ticker_news_is_not(self):
        assert interface.get_vendor("news_data", "get_global_news") == "google_news"
        assert interface.get_vendor("news_data", "get_news") == "yfinance"
        assert interface.get_vendor("news_data", "get_insider_transactions") == "yfinance"
        assert (
            interface.VENDOR_METHODS["get_global_news"]["google_news"]
            is google_news.get_global_news
        )
        assert "google_news" in interface.VENDOR_LIST

    def test_the_default_route_asks_google_news_once_per_query(self, monkeypatch):
        asked = _serve(monkeypatch, default=_feed(_item("Headline")))
        out = interface.route_to_vendor("get_global_news", GOOD, None, None)
        assert [q for q, _s, _e in asked] == interface.get_config()["global_news_queries"]
        assert "### Headline (source: PBS)" in out

    def test_a_module_failure_ends_in_the_optional_news_sentinel_too(self, monkeypatch):
        # The plain module type (an oversized body, a 404) is not an outage,
        # but news is optional: the chain still ends in the sentinel, naming
        # this vendor and its words.
        monkeypatch.setattr(
            google_news,
            "_request",
            mock.Mock(side_effect=google_news.GoogleNewsError("Google News feed exceeded")),
        )
        out = interface.route_to_vendor("get_global_news", GOOD, None, None)
        assert out.startswith("DATA_UNAVAILABLE: optional news_data could not be retrieved")
        assert "google_news" in out and "feed exceeded" in out

    def test_the_category_off_switch_still_covers_this_tool(self, monkeypatch):
        # ``tool_vendors`` routes the tool to Google News, but the category's
        # "none" outranks it: the documented off switch for news (#393) must
        # not leave this one tool live.
        set_config({"data_vendors": {"news_data": "none"}})
        asked = _serve(monkeypatch, default=_feed(_item("T")))
        assert interface.is_category_disabled("news_data", "get_global_news")
        out = interface.route_to_vendor("get_global_news", GOOD, None, None)
        assert out.startswith("DATA_UNAVAILABLE: optional news_data is disabled by configuration")
        assert asked == []

    def test_an_outage_ends_in_the_optional_news_sentinel(self, monkeypatch):
        monkeypatch.setattr(
            google_news,
            "_request",
            mock.Mock(side_effect=google_news.GoogleNewsUnavailableError("Google News is down")),
        )
        out = interface.route_to_vendor("get_global_news", GOOD, None, None)
        assert out.startswith("DATA_UNAVAILABLE: optional news_data could not be retrieved")
        assert "google_news" in out and "Google News is down" in out
