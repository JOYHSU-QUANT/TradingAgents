"""yfinance memoizes ``Search`` process-wide, and the daemon's global news froze (#198).

``Search.search`` reads through ``YfData.cache_get`` — a ``functools.lru_cache``
on the library's singleton with no TTL — keyed on request parameters that carry
no date. ``get_global_news_yfinance`` therefore contacted Yahoo once per process
and served the same headlines to every later cycle until a restart. It now
forgets that memo before each call. Verified on yfinance 1.4.1, the pinned
floor; these pins run the real ``Search`` below the library's own swallow, with
only ``YfData.get`` (the wire) replaced.
"""

import json

import pytest
import yfinance as yf
import yfinance.data as yfdata

import tradingagents.dataflows.yfinance_news as ynews
from tradingagents.dataflows.config import get_config


@pytest.fixture(autouse=True)
def _forget_yfinance_memo():
    """The memo is process-global: start and leave every test here with it empty."""
    yfdata.YfData.cache_get.cache_clear()
    yield
    yfdata.YfData.cache_get.cache_clear()


class _Response:
    status_code = 200
    url = "https://query2.finance.yahoo.com/v1/finance/search"

    def __init__(self, body):
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


def _yahoo_counts_requests(monkeypatch):
    """Replace ``YfData.get`` — below ``cache_get`` — with one that counts and answers."""
    calls = []

    def get(self, url, params=None, timeout=30):
        calls.append(url)
        article = {
            "content": {"title": f"headline {len(calls)}", "pubDate": "2026-06-02T00:00:00Z"}
        }
        return _Response({"quotes": [], "news": [article]})

    monkeypatch.setattr(yfdata.YfData, "get", get)
    return calls


_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL"

# The getter stops querying once it holds ``limit`` articles; the widest limit
# it accepts keeps "one request per configured query" true however many
# queries the config grows to.
_NO_EARLY_EXIT = ynews.MAX_SEARCH_NEWS_COUNT


@pytest.mark.unit
def test_global_news_contacts_yahoo_on_every_call(monkeypatch):
    # Two calls, two days: each is a fresh set of requests, one per configured
    # query. Before #198 the second call was that many memo hits and zero
    # requests, so every daemon cycle after the first re-read the headlines
    # the first one fetched.
    calls = _yahoo_counts_requests(monkeypatch)
    queries = len(get_config()["global_news_queries"])

    ynews.get_global_news_yfinance("2026-06-03", look_back_days=7, limit=_NO_EARLY_EXIT)
    assert len(calls) == queries
    ynews.get_global_news_yfinance("2026-06-04", look_back_days=7, limit=_NO_EARLY_EXIT)
    assert len(calls) == 2 * queries


@pytest.mark.unit
def test_search_reads_through_the_memo_the_call_forgets(monkeypatch):
    # The premise, pinned: left alone, the library serves a repeated Search
    # from its memo. If a yfinance release stops doing that, this fails and
    # the forget in get_global_news_yfinance can go.
    calls = _yahoo_counts_requests(monkeypatch)

    yf.Search(query="Federal Reserve", news_count=10, enable_fuzzy_query=True)
    yf.Search(query="Federal Reserve", news_count=10, enable_fuzzy_query=True)

    assert len(calls) == 1


@pytest.mark.unit
def test_the_forget_empties_the_memo_without_disabling_it(monkeypatch):
    # What the call forgets is content, not the mechanism: afterwards a leaf
    # that reads through cache_get (history's past window, the timezone
    # lookup) is still served from the memo on its repeat within a cycle.
    calls = _yahoo_counts_requests(monkeypatch)
    ynews.get_global_news_yfinance("2026-06-03", look_back_days=7, limit=_NO_EARLY_EXIT)
    before = len(calls)

    data = yfdata.YfData()
    data.cache_get(url=_CHART_URL, params={"range": "1d", "interval": "1d"})
    data.cache_get(url=_CHART_URL, params={"range": "1d", "interval": "1d"})

    assert len(calls) == before + 1
