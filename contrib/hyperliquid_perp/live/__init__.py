"""Phase 3 live execution package (parallel to :mod:`..paper`, phase3-spec §2.1).

PR 1 scope: the typed ``live:`` config block with its startup gates
(:mod:`.config`), per-network agent-key loading (:mod:`.secrets`), and the
startup agent-authorization check (:mod:`.authorization`). The live execution
engine itself arrives in PR 5; nothing in this package places orders yet.
"""
