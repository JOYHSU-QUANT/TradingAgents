"""Construction-time guards for injected seams, shared across layers.

The live side takes its exchange reads as injected callables — a signed
client's ``open_orders``, an Info ``user_state`` read, ``user_fills_by_time``,
the orderStatus probe — and calls each one inside a fail-soft ``except
Exception`` lane on the first sweep. CI runs no type checker, so a payload
passed where a reader was meant would surface there only as "open_orders
failed: 'dict' object is not callable": an unclean pass every sweep, never a
crash (issues #132, #159). :func:`require_seam` refuses that at construction
instead, naming the seam, so every constructor that takes one
(``LiveReconciler``, ``VenueIdentityMonitor``, ``FillBackfiller``,
``WsConnectionSupervisor``) refuses the same way and the message has one
owner (issue #169).

Some seams are objects rather than callables — the reconciler reads a
``lookback`` off its backfiller and calls its ``backfill``, and drives a
stream through three methods. :func:`require_object_seam` is the same
refusal for those: it names the members the caller will use, and which of
them the object lacks, so a stand-in missing one is refused by name at the
binding instead of surfacing as an ``AttributeError`` inside a guarded leg
(issue #224). The two share one message template,
``<name> must be the <kind> seam (<shape>), got <type>``; the object form
appends ``without <members>``.

Deliberately ``callable()`` / ``hasattr()`` and nothing more — no arity or
signature check (decided with PR #168): the shape in the message is
documentation for the operator, not a contract this module enforces. The one
extra the object form makes is refusing a CLASS passed where an instance was
meant: a class answers ``hasattr`` for every member and its methods are
callable, so it would pass and then fail on the first call inside the guarded
lane (a missing ``self``) — the exact soft failure the guard exists to
refuse. Whether ``None`` is a legal "no seam" wiring is the caller's call,
made before calling either guard; both refuse it.

``kind`` names the seam family. The rule is: a callable seam is named by its
ROLE (``"exchange"``, ``"websocket"``, ``"kill-switch refresh"``), an object
seam by the CLASS the operator should construct (``"FillBackfiller"``,
``"VenueIdentityMonitor"``) — the refusal for an object points at what to
build, the refusal for a callable at what it does.

Pure: no I/O, no clock, no knowledge of which seams exist.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["require_object_seam", "require_seam"]


def require_seam(name: str, value: Any, *, kind: str, shape: str) -> None:
    """Raise ``TypeError`` naming ``name`` unless ``value`` is callable.

    ``kind`` is the seam family the message names (``"exchange"``,
    ``"orderStatus"``); ``shape`` is the call shape shown to the
    operator, e.g. ``"(start_ms, end_ms) -> fills list"``.
    """
    if not callable(value):
        raise TypeError(f"{name} must be the {kind} seam ({shape}), got {type(value).__name__}")


def require_object_seam(
    name: str,
    value: Any,
    *,
    kind: str,
    methods: Iterable[str] = (),
    attrs: Iterable[str] = (),
) -> None:
    """Raise ``TypeError`` naming ``name`` unless ``value`` answers every member.

    ``methods`` must be present AND callable (``.backfill()``); ``attrs``
    need only be present (``.lookback``). The message lists the whole shape
    the caller reads off the seam and then the members this value lacks, so
    the operator sees what to add — never a bare ``NoneType`` for a missing
    method, which is what ``getattr(value, m, None)`` handed the callable
    guard before this existed.
    """
    methods, attrs = tuple(methods), tuple(attrs)
    if not methods and not attrs:  # a guard that names nothing would refuse nothing
        raise ValueError(f"{name}: an object seam must name at least one method or attr")
    shape = ", ".join([f".{m}()" for m in methods] + [f".{a}" for a in attrs])
    if isinstance(value, type):  # would answer every member and fail on the first call
        raise TypeError(
            f"{name} must be the {kind} seam ({shape}), "
            f"got the class {value.__name__}, not an instance"
        )
    lacking = [f".{m}()" for m in methods if not callable(getattr(value, m, None))]
    lacking += [f".{a}" for a in attrs if not hasattr(value, a)]
    if lacking:
        raise TypeError(
            f"{name} must be the {kind} seam ({shape}), "
            f"got {type(value).__name__} without {', '.join(lacking)}"
        )
