"""Tests for the shared construction-time seam guard (issue #169).

The three constructors that take a seam pin THAT they refuse through it
(``tests/live/test_reconcile.py``, ``test_venue_identity.py``,
``test_ws_stream.py``); this file pins the guard's own contract.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.seam_guard import require_seam

_SHAPE = "(start_ms, end_ms) -> fills list"


class _Reader:
    def read(self):
        return []


@pytest.mark.parametrize(
    "value",
    [lambda: [], _Reader().read, _Reader.read, len],
    ids=["zero-arg lambda", "bound method", "unbound function", "builtin"],
)
def test_any_callable_passes_without_a_signature_check(value):
    # ``callable()`` and nothing more (decided with PR #168): a zero-arg lambda
    # passes a seam documented as two-arg — ``shape`` is documentation for the
    # operator, not a contract the guard enforces.
    require_seam("fetch_fills", value, kind="exchange", shape=_SHAPE)


@pytest.mark.parametrize(
    ("value", "type_name"), [({"fills": []}, "dict"), (None, "NoneType")], ids=["payload", "None"]
)
def test_a_non_callable_is_refused_naming_seam_kind_shape_and_type(value, type_name):
    # ``None`` is refused too: whether "no seam" is a legal wiring is the
    # caller's decision, made before calling the guard (``fetch_fills`` may be
    # None on the reconciler; ``fetch_open_orders`` may not).
    with pytest.raises(TypeError) as excinfo:
        require_seam("fetch_fills", value, kind="exchange", shape=_SHAPE)
    assert (
        str(excinfo.value) == f"fetch_fills must be the exchange seam ({_SHAPE}), got {type_name}"
    )
