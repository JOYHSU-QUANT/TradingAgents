"""Graph node names that more than one layer has to spell identically.

A leaf on purpose: ``tradingagents.graph``'s package ``__init__`` imports the
whole engine (every agent factory, langgraph), so a consumer that needs one
node NAME — a callback handler attributing per-call metadata by langgraph's
``langgraph_node``, the perp adapter deciding which completion was the
decision — must be able to read it without paying that import. The graph's
own modules import from here too, so the string that ``add_node`` registers
under and the string a consumer compares against are one object.
"""

# The node that writes ``final_trade_decision`` — the one LLM call whose
# completion IS the decision. A routing target in ``graph.conditional_logic``,
# a registered node in ``graph.setup``, and the ``langgraph_node`` a callback
# handler sees for that call.
PORTFOLIO_MANAGER_NODE = "Portfolio Manager"

__all__ = ["PORTFOLIO_MANAGER_NODE"]
