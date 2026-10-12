"""Cross-layer shared utilities — the bottom of the package's import graph.

Home for the pure helpers that several layers (domains, persistence, paper,
live, audit) import but none owns: the construction-time seam guard
(:mod:`.seam_guard`; issue #169), the YAML-coercion seam, the network
vocabulary, the on-disk layout beside a store (:mod:`.store_layout`; issue
#221), the legacy-vs-subcommand argv split the two entry points make
(:mod:`.entry_argv`; issue #221) and the sidecar contract every artifact
beside an input payload follows (:mod:`.sidecar`) — plus the two things the
paper and live sides share without either owning them: the decision cadence
(``constants.CYCLE_INTERVAL``) and the in-flight decision state machine the
two drivers advance a cycle through (:mod:`.inflight`; issue #181 — the
fields, the ordering rules between them, the per-try id scheme and the
resume step, with only the escalation policy left to each lane). The
no-decision escalation policy lives in ``runtime.no_decision``: it queries
``decision_attempts``, which nothing here may know.

The helpers that know nothing of this strategy — the enum guard, the pinned
decimal context, the store's timestamp decoder, the atomic write and the
artifact digest — moved to :mod:`contrib.core` (dashboard plan PR 1), the
floor every contrib package shares.

Nothing here may import from any other ``hyperliquid_perp`` package, and
the only contrib package it may import is ``contrib.core``, so any module
can depend on ``common`` without creating a cycle.
"""
