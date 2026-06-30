"""``HyperliquidTradingGraph`` — drive the engine with perp context injected.

Direction 2: ``tradingagents/`` is a black box. The one override point we need
is :meth:`resolve_instrument_context`, which the base class injects into the
initial state so the perp snapshot "reaches the whole graph" (every analyst, the
trader, the portfolio manager). See ``docs/INTEGRATION.md`` part 1.

The base ``TradingAgentsGraph`` lives under ``tradingagents/`` and is imported
lazily inside :func:`build_graph` so importing this module (and running the
``--context-only`` path or the pure-function tests) never requires the heavy
engine dependencies — they are only needed for a real engine run.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # import only for type checkers; avoids the heavy dep at runtime
    from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)


def inject_perp_context(base: str, perp_context_text: str) -> str:
    """Append the perp snapshot to the engine's base instrument context.

    Pure string assembly, split out so it is unit-testable without importing the
    heavy engine. An empty ``perp_context_text`` leaves ``base`` untouched.
    """
    if not perp_context_text:
        return base
    return f"{base}\n\n## Perpetual market context\n{perp_context_text}"


def build_graph(
    *,
    perp_context_text: str,
    config: dict[str, Any],
    selected_analysts: list[str],
    debug: bool = False,
) -> TradingAgentsGraph:
    """Construct a :class:`HyperliquidTradingGraph`.

    Defined as a factory so the ``tradingagents`` import — and therefore the
    ``langchain``/``langgraph`` dependency tree — is only pulled in when an
    actual engine run is requested, keeping ``--context-only`` import-light.
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    class HyperliquidTradingGraph(TradingAgentsGraph):
        """Drive the unmodified engine with Hyperliquid perp context injected."""

        def __init__(self, *args: Any, perp_context_text: str = "", **kwargs: Any) -> None:
            self._perp_context_text = perp_context_text
            super().__init__(*args, **kwargs)

        def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
            try:
                base = super().resolve_instrument_context(ticker, asset_type)
            except Exception:
                # The base engine resolves instrument context (its own I/O / lookups).
                # Log the ticker so a failure here is identifiable at the injection seam,
                # then re-raise unchanged — wrapping it in a new type would hide the
                # original exception from any caller matching on a specific type.
                logger.exception("base resolve_instrument_context failed for %r", ticker)
                raise
            return inject_perp_context(base, self._perp_context_text)

    return HyperliquidTradingGraph(
        selected_analysts=selected_analysts,
        debug=debug,
        config=config,
        perp_context_text=perp_context_text,
    )
