"""Tests for the lone-surrogate replacement (issue #427).

The callers pin THAT they store through it (the perp package's
``tests/domains/test_target_decision.py`` and ``tests/paper/test_scheduler.py``);
this file pins the function's own contract.
"""

from __future__ import annotations

import json

import pytest

from contrib.core.surrogates import REPLACEMENT_CHARACTER, replace_lone_surrogates


def test_the_replacement_is_the_unicode_replacement_character():
    assert REPLACEMENT_CHARACTER == "\ufffd"


def test_clean_text_comes_back_as_the_same_object():
    # A whole emoji is one code point above U+FFFF, not a pair of surrogates,
    # and a U+FFFD the model wrote itself is ordinary text.
    text = "plain, a whole emoji \U0001f600, and a model's own \ufffd"
    replaced, count = replace_lone_surrogates(text)
    assert replaced is text
    assert count == 0


@pytest.mark.parametrize(
    "half",
    ["\ud800", "\udbff", "\udc00", "\udfff", "\ud83d"],
    ids=["first-high", "last-high", "first-low", "last-low", "an-emoji's-high"],
)
def test_both_ends_of_both_surrogate_ranges_are_replaced(half):
    assert replace_lone_surrogates(f"a{half}b") == ("a\ufffdb", 1)


def test_the_code_points_on_either_side_of_the_range_are_left_alone():
    text = "\ud7ff\ue000"
    assert replace_lone_surrogates(text) == (text, 0)


def test_each_one_is_counted_and_replaced_one_for_one():
    # A high and a low side by side in a ``str`` are two lone halves, not a
    # pair: a pair would have been one code point. The length does not move.
    text = "\ud83d\ude00 x \ud83d"
    replaced, count = replace_lone_surrogates(text)
    assert replaced == "\ufffd\ufffd x \ufffd"
    assert count == 3
    assert len(replaced) == len(text)


def test_the_result_encodes_where_the_input_could_not():
    text = "half \ud83d"
    with pytest.raises(UnicodeEncodeError):
        text.encode("utf-8")
    assert replace_lone_surrogates(text)[0].encode("utf-8") == b"half \xef\xbf\xbd"


def test_an_escaped_half_pair_is_six_characters_until_json_reads_it():
    # The second way in (issue #427): nothing to replace in the reply, one to
    # replace in the field parsed out of it.
    reply = '{"note": "half \\ud83d"}'
    assert reply.isascii()
    assert replace_lone_surrogates(reply) == (reply, 0)
    assert replace_lone_surrogates(json.loads(reply)["note"]) == ("half \ufffd", 1)


def test_an_escaped_whole_pair_is_one_character_and_is_kept():
    note = json.loads('{"note": "\\ud83d\\ude00"}')["note"]
    assert note == "\U0001f600"
    assert replace_lone_surrogates(note) == (note, 0)
