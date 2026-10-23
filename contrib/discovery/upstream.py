"""What this package borrows from the research package and the floor — in one place.

``contrib.discovery`` benchmarks ``contrib.autoresearch`` (discovery plan
§0): it builds synthetic worlds behind that package's ``HistoryMarketData``
port, runs that package's own ``search()`` with hypothesists of its own, and
judges the result from that package's ledger. So almost everything here is
a borrow from one neighbour, and the plan's architecture (§2) draws the
line in both directions: this package may import ``contrib.autoresearch``
and the shared floor ``contrib.core``, and NOTHING else under ``contrib/``
(not the perp trader, not replay, not carry) and not the engine
(``tradingagents``) — the only model this package will ever talk to is
reached through the research package's own ``ChatHypothesist`` (plan PR 5).

The borrow is funnelled the way the research package and replay funnel
theirs: every module here imports from THIS module, ``BORROWED`` is the
audit list, and ``tests/test_upstream.py`` parses the sources to hold both
halves — that no other module reaches out, and that neither upstream
package reaches back. ``__all__`` repeats the names as a literal on purpose:
ruff reads a literal ``__all__`` as a use of each import, and one derived
from ``BORROWED`` at runtime it cannot.

Not split into a light and a lazy half, as replay's engine borrow is. The
evaluator (``features``, ``evaluator``, ``research``, ``hypothesis``) drags
pandas in — half a second on import — and it is borrowed at module level
here. The research package keeps its ``report`` command off that cost and
tests it; nothing in THIS package runs without the evaluator — a world is
validated by measuring it, an arm is a search, and a verdict is read off a
search's report — so a split would buy nothing today. If a command that
only reads JSON (plan PR 4's ``report``) turns out to need one, the replay
pattern is one function away.

What is borrowed and why:

- the ledger's types and its gate (``Experiment``, ``Ledger``, ``Penalty``,
  ``Trial``, ``TrialStatus``, ``promotion_verdict``): the final verdict
  (plan §4.5) is the research package's own gate re-applied at the rule
  count the search ended with — never a second gate;
- the search's report and its interrupt sentence (``SearchReport``,
  ``INTERRUPTED``), so how an arm ended is read off the loop that ended it;
- the spec language (``StrategySpec``, ``Op``, ``Side``, ``MAX_HOLD_BARS``,
  ``parse_spec``, ``spec_hash``, ``spec_to_document``): a planted rule is a
  spec of that language — "expressible in the existing DSL" is the
  benchmark's scope (plan §0.3), and the parser is what decides it;
- the conditions a trial is measured under (``CostModel``, ``Split`` and
  its default shares, ``SegmentMetrics``), serialised through their own
  ``to_dict``/``from_dict`` so the manifest and the verdict snapshot write
  the records the ledger writes, not copies of them;
- the engine's indicator floor (``MIN_INDICATOR_LOOKBACK``), so a manifest
  names a window the frame would refuse where it is written;
- the evaluator's read path (``FeatureFrame``, ``evaluate_split`` and its
  ``SplitResult``, ``load_bundle``, ``require_clean_history``,
  ``require_measurable_tail``): the oracle scores the planted rule through
  the pure function ``calibrate`` uses, so nothing it does is filed (plan
  §1 #5), and checks the holdout's tail as ``promote`` does;
- the vocabulary the manifest validates its search space against
  (``parse_feature_name``) and its numeric guards (``require_number``,
  ``require_amount``, ``SpecError``), the studied intervals
  (``studied_interval``) and the day the world grid is laid on
  (``MS_PER_DAY``);
- from the floor, the ``VocabEnum`` base every vocabulary here is spelled in.
"""

from __future__ import annotations

from contrib.autoresearch.constants import MS_PER_DAY
from contrib.autoresearch.costs import CostModel, require_amount
from contrib.autoresearch.dsl import (
    MAX_HOLD_BARS,
    Op,
    Side,
    StrategySpec,
    parse_spec,
    spec_hash,
    spec_to_document,
)
from contrib.autoresearch.evaluator import SplitResult, evaluate_split, load_bundle
from contrib.autoresearch.features import MIN_INDICATOR_LOOKBACK, FeatureFrame
from contrib.autoresearch.hypothesis import INTERRUPTED, SearchReport
from contrib.autoresearch.ledger import (
    Experiment,
    Ledger,
    Penalty,
    Trial,
    TrialStatus,
    promotion_verdict,
)
from contrib.autoresearch.metrics import SegmentMetrics
from contrib.autoresearch.research import require_clean_history, require_measurable_tail
from contrib.autoresearch.split import (
    DEFAULT_TRAIN_SHARE,
    DEFAULT_VALIDATION_SHARE,
    Split,
    studied_interval,
)
from contrib.autoresearch.vocabulary import SpecError, parse_feature_name, require_number
from contrib.core.enum_guard import VocabEnum

__all__ = [
    "BORROWED",
    "DEFAULT_TRAIN_SHARE",
    "DEFAULT_VALIDATION_SHARE",
    "INTERRUPTED",
    "MAX_HOLD_BARS",
    "MIN_INDICATOR_LOOKBACK",
    "MS_PER_DAY",
    "UPSTREAM_PACKAGES",
    "CostModel",
    "Experiment",
    "FeatureFrame",
    "Ledger",
    "Op",
    "Penalty",
    "SearchReport",
    "SegmentMetrics",
    "Side",
    "SpecError",
    "Split",
    "SplitResult",
    "StrategySpec",
    "Trial",
    "TrialStatus",
    "VocabEnum",
    "evaluate_split",
    "load_bundle",
    "parse_feature_name",
    "parse_spec",
    "promotion_verdict",
    "require_amount",
    "require_clean_history",
    "require_measurable_tail",
    "require_number",
    "spec_hash",
    "spec_to_document",
    "studied_interval",
]

# The packages this one may import from, and no other: the plan's edge
# (§2), held by ``tests/test_upstream.py`` here and by the package graph in
# ``contrib/tests/test_package_graph.py``.
UPSTREAM_PACKAGES = ("contrib.autoresearch", "contrib.core")

# ``(module, attribute)`` for every borrowed name: the audit list. Every entry
# is checked to still exist upstream and to be the upstream object itself.
BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.autoresearch.constants", "MS_PER_DAY"),
    ("contrib.autoresearch.costs", "CostModel"),
    ("contrib.autoresearch.costs", "require_amount"),
    ("contrib.autoresearch.dsl", "MAX_HOLD_BARS"),
    ("contrib.autoresearch.dsl", "Op"),
    ("contrib.autoresearch.dsl", "Side"),
    ("contrib.autoresearch.dsl", "StrategySpec"),
    ("contrib.autoresearch.dsl", "parse_spec"),
    ("contrib.autoresearch.dsl", "spec_hash"),
    ("contrib.autoresearch.dsl", "spec_to_document"),
    ("contrib.autoresearch.evaluator", "SplitResult"),
    ("contrib.autoresearch.evaluator", "evaluate_split"),
    ("contrib.autoresearch.evaluator", "load_bundle"),
    ("contrib.autoresearch.features", "FeatureFrame"),
    ("contrib.autoresearch.features", "MIN_INDICATOR_LOOKBACK"),
    ("contrib.autoresearch.hypothesis", "INTERRUPTED"),
    ("contrib.autoresearch.hypothesis", "SearchReport"),
    ("contrib.autoresearch.ledger", "Experiment"),
    ("contrib.autoresearch.ledger", "Ledger"),
    ("contrib.autoresearch.ledger", "Penalty"),
    ("contrib.autoresearch.ledger", "Trial"),
    ("contrib.autoresearch.ledger", "TrialStatus"),
    ("contrib.autoresearch.ledger", "promotion_verdict"),
    ("contrib.autoresearch.metrics", "SegmentMetrics"),
    ("contrib.autoresearch.research", "require_clean_history"),
    ("contrib.autoresearch.research", "require_measurable_tail"),
    ("contrib.autoresearch.split", "DEFAULT_TRAIN_SHARE"),
    ("contrib.autoresearch.split", "DEFAULT_VALIDATION_SHARE"),
    ("contrib.autoresearch.split", "Split"),
    ("contrib.autoresearch.split", "studied_interval"),
    ("contrib.autoresearch.vocabulary", "SpecError"),
    ("contrib.autoresearch.vocabulary", "parse_feature_name"),
    ("contrib.autoresearch.vocabulary", "require_number"),
    ("contrib.core.enum_guard", "VocabEnum"),
)
