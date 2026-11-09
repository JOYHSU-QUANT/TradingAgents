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
search's report — so a split would buy nothing today. ``calibrate`` is
the one command that only reads JSON (the manifest and the two records)
and still pays the import; if it, or plan PR 4's ``report``, turns out to
need the split, the replay pattern is one function away.

What is borrowed and why:

- the ledger's types and its gate (``Experiment``, ``Ledger``, ``Penalty``,
  ``Trial``, ``TrialStatus``, ``promotion_verdict`` and its ``Verdict``): the final verdict
  (plan §4.5) is the research package's own gate re-applied at the rule
  count the search ended with — never a second gate;
- the search loop itself (``search``), its report and its interrupt
  sentence (``SearchReport``, ``INTERRUPTED``), so every arm is one run of
  the research package's own loop (plan §1 #2) and how it ended is read
  off the loop that ended it, and the one error the loop stops on
  (``HypothesistError``), which a candidate pool walked to its end raises
  (plan §4.3: the research package is not taught about pools); the port an
  arm implements (``Hypothesist``) is structural and is not borrowed —
  ``search()`` takes anything that answers ``propose``;
- the one promotion an arm makes (``promote``): the trial the final verdict
  selects is promoted through the research package's own verb, which is
  what reads the holdout for it, after the verdict is snapshotted;
- the spec language (``StrategySpec``, ``Family``, ``Op``, ``Side``,
  ``MAX_HOLD_BARS``, ``parse_spec``, ``spec_hash``, ``spec_to_document``): a
  planted rule is a spec of that language — "expressible in the existing
  DSL" is the benchmark's scope (plan §0.3), and the parser is what
  decides it — and a candidate of the pool is written as one, labelled
  with the family its features earn;
- the conditions a trial is measured under (``CostModel``, ``Split`` and
  its default shares, ``SegmentMetrics`` and the window names
  ``SegmentName`` a figure is filed under), serialised through their own
  ``to_dict``/``from_dict`` so the manifest and the verdict snapshot write
  the records the ledger writes, not copies of them;
- the engine's indicator floor (``MIN_INDICATOR_LOOKBACK``), so a manifest
  names a window the frame would refuse where it is written;
- the evaluator's read path (``FeatureFrame``, ``evaluate_split`` and its
  ``SplitResult``, ``load_bundle``, ``require_clean_history``,
  ``require_measurable_tail``): the oracle scores the planted rule through
  the pure function the research package's own ``calibrate`` uses, so
  nothing it does is filed (plan
  §1 #5), and checks the holdout's tail as ``promote`` does; the scorer
  reads a rule's positions off the same result's ``Trade`` records and its
  structure off the language's ``Condition`` and ``FeatureRef``;
- the vocabulary the manifest validates its search space against
  (``parse_feature_name``), the declarations behind it (``FeatureKind``,
  ``FeatureUnit``, ``SeriesSource``, ``spec_of``: which series a
  candidate's features read decides the family label it is written under,
  and which features are categorical decides which grids take labels) and
  its numeric guards
  (``require_number``, ``require_amount``, ``SpecError``), the studied
  intervals (``studied_interval``) and the day the world grid is laid on
  (``MS_PER_DAY``);
- what a world is made of and lands through (plan PR 2): the port's own
  value types (``Candle``, ``FundingPoint``, reached as the research package
  reaches them, through its ``upstream``), the grid facts a generated series
  is held to (``DAILY_INTERVAL``, ``FUNDING_INTERVAL_MS``, ``bar_span_ms``,
  ``interval_to_ms``), the replay that says which bars a planted rule holds
  a position on (``replay_sides``, over a ``SeriesBundle``), the research
  package's own walk into its own store (``backfill_candles``,
  ``backfill_funding`` and their ``SeriesFetch``/``StopReason``/
  ``describe_stop``/``render_fetch``, and ``FUNDING_PAGE_DAYS``, how far
  past a clock that walk's last window reaches), the store itself (``ResearchStore``,
  ``StoreError``, and ``default_db_path`` — the one statement of where
  ``data/`` is, which the bench root sits beside),
  the measured start of the span (``first_measurable_index``) and the
  refusals a landed world can meet (``EvaluationError``, ``FeatureError``,
  and ``SplitError`` for a decision span too short to cut);
- from the floor, the ``VocabEnum`` base every vocabulary here is spelled in
  and the venue's time form (``epoch_ms``, ``from_epoch_ms``, and its upper
  edge ``MAX_EPOCH_MS``, which bounds the last stamp a world may write), and
  the atomic write a score is put in place with (``atomic_write_bytes``).
"""

from __future__ import annotations

from contrib.autoresearch.constants import (
    DAILY_INTERVAL,
    FUNDING_INTERVAL_MS,
    MS_PER_DAY,
    bar_span_ms,
)
from contrib.autoresearch.costs import CostModel, require_amount
from contrib.autoresearch.dsl import (
    MAX_HOLD_BARS,
    Condition,
    Family,
    Op,
    Side,
    StrategySpec,
    parse_spec,
    spec_hash,
    spec_to_document,
)
from contrib.autoresearch.evaluator import (
    EvaluationError,
    SplitResult,
    Trade,
    evaluate_split,
    load_bundle,
    replay_sides,
)
from contrib.autoresearch.features import (
    MIN_INDICATOR_LOOKBACK,
    FeatureError,
    FeatureFrame,
    SeriesBundle,
)
from contrib.autoresearch.fetch import (
    FUNDING_PAGE_DAYS,
    SeriesFetch,
    StopReason,
    backfill_candles,
    backfill_funding,
    describe_stop,
    render_fetch,
)
from contrib.autoresearch.hypothesis import INTERRUPTED, SearchReport, search
from contrib.autoresearch.ledger import (
    Experiment,
    Ledger,
    Penalty,
    Trial,
    TrialStatus,
    Verdict,
    promotion_verdict,
)
from contrib.autoresearch.metrics import SegmentMetrics
from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.research import (
    first_measurable_index,
    promote,
    require_clean_history,
    require_measurable_tail,
)
from contrib.autoresearch.split import (
    DEFAULT_TRAIN_SHARE,
    DEFAULT_VALIDATION_SHARE,
    SegmentName,
    Split,
    SplitError,
    studied_interval,
)
from contrib.autoresearch.store import ResearchStore, StoreError, default_db_path
from contrib.autoresearch.upstream import Candle, FundingPoint, interval_to_ms
from contrib.autoresearch.vocabulary import (
    FeatureKind,
    FeatureRef,
    FeatureUnit,
    SeriesSource,
    SpecError,
    parse_feature_name,
    require_number,
    spec_of,
)
from contrib.core.atomic_io import atomic_write_bytes
from contrib.core.enum_guard import VocabEnum
from contrib.core.instants import MAX_EPOCH_MS, epoch_ms, from_epoch_ms

__all__ = [
    "BORROWED",
    "DAILY_INTERVAL",
    "DEFAULT_TRAIN_SHARE",
    "DEFAULT_VALIDATION_SHARE",
    "FUNDING_INTERVAL_MS",
    "FUNDING_PAGE_DAYS",
    "INTERRUPTED",
    "MAX_EPOCH_MS",
    "MAX_HOLD_BARS",
    "MIN_INDICATOR_LOOKBACK",
    "MS_PER_DAY",
    "UPSTREAM_PACKAGES",
    "Candle",
    "Condition",
    "CostModel",
    "EvaluationError",
    "Experiment",
    "Family",
    "FeatureError",
    "FeatureFrame",
    "FeatureKind",
    "FeatureRef",
    "FeatureUnit",
    "FundingPoint",
    "HypothesistError",
    "Ledger",
    "Op",
    "Penalty",
    "ResearchStore",
    "SearchReport",
    "SegmentMetrics",
    "SegmentName",
    "SeriesBundle",
    "SeriesFetch",
    "SeriesSource",
    "Side",
    "SpecError",
    "Split",
    "SplitError",
    "SplitResult",
    "StopReason",
    "StoreError",
    "StrategySpec",
    "Trade",
    "Trial",
    "TrialStatus",
    "Verdict",
    "VocabEnum",
    "atomic_write_bytes",
    "backfill_candles",
    "backfill_funding",
    "bar_span_ms",
    "default_db_path",
    "describe_stop",
    "epoch_ms",
    "evaluate_split",
    "first_measurable_index",
    "from_epoch_ms",
    "interval_to_ms",
    "load_bundle",
    "parse_feature_name",
    "parse_spec",
    "promote",
    "promotion_verdict",
    "render_fetch",
    "replay_sides",
    "require_amount",
    "require_clean_history",
    "require_measurable_tail",
    "require_number",
    "search",
    "spec_hash",
    "spec_of",
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
    ("contrib.autoresearch.constants", "DAILY_INTERVAL"),
    ("contrib.autoresearch.constants", "FUNDING_INTERVAL_MS"),
    ("contrib.autoresearch.constants", "MS_PER_DAY"),
    ("contrib.autoresearch.constants", "bar_span_ms"),
    ("contrib.autoresearch.costs", "CostModel"),
    ("contrib.autoresearch.costs", "require_amount"),
    ("contrib.autoresearch.dsl", "MAX_HOLD_BARS"),
    ("contrib.autoresearch.dsl", "Condition"),
    ("contrib.autoresearch.dsl", "Family"),
    ("contrib.autoresearch.dsl", "Op"),
    ("contrib.autoresearch.dsl", "Side"),
    ("contrib.autoresearch.dsl", "StrategySpec"),
    ("contrib.autoresearch.dsl", "parse_spec"),
    ("contrib.autoresearch.dsl", "spec_hash"),
    ("contrib.autoresearch.dsl", "spec_to_document"),
    ("contrib.autoresearch.evaluator", "EvaluationError"),
    ("contrib.autoresearch.evaluator", "SplitResult"),
    ("contrib.autoresearch.evaluator", "Trade"),
    ("contrib.autoresearch.evaluator", "evaluate_split"),
    ("contrib.autoresearch.evaluator", "load_bundle"),
    ("contrib.autoresearch.evaluator", "replay_sides"),
    ("contrib.autoresearch.features", "FeatureError"),
    ("contrib.autoresearch.features", "FeatureFrame"),
    ("contrib.autoresearch.features", "MIN_INDICATOR_LOOKBACK"),
    ("contrib.autoresearch.features", "SeriesBundle"),
    ("contrib.autoresearch.fetch", "FUNDING_PAGE_DAYS"),
    ("contrib.autoresearch.fetch", "SeriesFetch"),
    ("contrib.autoresearch.fetch", "StopReason"),
    ("contrib.autoresearch.fetch", "backfill_candles"),
    ("contrib.autoresearch.fetch", "backfill_funding"),
    ("contrib.autoresearch.fetch", "describe_stop"),
    ("contrib.autoresearch.fetch", "render_fetch"),
    ("contrib.autoresearch.hypothesis", "INTERRUPTED"),
    ("contrib.autoresearch.hypothesis", "SearchReport"),
    ("contrib.autoresearch.hypothesis", "search"),
    ("contrib.autoresearch.ledger", "Experiment"),
    ("contrib.autoresearch.ledger", "Ledger"),
    ("contrib.autoresearch.ledger", "Penalty"),
    ("contrib.autoresearch.ledger", "Trial"),
    ("contrib.autoresearch.ledger", "TrialStatus"),
    ("contrib.autoresearch.ledger", "Verdict"),
    ("contrib.autoresearch.ledger", "promotion_verdict"),
    ("contrib.autoresearch.metrics", "SegmentMetrics"),
    ("contrib.autoresearch.ports", "HypothesistError"),
    ("contrib.autoresearch.research", "first_measurable_index"),
    ("contrib.autoresearch.research", "promote"),
    ("contrib.autoresearch.research", "require_clean_history"),
    ("contrib.autoresearch.research", "require_measurable_tail"),
    ("contrib.autoresearch.split", "DEFAULT_TRAIN_SHARE"),
    ("contrib.autoresearch.split", "DEFAULT_VALIDATION_SHARE"),
    ("contrib.autoresearch.split", "SegmentName"),
    ("contrib.autoresearch.split", "Split"),
    ("contrib.autoresearch.split", "SplitError"),
    ("contrib.autoresearch.split", "studied_interval"),
    ("contrib.autoresearch.store", "ResearchStore"),
    ("contrib.autoresearch.store", "StoreError"),
    ("contrib.autoresearch.store", "default_db_path"),
    # The port's value types and the interval arithmetic are the perp
    # package's, reached the one way this package may reach them: as the
    # research package re-exports them for its own port.
    ("contrib.autoresearch.upstream", "Candle"),
    ("contrib.autoresearch.upstream", "FundingPoint"),
    ("contrib.autoresearch.upstream", "interval_to_ms"),
    ("contrib.autoresearch.vocabulary", "FeatureKind"),
    ("contrib.autoresearch.vocabulary", "FeatureRef"),
    ("contrib.autoresearch.vocabulary", "FeatureUnit"),
    ("contrib.autoresearch.vocabulary", "SeriesSource"),
    ("contrib.autoresearch.vocabulary", "SpecError"),
    ("contrib.autoresearch.vocabulary", "parse_feature_name"),
    ("contrib.autoresearch.vocabulary", "require_number"),
    ("contrib.autoresearch.vocabulary", "spec_of"),
    ("contrib.core.atomic_io", "atomic_write_bytes"),
    ("contrib.core.enum_guard", "VocabEnum"),
    ("contrib.core.instants", "MAX_EPOCH_MS"),
    ("contrib.core.instants", "epoch_ms"),
    ("contrib.core.instants", "from_epoch_ms"),
)
