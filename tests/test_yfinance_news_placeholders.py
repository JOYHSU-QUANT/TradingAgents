"""Missing yfinance news fields must render as explicit unavailability
markers, not fabricated values ("No title" / a publisher named "Unknown")
that read as authoritative data (#31)."""

from __future__ import annotations

import pytest

from tradingagents.dataflows.yfinance_news import _extract_article_data


@pytest.mark.unit
class TestNestedContentPlaceholders:
    def test_missing_title_and_provider_get_markers(self):
        data = _extract_article_data({"content": {"summary": "body"}})
        assert data["title"] == "(title unavailable)"
        assert data["publisher"] == "(source unavailable)"

    def test_empty_string_fields_get_markers(self):
        # Present-but-empty is just as missing as an absent key.
        data = _extract_article_data({"content": {"title": "", "provider": {"displayName": ""}}})
        assert data["title"] == "(title unavailable)"
        assert data["publisher"] == "(source unavailable)"

    def test_real_fields_pass_through(self):
        data = _extract_article_data(
            {"content": {"title": "Fed cuts", "provider": {"displayName": "Reuters"}}}
        )
        assert data["title"] == "Fed cuts"
        assert data["publisher"] == "Reuters"


@pytest.mark.unit
class TestFlatArticlePlaceholders:
    def test_missing_title_and_publisher_get_markers(self):
        data = _extract_article_data({"summary": "body"})
        assert data["title"] == "(title unavailable)"
        assert data["publisher"] == "(source unavailable)"

    def test_real_fields_pass_through(self):
        data = _extract_article_data({"title": "Fed cuts", "publisher": "Reuters"})
        assert data["title"] == "Fed cuts"
        assert data["publisher"] == "Reuters"
