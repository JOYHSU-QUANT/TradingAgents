"""Cross-layer shared utilities — the bottom of the import graph.

Home for the pure helpers that several layers (domains, persistence, paper,
live, audit) import but none owns: the enum guard, the YAML-coercion seam, the
pinned decimal context, the network vocabulary, the store's timestamp decoder,
and the atomic text write — plus the two things the paper and live sides
share without either owning them: the decision cadence
(``constants.CYCLE_INTERVAL``) and the no-decision escalation policy
(:mod:`.no_decision`; issue #122). That policy is the one module here that
knows the store's shape — it queries ``decision_attempts`` through plain
``sqlite3`` rather than through ``persistence.repository``, which sits above
this package; a change to that table has to look here too.
Nothing here may import from any other ``hyperliquid_perp`` package, so any
module can depend on ``common`` without creating a cycle.
"""
