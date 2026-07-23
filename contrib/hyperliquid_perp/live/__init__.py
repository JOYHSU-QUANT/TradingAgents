"""Phase 3 live execution package (parallel to :mod:`..paper`, phase3-spec §2.1).

PR 1: the typed ``live:`` config block with its startup gates (:mod:`.config`),
per-network agent-key loading (:mod:`.secrets`), and the startup
agent-authorization check (:mod:`.authorization`). PR 2: the §4.1 real order
gate (:mod:`.order_gate`), the §8.3 idempotent order-submission path
(:mod:`.orders`), and the §18 dead man's switch (:mod:`.kill_switch`). PR 5:
the live execution engine itself (:mod:`.engine`, the sliced-TWAP loop), SL/TP
protection (:mod:`.protection`), the §10 loss guards (:mod:`.loss_guards`),
and the background AI decision driver (:mod:`.decision`).
"""
