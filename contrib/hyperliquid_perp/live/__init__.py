"""Phase 3 live execution package (parallel to :mod:`..paper`, phase3-spec §2.1).

PR 1: the typed ``live:`` config block with its startup gates (:mod:`.config`),
per-network agent-key loading (:mod:`.secrets`), and the startup
agent-authorization check (:mod:`.authorization`). PR 2: the §4.1 real order
gate (:mod:`.order_gate`), the §8.3 idempotent order-submission path
(:mod:`.orders`), and the §18 dead man's switch (:mod:`.kill_switch`). The
live execution engine itself (the loop that decides WHEN to place orders)
arrives in PR 5.
"""
