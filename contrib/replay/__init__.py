"""Replay — the paper trader's offline exam: a scorecard now, past papers next.

The replay package is NOT a trading path: it places no orders, writes nothing
into ``contrib.hyperliquid_perp``'s store, and never bumps a prompt version.
It reads the decisions the paper trader already recorded (each
``decision_attempts`` row, with the final ``ai_inputs`` row and the
``ai_outputs`` row it names) and marks each one against the price that
followed — the scorecard — so that "did the model call it, and did the rule
let it through" becomes a table rather than an impression. That sentence is
the package's scope test: a change that needs it softened is out of scope,
not a bigger feature.

It is the one package under ``contrib/`` that imports BOTH neighbours —
``contrib.hyperliquid_perp`` for the decision vocabulary, the store and the
paper cost parameters, ``contrib.autoresearch`` for the split, the cost model
and the research store (replay plan §3-1). The edge is one-way: neither
neighbour imports this package, and ``tests/test_upstream.py`` reads their
sources to hold that. What is borrowed is listed once, in
:mod:`~contrib.replay.upstream`, and every other module here imports from
THAT rather than reaching out itself.

The simple version scores each decision on its own: the position it was made
from is the one the store recorded, and no simulated account carries a
position from one decision to the next. That makes it a re-grading of
judgement, not a backtest (replay plan §1).
"""
