"""Audit trail for the perp module — durable record of each decision.

Phase 1 writes one JSON file per decision (:mod:`.decision_log`): the prompt
hash the engine reasoned over, the models used, the resulting
:class:`~..domains.perp.decision.PerpTradeDecision`, and a timestamp — enough to
reconstruct and post-mortem any run.
"""
