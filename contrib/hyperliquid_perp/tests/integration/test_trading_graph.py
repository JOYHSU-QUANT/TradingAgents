"""Tests for the perp-context injection seam in the trading-graph subclass.

The injection logic is split into the pure :func:`inject_perp_context` helper so
it can be exercised without importing the heavy engine (langchain/langgraph).
:func:`build_graph` itself is exercised against a stub base class injected into
``sys.modules`` — the wiring from the ``output_format_text`` kwarg down to
``resolve_instrument_context`` is the seam every real run depends on, and no
other test touches it (test_main stubs ``build_graph`` outright).
"""

from __future__ import annotations

import sys
import types

from contrib.hyperliquid_perp.integration.trading_graph import build_graph, inject_perp_context


def test_inject_appends_perp_section():
    out = inject_perp_context("BASE INSTRUMENT INFO", "funding z 1.4")
    assert out == "BASE INSTRUMENT INFO\n\n## Perpetual market context\nfunding z 1.4"


def test_inject_empty_perp_text_returns_base_unchanged():
    assert inject_perp_context("BASE", "") == "BASE"


def test_inject_preserves_base_when_perp_text_present():
    out = inject_perp_context("BASE", "x")
    assert out.startswith("BASE")
    assert "## Perpetual market context" in out


def test_inject_appends_output_format_at_the_tail():
    # The Phase 2 structured-output contract must be the LAST thing the engine
    # reads — after the base context and the perp snapshot.
    fmt = "## Required final decision output format\nreturn JSON"
    out = inject_perp_context("BASE", "funding z 1.4", fmt)
    assert out.startswith("BASE")
    assert out.index("## Perpetual market context") < out.index(fmt)
    assert out.endswith(fmt)


def test_inject_output_format_without_perp_text():
    out = inject_perp_context("BASE", "", "FORMAT")
    assert out == "BASE\n\nFORMAT"


def test_inject_empty_everything_returns_base_unchanged():
    assert inject_perp_context("BASE", "", "") == "BASE"


def _stub_engine_base(monkeypatch):
    """Install a lightweight ``TradingAgentsGraph`` so build_graph never imports
    the real langchain/langgraph tree. All three module levels go into
    ``sys.modules`` so the ``from tradingagents.graph.trading_graph import ...``
    inside build_graph resolves without touching the real package."""

    class _StubBase:
        def __init__(self, *, selected_analysts, debug, config):
            self.selected_analysts = selected_analysts
            self.debug = debug
            self.config = config

        def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
            return f"BASE[{ticker}/{asset_type}]"

    pkg = types.ModuleType("tradingagents")
    graph_pkg = types.ModuleType("tradingagents.graph")
    leaf = types.ModuleType("tradingagents.graph.trading_graph")
    leaf.TradingAgentsGraph = _StubBase
    monkeypatch.setitem(sys.modules, "tradingagents", pkg)
    monkeypatch.setitem(sys.modules, "tradingagents.graph", graph_pkg)
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", leaf)
    return _StubBase


def test_build_graph_wires_context_and_format_into_resolution(monkeypatch):
    # The regression this guards: dropping the output_format_text kwarg anywhere
    # along build_graph -> __init__ -> resolve_instrument_context would make every
    # real run parse-fail-closed to maintain_current with no test noticing.
    base_cls = _stub_engine_base(monkeypatch)

    graph = build_graph(
        perp_context_text="funding z 1.4",
        config={"k": "v"},
        selected_analysts=["market"],
        output_format_text="## Required final decision output format\nreturn JSON",
    )
    assert isinstance(graph, base_cls)
    assert graph.selected_analysts == ["market"]
    assert graph.config == {"k": "v"}

    resolved = graph.resolve_instrument_context("BTC", "crypto")
    assert resolved.startswith("BASE[BTC/crypto]")
    assert "## Perpetual market context\nfunding z 1.4" in resolved
    assert resolved.endswith("## Required final decision output format\nreturn JSON")
