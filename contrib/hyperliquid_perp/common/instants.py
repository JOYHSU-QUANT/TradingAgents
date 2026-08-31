"""Instants and spans as the store and the operator see them.

:func:`parse_instant` decodes the repository's timestamp form. It writes every
instant as an ISO-8601 UTC string, and readers across four layers decode it:
the scheduler, the run lock, reconciliation, the paper and live validators,
the CLI's lease probes, the no-decision policy. The decoder lived on
``paper.scheduler``, which every one of them then had to import — pulling the
paper engine into modules as far from it as ``live.validation`` and the
keyless CLI lease checks, for one function. Here, at the bottom of the import
graph, it costs nothing to reach (issue #122); ``paper.scheduler`` re-exports
it for the callers that always found it there.

:func:`whole_hours_label` renders a span the way an operator-facing message
states a window ("6h", "4h"). Two modules derive such a label from a constant
(the reconciler's fill-backfill window, the freshness guard's decision cycle)
and each had grown its own copy of the same guard: a span that is not whole
hours must refuse at import rather than render truncated, because "5h" over a
5h30m window understates the bound the message is describing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

__all__ = ["parse_instant", "whole_hours_label"]

_HOUR = timedelta(hours=1)


def parse_instant(text: str) -> datetime:
    """Decode a stored ISO-8601 UTC timestamp (the repository's storage form)."""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:  # the write boundary never stores naive stamps
        raise ValueError(f"stored timestamp {text!r} is naive; the store is corrupt")
    return value


def whole_hours_label(span: timedelta, *, what: str) -> str:
    """``span`` as ``"6h"``; ``ValueError`` naming ``what`` if it is not whole hours.

    Meant for module-level constants, so the raise lands at import time — a
    retuned window that is no longer whole hours is a change the message
    rendering it has to be rewritten for, not rounded past.
    """
    if span % _HOUR:
        raise ValueError(
            f"{what} must be a whole number of hours; the operator-facing label "
            f"renders it as hours (got {span})"
        )
    return f"{span // _HOUR}h"
