"""The README's opening claim is a scope test, so it is pinned like a constant.

The rule ``contrib.autoresearch`` and ``contrib.replay`` keep: the first
content line says what the package is NOT, a reviewer judges "is this
change in scope" by whether that sentence would have to be softened, and
the package docstring makes the claim in English for whoever lands on
``help()`` first. Plan §0.3's scope statement is pinned beside it, by its
four headings and by the one phrase §0.1 chose over the one it rejected.
"""

from __future__ import annotations

from pathlib import Path

SCOPE_SENTENCE = "Discovery Lab 不是另一條研究路徑，也不是交易路徑。"


def _readme() -> str:
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def _readme_lines() -> list[str]:
    return [line for line in _readme().splitlines() if line.strip()]


def test_the_readme_opens_with_the_scope_sentence():
    title, first_claim = _readme_lines()[:2]
    assert title == "# discovery"
    assert first_claim == SCOPE_SENTENCE


def test_the_package_docstring_makes_the_same_claim():
    import contrib.discovery as package

    assert "NOT a second research path" in package.__doc__


def test_the_scope_statement_is_in_the_readme_under_its_four_headings():
    text = _readme()
    for heading in ("**Objective**", "**In scope**", "**Out of scope**", "**Interpretation**"):
        assert heading in text, heading


def test_the_hypothesis_space_is_called_bounded_and_never_enumerable():
    # Plan §0.1: the structure is finite and the thresholds are continuous,
    # so "enumerable" would overclaim — and it is the word a reader of the
    # objective would reach for first.
    text = _readme()
    assert "finite-structure, bounded" in text
    assert "enumerable" not in text
