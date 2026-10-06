"""The package's literals that spell an engine name, pinned to the engine's own symbols.

The agent layer reaches into the ``tradingagents`` engine by name: the
rating strings it reads out of the graph's signal, the ``final_state``
keys the sidecar keeps, the config keys it writes over the engine's
defaults, the analysts it sets the graph up with. A rename upstream would
not raise on our side: a rating would fail to parse on every verdict, a
report would be ``null`` in every sidecar, a config write would land on a
dead key. These pins turn each such rename into a red test naming the
literal that drifted. The engine is imported inside each test: its
``agents`` package pulls in every agent and langgraph on import, which
the rest of this suite, stdlib-only, should not wait on at collection.
"""

from __future__ import annotations

from pathlib import Path

from contrib.uniswap_v3.agent.graph import REPORT_KEYS, engine_config
from contrib.uniswap_v3.agent.settings import ANALYSTS, AgentSettings
from contrib.uniswap_v3.domain.verdicts import RATINGS, Rating

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_ratings_are_the_engines_five_tiers_and_its_review_signal():
    from tradingagents.agents.utils.rating import RATING_REVIEW, RATINGS_5_TIER

    assert [rating.value for rating in RATINGS] == list(RATINGS_5_TIER)
    assert Rating.REVIEW.value == RATING_REVIEW


def test_the_report_keys_the_sidecar_keeps_are_agent_state_fields():
    from tradingagents.agents.utils.agent_states import AgentState

    missing = set(REPORT_KEYS) - set(AgentState.__annotations__)
    assert not missing, f"no longer AgentState fields: {sorted(missing)}"


def test_the_engine_config_keys_written_exist_upstream(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG

    written = set(engine_config(AgentSettings(), tmp_path)) - set(DEFAULT_CONFIG)
    assert not written, f"not DEFAULT_CONFIG keys: {sorted(written)}"


def test_the_analysts_are_the_ones_the_graph_setup_names():
    # A text canary: the graph setup keys its analyst factories by these names.
    setup = (_REPO_ROOT / "tradingagents" / "graph" / "setup.py").read_text(encoding="utf-8")
    missing = [name for name in ANALYSTS if f'"{name}"' not in setup]
    assert not missing, f"not named in the graph setup: {missing}"
