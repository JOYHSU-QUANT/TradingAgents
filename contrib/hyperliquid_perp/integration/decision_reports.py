"""The ``<payload>.reports.json`` sidecar: what the engine's agents wrote on the
way to the decision.

Each cycle runs the whole TradingAgents graph — the analysts fetch their data
and write reports at run time, the researchers debate, the trader plans, the
risk debate judges — and only the portfolio manager's ``final_trade_decision``
was read out of ``final_state``; the rest was dropped, so a past decision
could not be replayed: the input payload holds the perp snapshot and the
format text, not what the analysts saw that cycle (the replay plan's PR 0).
This module keeps those reports beside the payload under the sidecar contract
(:mod:`..common.sidecar`: not the payload, no row points at it, atomic, never
raises). The model is shown no different text, so ``PROMPT_VERSION`` and the
prompt regime's three keys are untouched.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..common.sidecar import write_sidecar

__all__ = ["REPORT_KEYS", "reports_record", "write_decision_reports"]

#: The ``final_state`` keys the sidecar keeps, in pipeline order: the four
#: analyst reports, the researcher debate and its verdict, the trader's plan,
#: the risk debate, and the decision text the parse seam reads. An allowlist,
#: not ``final_state`` minus a denylist, so the replay format has a declared
#: key set and ``messages`` (LangChain objects) stays out. ``past_context``
#: (the upstream memory log's injection) is left out too: the contrib does
#: not enable that log, and the key is not something an agent wrote this cycle.
REPORT_KEYS: tuple[str, ...] = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_debate_state",
    "investment_plan",
    "trader_investment_plan",
    "risk_debate_state",
    "final_trade_decision",
)


def reports_record(final_state: Mapping[str, object]) -> dict[str, object]:
    """The sidecar's shape: one entry per :data:`REPORT_KEYS`, ``None`` for a key
    the state lacks, values as the engine left them (strings, and the two
    debate states as dicts)."""
    return {key: final_state.get(key) for key in REPORT_KEYS}


def write_decision_reports(final_state: Mapping[str, object], *, payload_path: str | None) -> None:
    """Write ``<payload>.reports.json`` beside the input payload. Never raises."""
    write_sidecar(
        payload_path,
        suffix=".reports.json",
        record=reports_record(final_state),
        what="decision reports",
    )
