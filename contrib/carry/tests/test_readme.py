"""The README's opening claim is a scope test, so it is pinned like a constant.

The rule ``contrib.replay`` and ``contrib.autoresearch`` keep: the first
content line says what the package is NOT, a reviewer judges "is this
change in scope" by whether that sentence would have to be softened, and
the package docstring makes the claim in English.
"""

from __future__ import annotations

from pathlib import Path

SCOPE_SENTENCE = "協調者不下單。"


def _readme_lines() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "README.md"
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_readme_opens_with_the_scope_sentence():
    title, first_claim = _readme_lines()[:2]
    assert title == "# carry"
    assert first_claim == SCOPE_SENTENCE


def test_the_package_docstring_makes_the_same_claim():
    import contrib.carry as package

    assert "NOT a trading path" in package.__doc__
    assert "NOT a decision maker" in package.__doc__
