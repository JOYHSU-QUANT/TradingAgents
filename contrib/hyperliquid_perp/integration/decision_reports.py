"""The ``<payload>.reports.json`` sidecar: what the agents wrote on the way to the decision.

Each cycle runs the whole TradingAgents graph — the analysts fetch their data
and write reports at run time, the researchers debate, the trader plans, the
risk debate judges — and only the portfolio manager's ``final_trade_decision``
was read out of ``final_state``; the rest was dropped, so a past decision
could not be replayed: the input payload holds the perp snapshot and the
format text, not what the analysts saw that cycle (the replay plan's PR 0).
This module keeps those reports beside the payload under the sidecar contract
(:mod:`..common.sidecar`: not the payload, no row points at it, schema-stamped,
atomic, never raises). The model is shown no different text, so
``PROMPT_VERSION`` and the prompt regime's three keys are untouched.

Written only for a cycle that got a dict ``final_state`` back: an engine run
that raised, or returned a shape the parse cannot read (the daemon records
both as ``api_failed``), has nothing to record, so a ``.usage.json`` with no
``.reports.json`` beside it is an engine-failed cycle, not a lost write —
unless that cycle's log carries the ``decision reports sidecar could not be
written`` ERROR, the other way to the same pairing short of a kill landing
between the two writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..common.sidecar import write_sidecar
from ..domains.perp.target_decision import FINAL_TRADE_DECISION_KEY

__all__ = ["REPORT_KEYS", "reports_record", "write_decision_reports"]

#: The ``final_state`` keys the sidecar keeps, in pipeline order: the four
#: analyst reports, the researcher debate and its verdict, the trader's plan,
#: the risk debate, and the decision text the parse seam reads. An allowlist,
#: not ``final_state`` minus a denylist, so the replay format has a declared
#: key set and ``messages`` (LangChain objects) stays out. ``past_context``
#: (the upstream memory log's injection) is left out too: the contrib does
#: not enable that log, and the key is not something an agent wrote this cycle.
#: These spell upstream ``AgentState``'s field names; ``tests/test_upstream_names.py``
#: pins them to it so a rename there fails CI instead of quietly recording
#: ``null`` forever. The last one is the parse seam's own key, shared.
REPORT_KEYS: tuple[str, ...] = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_debate_state",
    "investment_plan",
    "trader_investment_plan",
    "risk_debate_state",
    FINAL_TRADE_DECISION_KEY,
)


def reports_record(
    final_state: Mapping[str, object], *, selected_analysts: Sequence[str]
) -> dict[str, object]:
    """The sidecar's shape: ``selected_analysts`` — which analysts this cycle's
    engine was configured with — then one entry per :data:`REPORT_KEYS`,
    ``None`` for a key the state lacks, values as the engine left them
    (strings, and the two debate states as dicts).

    ``selected_analysts`` is what makes a ``null`` report legible: under the
    default ``[market, social, news]`` the fundamentals report is ``null`` on
    every cycle, and nothing in the store says so per cycle."""
    return {
        "selected_analysts": list(selected_analysts),
        **{key: final_state.get(key) for key in REPORT_KEYS},
    }


def write_decision_reports(
    final_state: Mapping[str, object],
    *,
    payload_path: str | None,
    selected_analysts: Sequence[str],
) -> None:
    """Write ``<payload>.reports.json`` beside the input payload, when the run has one. Never raises."""
    write_sidecar(
        payload_path,
        suffix=".reports.json",
        what="decision reports",
        build=lambda: reports_record(final_state, selected_analysts=selected_analysts),
    )
