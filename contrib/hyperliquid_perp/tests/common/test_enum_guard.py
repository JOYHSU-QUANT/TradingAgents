"""Tests for the shared vocabulary base (issue #166).

``check_enum``'s callers pin THAT they refuse through it (``tests/live/
test_reconcile.py``, ``test_fills.py``), and the four ``schema`` enums pin
their own sentences in ``tests/domains/test_schema.py``; this file pins the
``VocabEnum`` base's own contract on a synthetic enum.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.enum_guard import VocabEnum, check_enum


@pytest.mark.parametrize(
    "value", [5, None, ["a"], {"a": 1}], ids=["int", "none", "list", "dict"]
)
def test_check_enum_refuses_a_non_string_without_looking_it_up(value):
    # A YAML value of the wrong type is refused by the same sentence as a wrong
    # spelling. An UNHASHABLE one must not escape as a ``TypeError`` from a
    # frozenset membership test — the callers' ``except ValueError`` lanes
    # (config load errors) would let a raw traceback through (issue #226).
    with pytest.raises(ValueError, match=r"^k must be one of \['a', 'b'\], got "):
        check_enum(value, frozenset({"a", "b"}), name="k")


class _Colour(VocabEnum, noun="paint colour"):
    RED = "red"
    BLUE = "blue"
    AMBER = "amber"  # out of sorted order on purpose: the sentence must not re-sort


def test_an_unknown_value_names_the_noun_and_the_vocabulary_in_declaration_order():
    # ``Enum`` alone would say "'Red' is not a valid _Colour". The value is
    # repr'd so a mis-cased or blank string shows its quotes, and a non-string
    # (a YAML integer) is named as what it is.
    with pytest.raises(
        ValueError,
        match=r"^unsupported paint colour 'Red'; choose from \['red', 'blue', 'amber'\]$",
    ):
        _Colour("Red")
    with pytest.raises(ValueError, match=r"^unsupported paint colour 4; choose from "):
        _Colour(4)
    with pytest.raises(ValueError, match=r"^unsupported paint colour ''; choose from "):
        _Colour("")


def test_a_member_named_noun_cannot_shadow_the_noun():
    # The noun lives under a name-mangled attribute precisely so a subclass's
    # member names are irrelevant to it: with a plain ``_noun`` attribute, a
    # member called ``_noun`` silently replaced it on 3.10 (the sentence read
    # ``unsupported x ...``) and made the class fail to define on 3.11+.
    class _Shadow(VocabEnum, noun="widget"):
        _noun = "x"
        A = "a"

    assert [m.name for m in _Shadow] == ["_noun", "A"]
    with pytest.raises(ValueError, match=r"^unsupported widget 'nope'; choose from \['x', 'a'\]$"):
        _Shadow("nope")


def test_looking_up_the_bare_base_is_a_type_error_not_an_attribute_error():
    # The base has no noun; 3.11+ refuses the memberless lookup itself, 3.10
    # reached ``_missing_`` and died on the missing attribute. One answer.
    with pytest.raises(TypeError):
        VocabEnum("x")


def test_the_noun_is_required_at_class_definition():
    # The signature's own refusal (a required keyword-only parameter), pinned
    # because it is the contract: a subclass that forgot the keyword fails
    # HERE, not with an ``unsupported None`` sentence at its first bad lookup.
    with pytest.raises(TypeError, match="noun"):

        class _Nameless(VocabEnum):
            A = "a"


@pytest.mark.parametrize("noun", ["", " ", "\t"], ids=["empty", "space", "tab"])
def test_a_blank_noun_is_refused_at_class_definition(noun):
    # A blank noun would print ``unsupported  'x'`` — a sentence naming no
    # vocabulary at all — so it is refused where it is written; whitespace
    # counts as blank (an ``if not noun`` check let ``" "`` through).
    with pytest.raises(TypeError, match="non-blank str"):

        class _Blank(VocabEnum, noun=noun):
            A = "a"
