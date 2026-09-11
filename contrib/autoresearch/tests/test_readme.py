"""The README's opening claim is a scope test, so it is pinned like a constant.

Plan §3.1 fixes the package's first line and says what it is FOR: a reviewer
judges "is this change in scope" by whether the change would need that
sentence softened. A sentence that can be edited away without anything going
red is not a test of anything, so it is pinned here — and pinned as the first
CONTENT line, not merely as a substring somewhere in the file, because a
scope statement buried in the middle is not the thing a reviewer reads first.
"""

from __future__ import annotations

from pathlib import Path

SCOPE_SENTENCE = "\u7814\u7a76\u96f7\u9054\u4e0d\u662f\u53e6\u4e00\u5957\u4ea4\u6613\u7cfb\u7d71\u3002"


def _readme_lines() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "README.md"
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_readme_opens_with_the_scope_sentence():
    title, first_claim = _readme_lines()[:2]
    assert title == "# autoresearch"
    assert first_claim == SCOPE_SENTENCE


def test_the_package_docstring_makes_the_same_claim():
    """The sentence has to survive being read from inside Python too.

    ``help(contrib.autoresearch)`` is where someone lands who never opens the
    README, and a scope rule that only one of the two states is a scope rule
    one of them can drift out of.
    """
    import contrib.autoresearch as package

    assert "NOT a second trading system" in package.__doc__
