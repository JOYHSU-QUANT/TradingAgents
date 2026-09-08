"""Which entry a ``python -m contrib.hyperliquid_perp`` argv belongs to.

Two entries share one package: the legacy one-shot shell (``.main`` —
``--context-only`` and the single-shot engine run, flags only) and the
subcommand CLI (``.cli`` — ``paper`` / ``export`` / ``validate`` / …, a bare
word first). The split is decided by the FIRST argument alone, and it is
decided twice: by ``__main__`` before either module is imported (so the
keyless preview never loads the daemon surface; issue #221) and by
``cli.main`` for callers that reach it directly. One predicate here, at the
bottom of the import graph, so neither entry has to import the other to
agree with it.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["is_legacy_argv"]


def is_legacy_argv(argv: Sequence[str]) -> bool:
    """``argv`` (without the program name) is the legacy shell's, not a subcommand.

    Empty argv and a flag-shaped first argument: the legacy parser accepts no
    positionals, so anything else can only be a subcommand or a typo of one,
    and both are ``cli.main``'s to name.
    """
    return not argv or argv[0].startswith("-")
