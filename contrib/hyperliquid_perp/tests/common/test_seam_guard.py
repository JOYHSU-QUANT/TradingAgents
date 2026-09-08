"""Tests for the shared construction-time seam guards (issues #169, #224).

The constructors that take a seam pin THAT they refuse through them
(``tests/live/test_reconcile.py``, ``test_venue_identity.py``,
``test_ws_stream.py``); this file pins the guards' own contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.common.seam_guard import require_object_seam, require_seam

_SHAPE = "(start_ms, end_ms) -> fills list"


class _Reader:
    def read(self):
        return []


class _Leg:
    lookback = 1  # an attr need only exist; it is not called

    def backfill(self):
        return None


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


def test_an_object_answering_every_member_passes():
    # Presence and callability, nothing more: the attr is not read and the
    # method is not called.
    require_object_seam(
        "backfiller", _Leg(), kind="FillBackfiller", methods=("backfill",), attrs=("lookback",)
    )


def test_the_class_itself_is_refused_where_an_instance_was_meant():
    # A class answers ``hasattr`` for every member and its functions are
    # callable, so presence alone would let ``stream=LiveWsStream`` (no
    # parentheses) through — and it would then fail on the first call inside
    # the guarded lane, missing ``self``: the soft failure the guard is for.
    with pytest.raises(TypeError) as excinfo:
        require_object_seam(
            "backfiller", _Leg, kind="FillBackfiller", methods=("backfill",), attrs=("lookback",)
        )
    assert str(excinfo.value) == (
        "backfiller must be the FillBackfiller seam (.backfill(), .lookback), "
        "got the class _Leg, not an instance"
    )


def test_an_object_seam_that_names_no_member_is_a_caller_bug():
    # Both member lists default to empty so a caller can pass either alone;
    # passing neither would make the guard accept everything, ``None``
    # included — the opposite of what a guard is for — so that is refused as
    # the wiring mistake it is, not silently honoured.
    with pytest.raises(ValueError, match="^processor: an object seam must name at least one"):
        require_object_seam("processor", _Leg(), kind="fill processor")


@pytest.mark.parametrize(
    ("value", "type_name", "lacking"),
    [
        (SimpleNamespace(lookback=1), "SimpleNamespace", ".backfill()"),
        (SimpleNamespace(backfill=lambda: None), "SimpleNamespace", ".lookback"),
        (SimpleNamespace(lookback=1, backfill=3), "SimpleNamespace", ".backfill()"),
        (None, "NoneType", ".backfill(), .lookback"),
    ],
    ids=["no method", "no attr", "method not callable", "None"],
)
def test_an_object_lacking_a_member_is_refused_naming_the_shape_and_the_lack(
    value, type_name, lacking
):
    # Same template as the callable guard, then WHAT is missing — never a bare
    # ``NoneType`` for an absent method, which is what feeding ``getattr``'s
    # default to ``require_seam`` produced (issue #224). ``None`` is refused
    # like any other object: whether "no seam" is legal is the caller's call.
    with pytest.raises(TypeError) as excinfo:
        require_object_seam(
            "backfiller", value, kind="FillBackfiller", methods=("backfill",), attrs=("lookback",)
        )
    assert str(excinfo.value) == (
        "backfiller must be the FillBackfiller seam (.backfill(), .lookback), "
        f"got {type_name} without {lacking}"
    )
