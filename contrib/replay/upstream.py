"""What this package borrows from its two neighbours — in one place, read-only.

``contrib.replay`` is the one package under ``contrib/`` that imports both
``contrib.hyperliquid_perp`` and ``contrib.autoresearch`` (replay plan §3-1).
The alternative — a ``score`` subcommand inside the perp package's
``cli/offline.py`` — would have copied the research package's cost constants
into a second home, and the C1 decision that keeps the two packages apart is
about the TRADING path (the prompt's research-signal section is handed over
as a JSON document, never as an import); an offline tool that never reaches
the prompt is not that path.

The borrow is funnelled the way ``contrib.autoresearch.upstream`` funnels
its own: every module here imports from THIS module, ``BORROWED`` is the
audit list, and ``tests/test_upstream.py`` parses the sources to hold both
halves — that no other module reaches out, and that neither neighbour
reaches back. A borrowed name that vanishes upstream fails the pin test by
name, not as an ImportError deep in a command. ``__all__`` repeats the
names as a literal on purpose: ruff reads a literal ``__all__`` as a use of
each import, and one derived from ``BORROWED`` at runtime it cannot.

What is borrowed and why:

- the decision vocabulary (``DecisionMode``, ``TargetSide``, ``RiskAction``),
  so a scorecard reads a row's ``set_target`` / ``clamped`` / ``long`` with
  the definitions that wrote it, and the attempt-status vocabulary, so
  "finished" means what the daemon means by it;
- the store (``Database``, ``SchemaVersionError``, ``get_run``) and the
  paper config (``PaperTradingConfig``), because the cost a run was
  measured under is the run's own fill model (plan §3-6);
- the instants and the interval table, so a decision's ``timestamp`` is
  decoded by the same integer arithmetic that encoded it;
- the on-disk layout (``payload_dir``, ``sidecar_path``), to count how many
  questions already carry a ``.reports.json`` beside their payload;
- from the research package: the split and its holdout lock (plan §3-9)
  with the intervals it can be cut on, the cost model (plan §3-6), the
  research store that fills a missing cycle's later mark (plan §2-5), its
  numeric guards (one finiteness rule for every number a record carries),
  and the day in milliseconds the annualisation is built from;
- for the past papers (plan PR 2): the parse seam and the gate
  (``parse_target_decision``, ``evaluate`` and the types they take and
  return), so a replayed answer is judged by the functions that judged the
  recorded one; the payload digest, so a payload is checked against the
  hash its input row recorded before it is sent anywhere; the prompt
  assembly (``inject_perp_context``), so the replayed message spells the
  context the way the engine did; and the decimal context the books are
  multiplied in.

The engine half (the LLM client factory, the message types, the
completion collector) is borrowed LAZILY: it is listed in
``ENGINE_BORROWED`` and imported only by :func:`load_engine`, because it
pulls in ``langchain_core`` and a scorecard, a dry run and every test with
a fake model must not pay for that. ``tradingagents`` is the one package
outside ``contrib/`` borrowed from, and only through that function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contrib.autoresearch.constants import MS_PER_DAY, STUDIED_INTERVALS
from contrib.autoresearch.costs import CostModel, FillRole, require_amount
from contrib.autoresearch.split import SegmentName, Split, SplitError
from contrib.autoresearch.store import ResearchStore, StoreError
from contrib.autoresearch.vocabulary import SpecError, require_number
from contrib.hyperliquid_perp.common.decimal_context import DECIMAL_CONTEXT
from contrib.hyperliquid_perp.common.digest import payload_digest
from contrib.hyperliquid_perp.common.instants import epoch_ms, from_epoch_ms, parse_instant
from contrib.hyperliquid_perp.common.sidecar import sidecar_path
from contrib.hyperliquid_perp.common.store_layout import payload_dir
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.risk_gate import (
    CurrentPositionState,
    RiskAction,
    RiskConfig,
    RiskGateResult,
    evaluate,
)
from contrib.hyperliquid_perp.domains.perp.schema import interval_to_ms
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    DecisionMode,
    TargetSide,
    parse_target_decision,
)
from contrib.hyperliquid_perp.integration.trading_graph import inject_perp_context
from contrib.hyperliquid_perp.paper.config import PaperTradingConfig
from contrib.hyperliquid_perp.persistence.db import Database, SchemaVersionError
from contrib.hyperliquid_perp.persistence.repository import TERMINAL_ATTEMPT_STATUSES, get_run

__all__ = [
    "BORROWED",
    "DECIMAL_CONTEXT",
    "ENGINE_BORROWED",
    "MS_PER_DAY",
    "STUDIED_INTERVALS",
    "TERMINAL_ATTEMPT_STATUSES",
    "UPSTREAM_PACKAGES",
    "CostModel",
    "CurrentPositionState",
    "Database",
    "DecisionConfig",
    "DecisionMode",
    "Engine",
    "FillRole",
    "MarketDataConfig",
    "PaperTradingConfig",
    "ResearchStore",
    "RiskAction",
    "RiskConfig",
    "RiskGateResult",
    "SchemaVersionError",
    "SegmentName",
    "SpecError",
    "Split",
    "SplitError",
    "StoreError",
    "TargetSide",
    "epoch_ms",
    "evaluate",
    "from_epoch_ms",
    "get_run",
    "inject_perp_context",
    "interval_to_ms",
    "load_engine",
    "parse_instant",
    "parse_target_decision",
    "payload_digest",
    "payload_dir",
    "require_amount",
    "require_number",
    "sidecar_path",
]

# The packages this one may name at all. Both, and only these two: the
# reverse edge (either of them naming this package) is held shut by
# ``tests/test_upstream.py``.
UPSTREAM_PACKAGES: tuple[str, ...] = ("contrib.hyperliquid_perp", "contrib.autoresearch")

# Every borrow as ``(dotted module, attribute)`` — the audit list, and the
# sequence the pin test walks. ``tests/test_upstream.py`` holds it equal to
# the imports above and to ``__all__``.
BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.autoresearch.constants", "MS_PER_DAY"),
    ("contrib.autoresearch.constants", "STUDIED_INTERVALS"),
    ("contrib.autoresearch.costs", "CostModel"),
    ("contrib.autoresearch.costs", "FillRole"),
    ("contrib.autoresearch.costs", "require_amount"),
    ("contrib.autoresearch.split", "SegmentName"),
    ("contrib.autoresearch.split", "Split"),
    ("contrib.autoresearch.split", "SplitError"),
    ("contrib.autoresearch.store", "ResearchStore"),
    ("contrib.autoresearch.store", "StoreError"),
    ("contrib.autoresearch.vocabulary", "SpecError"),
    ("contrib.autoresearch.vocabulary", "require_number"),
    ("contrib.hyperliquid_perp.common.decimal_context", "DECIMAL_CONTEXT"),
    ("contrib.hyperliquid_perp.common.digest", "payload_digest"),
    ("contrib.hyperliquid_perp.common.instants", "epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "from_epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "parse_instant"),
    ("contrib.hyperliquid_perp.common.sidecar", "sidecar_path"),
    ("contrib.hyperliquid_perp.common.store_layout", "payload_dir"),
    ("contrib.hyperliquid_perp.domains.perp.market_data_config", "MarketDataConfig"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "CurrentPositionState"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "RiskAction"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "RiskConfig"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "RiskGateResult"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "evaluate"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "interval_to_ms"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "DecisionConfig"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "DecisionMode"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "TargetSide"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "parse_target_decision"),
    ("contrib.hyperliquid_perp.integration.trading_graph", "inject_perp_context"),
    ("contrib.hyperliquid_perp.paper.config", "PaperTradingConfig"),
    ("contrib.hyperliquid_perp.persistence.db", "Database"),
    ("contrib.hyperliquid_perp.persistence.db", "SchemaVersionError"),
    ("contrib.hyperliquid_perp.persistence.repository", "TERMINAL_ATTEMPT_STATUSES"),
    ("contrib.hyperliquid_perp.persistence.repository", "get_run"),
)

# The engine half, borrowed lazily (module docstring): ``(dotted module,
# attribute)`` like ``BORROWED``, imported only by :func:`load_engine`. The
# pin test imports every entry, so a name that vanishes upstream still fails
# by name, just not on every import of this package.
ENGINE_BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.hyperliquid_perp.integration.completion_usage", "CompletionUsageCollector"),
    ("langchain_core.messages", "HumanMessage"),
    ("langchain_core.messages", "SystemMessage"),
    ("tradingagents.llm_clients", "create_llm_client"),
    ("tradingagents.llm_clients.base_client", "normalize_content"),
)


@dataclass(frozen=True)
class Engine:
    """The lazily borrowed engine names, one attribute per ``ENGINE_BORROWED`` entry."""

    CompletionUsageCollector: Any
    HumanMessage: Any
    SystemMessage: Any
    create_llm_client: Any
    normalize_content: Any


def load_engine() -> Engine:
    """Import the engine half of the borrow: the only place this package reaches the engine.

    Function-local on purpose (module docstring). An engine that is not
    installed raises ``ImportError`` here, and the caller words it.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from contrib.hyperliquid_perp.integration.completion_usage import CompletionUsageCollector
    from tradingagents.llm_clients import create_llm_client
    from tradingagents.llm_clients.base_client import normalize_content

    return Engine(
        CompletionUsageCollector=CompletionUsageCollector,
        HumanMessage=HumanMessage,
        SystemMessage=SystemMessage,
        create_llm_client=create_llm_client,
        normalize_content=normalize_content,
    )
