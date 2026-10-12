"""Strategy-free helpers every contrib package may share — the floor of ``contrib``.

Moved out of ``contrib.hyperliquid_perp.common`` (dashboard plan PR 1) so a
package that is not the perp trader can use them without importing the perp
trader:

- :mod:`.atomic_io` — the atomic text/bytes write (tmp, then replace);
- :mod:`.decimal_context` — the one pinned decimal context for money math;
- :mod:`.digest` — the ``sha256:<hex>`` artifact digest and the one spelling
  of indented-JSON-as-bytes it hashes;
- :mod:`.enum_guard` — the "must be one of" refusal and the ``VocabEnum`` base;
- :mod:`.instants` — the store's ISO-8601 instant decoder, the epoch-ms
  conversions and the operator-facing span labels.

Two rules hold this package at the bottom, both checked by
``contrib/tests/test_package_graph.py``: nothing here imports another
contrib package, and nothing here imports anything outside the standard
library. A helper that needs a third-party library or knows a strategy's
vocabulary belongs in that strategy's package, not here.

The modules' docstrings still name the callers they were written for. Names
like ``paper.scheduler`` or ``domains.perp.schema`` there are relative to
``contrib.hyperliquid_perp``, where every one of those callers lives.
"""
