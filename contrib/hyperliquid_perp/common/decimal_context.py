"""The one decimal context for money math and its consistency checks.

Defined in this dependency-free shared module so every layer (margin tiers,
accounting, liquidation, repository identity checks) pins the *same*
arithmetic. Replay's "same committed events -> bit-for-bit same state"
guarantee — and the §12.2 reproducibility fields recorded on snapshots — must
not depend on the ambient (mutable, global) decimal context. 28 significant
digits — the decimal default — with default traps.
"""

from __future__ import annotations

from decimal import Context

__all__ = ["DECIMAL_CONTEXT"]

DECIMAL_CONTEXT = Context(prec=28)
