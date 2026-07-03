"""Audit trail for the perp module — durable record of each decision.

One JSON file per decision (:mod:`.decision_log`): the prompt hash, the models
used, the engine's raw response with its parse verdict, and the full RiskGate
outcome — enough to reconstruct and post-mortem any run.
"""
