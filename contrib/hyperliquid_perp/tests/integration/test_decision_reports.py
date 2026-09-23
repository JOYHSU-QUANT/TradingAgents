"""``<payload>.reports.json``: the agents' reports kept beside the input payload.

The sidecar mechanics (schema stamp, atomic, never raises, no payload path →
nothing) are ``common.sidecar``'s and tested there; this file pins what THIS
sidecar holds.
"""

from __future__ import annotations

import json

from contrib.hyperliquid_perp.common.sidecar import SIDECAR_SCHEMA
from contrib.hyperliquid_perp.integration.decision_reports import (
    REPORT_KEYS,
    reports_record,
    write_decision_reports,
)
from tradingagents.agents.utils.agent_states import AgentState

_ANALYSTS = ["market", "social", "news"]

_FULL_STATE = {
    "company_of_interest": "BTC",  # not a report: must not leak into the record
    "messages": [object()],  # LangChain objects: deliberately outside the allowlist
    "market_report": "market says up",
    "sentiment_report": "sentiment says meh",
    "news_report": "news says nothing",
    "fundamentals_report": "",
    "investment_debate_state": {
        "bull_history": "bull",
        "bear_history": "bear",
        "history": "bull\nbear",
        "current_response": "bear",
        "judge_decision": "hold",
        "count": 2,
    },
    "investment_plan": "hold",
    "trader_investment_plan": "hold, small",
    "risk_debate_state": {"judge_decision": "approve", "count": 3},
    "final_trade_decision": '{"decision_mode": "maintain_current"}',
}


def test_every_report_key_is_a_field_of_upstream_agent_state():
    # The allowlist spells upstream names; an upstream rename must fail here,
    # not quietly record null for that key on every cycle from then on.
    missing = set(REPORT_KEYS) - set(AgentState.__annotations__)
    assert not missing, f"REPORT_KEYS no longer match AgentState: {sorted(missing)}"


def test_the_record_is_the_analyst_set_then_the_nine_keys_in_pipeline_order_and_nothing_else():
    record = reports_record(_FULL_STATE, selected_analysts=_ANALYSTS)

    assert list(record) == ["selected_analysts", *REPORT_KEYS]
    assert record["selected_analysts"] == ["market", "social", "news"]
    assert record["market_report"] == "market says up"
    assert record["investment_debate_state"]["count"] == 2
    assert record["risk_debate_state"] == {"judge_decision": "approve", "count": 3}
    assert record["final_trade_decision"] == '{"decision_mode": "maintain_current"}'
    assert "company_of_interest" not in record
    assert "messages" not in record


def test_a_key_the_state_lacks_is_null_not_an_error():
    record = reports_record({"market_report": "only this"}, selected_analysts=_ANALYSTS)

    assert record["market_report"] == "only this"
    assert record["news_report"] is None
    assert record["final_trade_decision"] is None
    assert list(record) == [
        "selected_analysts",
        *REPORT_KEYS,
    ]  # every key present, missing ones null


def test_write_puts_the_stamped_record_beside_the_payload_as_reports_json(tmp_path):
    payload = tmp_path / "BTC-20260315T000000_000000Z.json"
    payload.write_bytes(b"{}")

    write_decision_reports(_FULL_STATE, payload_path=str(payload), selected_analysts=_ANALYSTS)

    sidecar = tmp_path / "BTC-20260315T000000_000000Z.reports.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "schema": SIDECAR_SCHEMA,
        **reports_record(_FULL_STATE, selected_analysts=_ANALYSTS),
    }
    assert payload.read_bytes() == b"{}"  # the hash-locked payload is untouched
