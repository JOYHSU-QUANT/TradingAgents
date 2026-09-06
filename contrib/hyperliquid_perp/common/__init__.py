"""Cross-layer shared utilities — the bottom of the import graph.

Home for the pure helpers that several layers (domains, persistence, paper,
live, audit) import but none owns: the enum guard, the YAML-coercion seam, the
pinned decimal context, the network vocabulary, the store's timestamp decoder,
and the atomic text write — plus the three things the paper and live sides
share without either owning them: the decision cadence
(``constants.CYCLE_INTERVAL``), the no-decision escalation policy
(:mod:`.no_decision`; issue #122), and the in-flight decision state machine
the two drivers advance a cycle through (:mod:`.inflight`; issue #181 — the
fields, the ordering rules between them, the per-try id scheme and the
resume step, with only the escalation policy left to each lane). That
no-decision policy is the one module here that
knows the store's shape — it queries ``decision_attempts`` through plain
``sqlite3`` rather than through ``persistence.repository``, which sits above
this package; a change to that table has to look here too.
Nothing here may import from any other ``hyperliquid_perp`` package, so any
module can depend on ``common`` without creating a cycle.
"""
