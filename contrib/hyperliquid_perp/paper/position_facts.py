"""Compatibility path: the books read moved to :mod:`..runtime.position_facts` (refactor plan v2, T1).

Every in-package importer already uses that path — this module exists for
one PR, until the accounting split (plan PR 3) deletes it.
"""

from __future__ import annotations

from ..runtime.position_facts import BookFacts, BookPosition, BookSource, read_books

__all__ = ["BookFacts", "BookPosition", "BookSource", "read_books"]
