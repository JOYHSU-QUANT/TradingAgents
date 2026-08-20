"""Generic "value must be one of an allowed set" guard, shared across layers.

Extracted here (a neutral, dependency-free shared module) so both the
persistence write boundary (:mod:`..persistence.repository`) and the paper
accounting result dataclasses (:class:`..paper.accounting.FundingResult`)
validate enum-like string columns/fields the same way — without either module
reaching into the other's private helpers.

Pure: no I/O, no clock, no domain knowledge of *which* values are allowed (each
caller owns its own frozenset).
"""

from __future__ import annotations

__all__ = ["check_enum"]


def check_enum(value: str, allowed: frozenset[str], *, name: str) -> None:
    """Raise ``ValueError`` naming ``name`` unless ``value`` is in ``allowed``."""
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
