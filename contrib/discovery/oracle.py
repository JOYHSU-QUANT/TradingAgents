"""The oracle's read path: score a spec on an arm's store and file nothing (plan §4.4).

The oracle is the planted rule measured under exactly the conditions the
arms search under — same store, same split, same costs — so that "the loop
did not find it" can be told apart from "it was not there to find" (plan
§3 step 3). It cannot go through :func:`~contrib.autoresearch.research.measure`:
that verb files what it scores as a trial, the next prompt would show the
model the answer before it searched, and the coin's rule count — the ``n``
of the promote threshold — would be one higher than the search earned
(plan §1 #4). It goes through the pure function ``calibrate`` uses instead,
:func:`~contrib.autoresearch.evaluator.evaluate_split`, which writes nothing
(plan §1 #5). ``tests/test_isolation.py`` holds that: the arm's store is
byte-identical around a call here.

``holdout`` is the research package's own lock, restated: the caller names
the window at the call site. WHEN the oracle may name it is the benchmark's
protocol, not this function's — train and validation figures may be read
before the arms search (to size the planted effect, on development seeds
only), the holdout figure only after every arm has searched and chosen
(plan §4.4), and the runner that sequences a world (plan PR 3) is where that
order is kept.

The read mirrors the research package's private ``research._frame`` (the
bundle bound, the clean-history scan, the frame) and, for the holdout, the
tail check ``promote`` makes on the holdout-bound frame; it is kept in step
by hand because that helper is private. Publishing it upstream would make
this function two lines (a follow-up candidate, not this package's to make).
"""

from __future__ import annotations

from .upstream import (
    Experiment,
    FeatureFrame,
    Ledger,
    SplitResult,
    StrategySpec,
    evaluate_split,
    load_bundle,
    require_clean_history,
    require_measurable_tail,
)

__all__ = ["evaluate_unfiled"]


def evaluate_unfiled(
    ledger: Ledger, experiment: Experiment, spec: StrategySpec, *, holdout: bool
) -> SplitResult:
    """``spec`` scored on ``experiment``'s windows, read no further than the lock allows, filed nowhere.

    Train and validation, and the holdout only when named — the same bundle
    bound (``Split.loadable_until``) the research package's own measurement
    reads under, so an oracle figure is computed over exactly the bars a
    trial's would be, and the holdout over exactly the bars ``promote``
    would refuse if its tail had lost a feature. The experiment has to be
    the stored one: the split it carries is what makes an oracle figure
    comparable with the arm's trials. The full result, trades included, is
    returned: the scorer (plan §4.6) reads the planted side's positions off
    it, and ``SegmentMetrics.from_result`` gives the figures.
    """
    ledger.require_stored(experiment)
    bundle = load_bundle(
        ledger.store,
        coin=experiment.coin,
        interval=experiment.split.interval,
        until_ms=experiment.split.loadable_until(holdout=holdout),
    )
    require_clean_history(bundle, experiment.split.interval)
    frame = FeatureFrame(bundle, indicator_lookback=experiment.indicator_lookback)
    if holdout:
        require_measurable_tail(frame)
    return evaluate_split(spec, frame, experiment.split, experiment.costs, holdout=holdout)
