"""Construction-time guard for injected callable seams, shared across layers.

The live side takes its exchange reads as injected callables — a signed
client's ``open_orders``, an Info ``user_state`` read, ``user_fills_by_time``,
the orderStatus probe — and calls each one inside a fail-soft ``except
Exception`` lane on the first sweep. CI runs no type checker, so a payload
passed where a reader was meant would surface there only as "open_orders
failed: 'dict' object is not callable": an unclean pass every sweep, never a
crash (issues #132, #159). :func:`require_seam` refuses that at construction
instead, naming the seam, so every constructor that takes one
(``LiveReconciler``, ``VenueIdentityMonitor``, ``FillBackfiller``) refuses
the same way and the message has one owner (issue #169).

Deliberately ``callable()`` and nothing more — no arity or signature check
(decided with PR #168): the ``shape`` in the message is documentation for the
operator, not a contract this module enforces. Whether ``None`` is a legal
"no seam" wiring is the caller's call, made before calling this.

Pure: no I/O, no clock, no knowledge of which seams exist.
"""

from __future__ import annotations

from typing import Any

__all__ = ["require_seam"]


def require_seam(name: str, value: Any, *, kind: str, shape: str) -> None:
    """Raise ``TypeError`` naming ``name`` unless ``value`` is callable.

    ``kind`` is the seam family the message names (``"exchange"``,
    ``"orderStatus"``); ``shape`` is the call shape shown to the
    operator, e.g. ``"(start_ms, end_ms) -> fills list"``.
    """
    if not callable(value):
        raise TypeError(f"{name} must be the {kind} seam ({shape}), got {type(value).__name__}")
