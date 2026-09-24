"""Contrib literals that spell an UPSTREAM name, pinned to the upstream symbol.

The contrib reaches into the unmodified ``tradingagents`` engine by name in a
few places: ``final_state`` keys, ``DEFAULT_CONFIG`` keys it writes, the
``asset_type`` value the crypto branches compare against. Each is a plain
string on our side, so an upstream rename would not raise — it would make a
``.get()`` return ``None`` (every cycle fails closed), a config write land on
a dead key (the engine keeps ITS default), or an ``==`` go quietly False (the
run falls back to the stock pipeline). The tests that fabricate engine output
spell the same literals, so they would stay green. These pins turn each such
rename into a red test naming the literal that drifted.
"""

from __future__ import annotations

import inspect

from contrib.hyperliquid_perp.domains.perp.target_decision import FINAL_TRADE_DECISION_KEY
from contrib.hyperliquid_perp.integration.decision_reports import REPORT_KEYS
from tradingagents.agents.utils import agent_utils
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.default_config import DEFAULT_CONFIG


def test_the_final_state_keys_the_contrib_reads_are_agent_state_fields():
    # The parse seam (main.py / integration/decision_provider.py) and the reports sidecar.
    ours = {FINAL_TRADE_DECISION_KEY, *REPORT_KEYS}
    missing = ours - set(AgentState.__annotations__)
    assert not missing, f"no longer AgentState fields: {sorted(missing)}"


def test_the_engine_config_keys_the_contrib_writes_exist_upstream():
    # engine_bridge._build_engine_config WRITES onto a dict(DEFAULT_CONFIG) copy.
    # A write is never a KeyError, so a renamed key would leave the engine on
    # its own default (structured_output back ON — the 2026-07-27 outage shape;
    # deep/quick_think_llm read the copy back only as an ``or`` fallback, so
    # with a model in YAML they never raise either). Of the eight keys it
    # writes, one is left out on purpose: ``max_tokens`` is refused by name
    # when absent (engine_bridge, EngineConfigError), so that rename is loud.
    # This proves the key still EXISTS upstream, not that upstream still
    # reads it — the cheap half of the guarantee.
    written = {
        "llm_provider",
        "deep_think_llm",
        "quick_think_llm",
        "backend_url",
        "structured_output",
        "llm_max_retries",
        "temperature",
    }
    missing = written - set(DEFAULT_CONFIG)
    assert not missing, f"no longer DEFAULT_CONFIG keys: {sorted(missing)}"


def test_the_asset_type_value_the_contrib_passes_is_the_one_upstream_branches_on():
    # ``propagate(..., asset_type="crypto")`` is compared upstream with ``==``;
    # a renamed value would silently select the stock pipeline for BTC.
    # A text canary, not a guarantee: it sees agent_utils only (the analysts
    # compare the same literal and are not scanned), and a reformat of the
    # comparison (single quotes, ``in {...}``, a helper) would go green while
    # the behaviour drifted. It catches the one drift that matters most —
    # the value itself changing — at zero cost.
    assert 'asset_type == "crypto"' in inspect.getsource(agent_utils)
