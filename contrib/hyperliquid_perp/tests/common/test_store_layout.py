"""The one payload-directory layout (issue #221) and its one definition.

The daemons write raw payloads under ``<db dir>/payloads/<run_id>/`` and the
offline backfill reads them back from there on a copied store, so the
segments are a contract between writers and reader. They are pinned here,
once; the writer-side pins (``tests/cli``) assert identity with this helper
rather than re-spelling them, and the source scan below is what keeps a
fifth hand-copy from creeping back into production code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from contrib import hyperliquid_perp as package
from contrib.hyperliquid_perp.common import store_layout
from contrib.hyperliquid_perp.common.store_layout import PAYLOADS_DIRNAME, payload_dir

from ..conftest import package_sources


def test_the_layout_is_the_stores_absolute_parent_then_payloads_then_the_run(tmp_path, monkeypatch):
    # Segment by segment, so a failure names which one moved.
    db = tmp_path / "srv" / "paper_trading.db"  # need not exist
    got = payload_dir(db, "paper-BTC-4")
    assert got.name == "paper-BTC-4"
    assert got.parent.name == PAYLOADS_DIRNAME == "payloads"
    assert got.parent.parent == (tmp_path / "srv").resolve()
    assert got.is_absolute()
    assert not got.exists(), "the helper derives; each writer creates on first use"

    # A relative --db (the common invocation) resolves against the cwd, so
    # the recorded paths survive a later chdir; a str is accepted because
    # every cli site holds ``args.db`` as one.
    (tmp_path / "srv").mkdir()
    monkeypatch.chdir(tmp_path / "srv")
    assert payload_dir("paper_trading.db", "r") == got.parent / "r"
    assert payload_dir(Path("paper_trading.db"), "r") == payload_dir("paper_trading.db", "r")


def _path_segment_literals(tree: ast.AST) -> Iterator[str]:
    """Every string literal in ``tree`` that names a path segment, docstrings aside.

    Structural rather than a regex over the text: a hand-copy of the recipe
    can be spelled ``p / "payloads"``, ``p / 'payloads'``,
    ``os.path.join(p, "payloads", r)`` or ``f"{p}/payloads/{r}"``, and all
    four put the segment in a string constant somewhere in the AST — the
    f-string's as a ``Constant`` inside its ``JoinedStr``. Docstrings are
    constants too, and they legitimately describe the layout in prose, so the
    first statement of a module / class / function body is skipped.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            yield from re.split(r"[/\\]", node.value)


def test_the_middle_segment_is_spelled_once_in_production_code():
    # The recipe used to be hand-copied at four cli sites plus the backfill
    # hint; the bug class is a rename at one of them. Every production module
    # that builds — or names — the directory has to go through the helper, so
    # the segment may appear as a string constant nowhere else (prose in
    # docstrings and comments excepted; a hint that prints the layout takes
    # ``PAYLOADS_DIRNAME``). Text this test cannot see: a literal assembled
    # at runtime (``"pay" + "loads"``) — accepted, nobody writes that by
    # accident.
    root = Path(package.__path__[0])
    tests_dir = root / "tests"
    helper = Path(store_layout.__file__).resolve()  # the one definition
    offenders = []
    for source in package_sources(package):
        if tests_dir in source.parents or source.resolve() == helper:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        if PAYLOADS_DIRNAME in set(_path_segment_literals(tree)):
            offenders.append(source.relative_to(root).as_posix())
    assert offenders == [], offenders


def test_the_segment_scan_sees_every_spelling_but_not_prose(tmp_path):
    # The scan's own coverage, on a synthetic module: the four spellings a
    # hand-copy could take are each caught, the docstring's prose is not.
    module = tmp_path / "m.py"
    module.write_text(
        '"""Writes under <db dir>/payloads/<run_id>/ (prose, allowed)."""\n'
        "import os\n"
        "def f(p, r):\n"
        '    """Also prose: payloads/<run_id>."""\n'
        '    a = p / "payloads" / r\n'
        "    b = p / 'payloads' / r\n"
        '    c = os.path.join(p, "payloads", r)\n'
        '    d = f"{p}/payloads/{r}"\n'
        '    e = "and payloads/orderStatus-*.json)"\n'
        "    return a, b, c, d, e\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    segments = list(_path_segment_literals(tree))
    assert segments.count(PAYLOADS_DIRNAME) == 4, segments
    assert "and payloads" in segments  # the prose-shaped message is not a segment hit
    prose_only = ast.parse('"""<db dir>/payloads/<run_id>/"""\ndef f():\n    """payloads/"""\n')
    assert list(_path_segment_literals(prose_only)) == []
