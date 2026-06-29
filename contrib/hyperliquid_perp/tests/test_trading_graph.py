"""Tests for the perp-context injection seam in the trading-graph subclass.

The injection logic is split into the pure :func:`inject_perp_context` helper so
it can be exercised without importing the heavy engine (langchain/langgraph).
"""

from __future__ import annotations

from contrib.hyperliquid_perp.integration.trading_graph import inject_perp_context


def test_inject_appends_perp_section():
    out = inject_perp_context("BASE INSTRUMENT INFO", "funding z 1.4")
    assert out == "BASE INSTRUMENT INFO\n\n## Perpetual market context\nfunding z 1.4"


def test_inject_empty_perp_text_returns_base_unchanged():
    assert inject_perp_context("BASE", "") == "BASE"


def test_inject_preserves_base_when_perp_text_present():
    out = inject_perp_context("BASE", "x")
    assert out.startswith("BASE")
    assert "## Perpetual market context" in out
