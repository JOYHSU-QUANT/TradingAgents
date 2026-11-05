"""A Yahoo news answer without the list is an outage, not "No news found" (#407).

Since yfinance 1.8.0 ``Ticker.get_news`` is a GraphQL POST read for
``data.lightyearList.main.edges`` alone; a refused query or a moved schema
answers HTTP 200 with ``errors`` and no ``data``, which the library turns
into an empty list — the quiet-week answer. These pins run the real
``get_news`` with only ``YfData.post`` (the wire) replaced, as the Search
cache pins do, and read what reaches the caller.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import tradingagents.dataflows.yfinance_news as ynews
from tradingagents.dataflows.errors import VendorUnavailableError

# The GraphQL asset as 1.8.0's ``_parse_news_asset`` reads it: a Story's
# summary, publish time, provider and links sit under ``contentAttributes``.
_ARTICLE = {
    "__typename": "Story",
    "id": "a1",
    "title": "Fed cuts",
    "contentAttributes": {
        "summary": "A cut.",
        "publishTime": "2026-11-04T12:00:00Z",
        "provider": {"displayName": "Reuters", "url": "https://r"},
        "canonicalUrl": "https://example.test/fed",
        "clickthroughUrl": "https://example.test/fed",
    },
}


def _list_body(edges):
    return {
        "data": {"lightyearList": {"main": {"edges": edges, "pageInfo": {"hasNextPage": False}}}}
    }


class _Response:
    status_code = 200
    url = "https://finance.yahoo.com/graphql"

    def __init__(self, body):
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def _wire(monkeypatch, body):
    import yfinance.data as yfdata

    posted = mock.Mock(return_value=_Response(body))
    monkeypatch.setattr(yfdata.YfData, "post", posted)
    return posted


@pytest.mark.unit
def test_an_article_on_the_wire_is_rendered(monkeypatch):
    _wire(monkeypatch, _list_body([{"node": {"asset": _ARTICLE}}]))
    out = ynews.get_news_yfinance("NVDA", "2026-11-01", "2026-11-05")
    assert "Fed cuts" in out and "Reuters" in out and "https://example.test/fed" in out


@pytest.mark.unit
def test_an_empty_list_on_the_wire_is_no_news(monkeypatch):
    _wire(monkeypatch, _list_body([]))
    assert ynews.get_news_yfinance("NVDA", "2026-11-01", "2026-11-05").startswith(
        "No news found for NVDA"
    )


@pytest.mark.unit
def test_an_error_body_without_the_list_is_an_outage_naming_the_version(monkeypatch):
    import yfinance

    _wire(monkeypatch, {"errors": [{"message": "Field 'lightyearList' is not defined"}]})
    with pytest.raises(VendorUnavailableError) as excinfo:
        ynews.get_news_yfinance("NVDA", "2026-11-01", "2026-11-05")
    text = str(excinfo.value)
    assert text.startswith("Yahoo Finance answered without data: ")
    assert "news GraphQL answered without the list: Field 'lightyearList'" in text
    assert text.endswith(f"(yfinance {yfinance.__version__})")


@pytest.mark.unit
def test_a_body_with_no_data_key_at_all_is_an_outage(monkeypatch):
    _wire(monkeypatch, {"unexpected": True})
    with pytest.raises(VendorUnavailableError, match="answered without the list"):
        ynews.get_news_yfinance("NVDA", "2026-11-01", "2026-11-05")


@pytest.mark.unit
def test_the_wire_is_read_through_the_router_as_the_optional_sentinel(monkeypatch):
    from tradingagents.dataflows import interface

    _wire(monkeypatch, {"errors": [{"message": "boom"}]})
    out = interface.route_to_vendor("get_news", "NVDA", "2026-11-01", "2026-11-05")
    assert out.startswith("DATA_UNAVAILABLE: optional news_data could not be retrieved")
    assert "answered without the list: boom" in out


@pytest.mark.unit
def test_the_singleton_post_is_put_back_after_the_call(monkeypatch):
    import yfinance.data as yfdata

    posted = _wire(monkeypatch, _list_body([]))
    ynews.get_news_yfinance("NVDA", "2026-11-01", "2026-11-05")
    data = yfdata.YfData()
    assert "post" not in vars(data)  # the class attribute (the mock) is what remains
    assert data.post is posted or data.post == posted
