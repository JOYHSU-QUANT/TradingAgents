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
- the instants and the interval table, so a ``candle_end`` is decoded by the
  same integer arithmetic that encoded it;
- the on-disk layout (``payload_dir``, ``sidecar_path``), to count how many
  questions already carry a ``.reports.json`` beside their payload;
- from the research package: the split and its holdout lock (plan §3-9),
  the cost model (plan §3-6), the research store that fills a missing
  cycle's later mark (plan §2-5), its numeric guards (one finiteness rule
  for every number a record carries), and the day in milliseconds the
  annualisation is built from.
"""

from __future__ import annotations

from contrib.autoresearch.constants import MS_PER_DAY
from contrib.autoresearch.costs import CostModel, FillRole, require_amount
from contrib.autoresearch.split import SegmentName, Split, SplitError
from contrib.autoresearch.store import ResearchStore, StoreError
from contrib.autoresearch.vocabulary import SpecError, require_number
from contrib.hyperliquid_perp.common.instants import epoch_ms, from_epoch_ms, parse_instant
from contrib.hyperliquid_perp.common.sidecar import sidecar_path
from contrib.hyperliquid_perp.common.store_layout import payload_dir
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskAction
from contrib.hyperliquid_perp.domains.perp.schema import interval_to_ms
from contrib.hyperliquid_perp.domains.perp.target_decision import DecisionMode, TargetSide
from contrib.hyperliquid_perp.paper.config import PaperTradingConfig
from contrib.hyperliquid_perp.persistence.db import Database, SchemaVersionError
from contrib.hyperliquid_perp.persistence.repository import TERMINAL_ATTEMPT_STATUSES, get_run

__all__ = [
    "BORROWED",
    "MS_PER_DAY",
    "TERMINAL_ATTEMPT_STATUSES",
    "UPSTREAM_PACKAGES",
    "CostModel",
    "Database",
    "DecisionMode",
    "FillRole",
    "MarketDataConfig",
    "PaperTradingConfig",
    "ResearchStore",
    "RiskAction",
    "SchemaVersionError",
    "SegmentName",
    "SpecError",
    "Split",
    "SplitError",
    "StoreError",
    "TargetSide",
    "epoch_ms",
    "from_epoch_ms",
    "get_run",
    "interval_to_ms",
    "parse_instant",
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
    ("contrib.hyperliquid_perp.common.instants", "epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "from_epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "parse_instant"),
    ("contrib.hyperliquid_perp.common.sidecar", "sidecar_path"),
    ("contrib.hyperliquid_perp.common.store_layout", "payload_dir"),
    ("contrib.hyperliquid_perp.domains.perp.market_data_config", "MarketDataConfig"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "RiskAction"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "interval_to_ms"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "DecisionMode"),
    ("contrib.hyperliquid_perp.domains.perp.target_decision", "TargetSide"),
    ("contrib.hyperliquid_perp.paper.config", "PaperTradingConfig"),
    ("contrib.hyperliquid_perp.persistence.db", "Database"),
    ("contrib.hyperliquid_perp.persistence.db", "SchemaVersionError"),
    ("contrib.hyperliquid_perp.persistence.repository", "TERMINAL_ATTEMPT_STATUSES"),
    ("contrib.hyperliquid_perp.persistence.repository", "get_run"),
)
