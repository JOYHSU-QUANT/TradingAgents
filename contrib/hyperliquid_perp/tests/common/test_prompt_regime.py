"""Tests for the shared ``prompt_regime:`` line (issue #163).

Three surfaces print the segmentation keys — ``validate`` (per bucket), the
running daemons (at the first prompt and on every flip) and
``--context-only`` (the keyless preview) — and an operator greps across all
three, so they must render through ONE function. The daemon's and the
preview's use of it are pinned beside those callers (``tests/cli``); this
module pins the grammar and the validators' side.
"""

from __future__ import annotations

from contrib.hyperliquid_perp.common.prompt_regime import (
    POSITION_SECTION_OMITTED,
    PROMPT_REGIME_PREFIX,
    PositionOmission,
    position_section_omitted,
    prompt_regime_line,
)
from contrib.hyperliquid_perp.paper import validation as validation_mod
from contrib.hyperliquid_perp.persistence import repository as repo


def test_the_line_names_the_three_keys_in_order_under_the_grep_prefix():
    line = prompt_regime_line("phase2-target-v5", "price|market|position", "abcd0000abcd0000")
    assert line == (
        "prompt_regime: prompt_version=phase2-target-v5"
        " context_shape=price|market|position format_fingerprint=abcd0000abcd0000"
    )
    assert line.startswith(PROMPT_REGIME_PREFIX + " ")


def test_a_key_a_row_was_written_without_prints_n_a_not_none():
    # A pre-v10 / pre-v11 row: "unknown", never "None" rendered raw, and never
    # an empty field a reader could mistake for a distinct regime.
    assert prompt_regime_line("phase2-target-v3", None, None) == (
        "prompt_regime: prompt_version=phase2-target-v3 context_shape=n/a format_fingerprint=n/a"
    )


def test_cycles_is_the_validators_suffix_and_absent_for_a_single_prompt():
    bare = prompt_regime_line("v", "s", "f")
    assert " cycles=" not in bare
    assert prompt_regime_line("v", "s", "f", cycles=7) == bare + " cycles=7"


def test_the_validators_render_their_buckets_through_the_shared_line(monkeypatch):
    # Both validators print through ``paper.validation.prompt_regime_lines``
    # (``live.validation`` imports it), so this one seam covers them. Patched
    # at the name the module bound: re-inlining the format there would keep
    # every byte-for-byte output pin green and leave the three surfaces free
    # to drift — this is the test that goes red.
    monkeypatch.setattr(
        validation_mod, "prompt_regime_line", lambda *args, **kwargs: f"X {args} {kwargs}"
    )
    lines = validation_mod.prompt_regime_lines(
        (repo.PromptRegime("v", "s", "f", 3), repo.PromptRegime("v", None, None, 1))
    )
    assert lines == ["X ('v', 's', 'f') {'cycles': 3}", "X ('v', None, None) {'cycles': 1}"]


def test_the_position_omission_line_is_one_template_over_a_closed_reason_vocabulary():
    # Issue #161 accepted that the two causes of a missing ``Position:``
    # section render the same prompt and the same ``context_shape`` and get
    # no store column; issue #197 made the WARNING that records which one
    # happened a shared template with a ``reason=`` grep handle rather than
    # two hand-written English lines. Both callers (``cli._provider`` for
    # ``no_books``, ``domains.perp.marginal_cost`` for ``non_positive_equity``)
    # are pinned at their sites; this pins the template and the vocabulary
    # they share.
    from typing import get_args

    assert get_args(PositionOmission) == ("no_books", "non_positive_equity")
    lines = {
        reason: position_section_omitted(reason, "why") for reason in get_args(PositionOmission)
    }
    assert lines == {
        "no_books": "position section omitted (reason=no_books): why",
        "non_positive_equity": "position section omitted (reason=non_positive_equity): why",
    }
    # RUNBOOK §7's grep: the prefix is the constant, the member follows it.
    assert all(line.startswith(f"{POSITION_SECTION_OMITTED} (reason=") for line in lines.values())
