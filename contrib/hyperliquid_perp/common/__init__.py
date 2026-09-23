"""Cross-layer shared utilities — the bottom of the import graph.

Home for the pure helpers that several layers (domains, persistence, paper,
live, audit) import but none owns: the enum guard, the construction-time seam
guard (:mod:`.seam_guard`; issue #169), the YAML-coercion seam, the
pinned decimal context, the network vocabulary, the store's timestamp decoder,
the on-disk layout beside a store (:mod:`.store_layout`; issue #221), the
legacy-vs-subcommand argv split the two entry points make
(:mod:`.entry_argv`; issue #221), the atomic text/bytes write and the
sidecar contract every artifact beside an input payload follows
(:mod:`.sidecar`) — plus the two
things the paper and live sides
share without either owning them: the decision cadence
(``constants.CYCLE_INTERVAL``) and the in-flight decision state machine
the two drivers advance a cycle through (:mod:`.inflight`; issue #181 — the
fields, the ordering rules between them, the per-try id scheme and the
resume step, with only the escalation policy left to each lane). The
no-decision escalation policy lives in ``runtime.no_decision``: it queries
``decision_attempts``, which nothing here may know.
Nothing here may import from any other ``hyperliquid_perp`` package, so any
module can depend on ``common`` without creating a cycle.
"""
