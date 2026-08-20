"""Guards for the common/ layer's two prose-only contracts.

Neither invariant is exercised anywhere else:

- the compat shims at the old ``domains/perp`` paths keep re-exporting the
  same objects — every in-tree importer was repointed to ``common``, so
  without these assertions a rotted shim would first break an out-of-tree
  consumer (deploy/paper cherry-picks), not the test suite;
- ``common/`` stays at the bottom of the import graph — the rule in
  ``common/__init__``'s docstring that nothing there imports from another
  ``hyperliquid_perp`` package would otherwise be enforced by review only.
"""

from __future__ import annotations

import ast
from pathlib import Path

from contrib.hyperliquid_perp import common as common_pkg
from contrib.hyperliquid_perp.common import config_coercion, decimal_context, enum_guard
from contrib.hyperliquid_perp.domains.perp import (
    config_coercion as coercion_shim,
    enum_guard as enum_shim,
    margin,
)


def test_the_old_domains_paths_still_reexport_the_common_objects():
    # Identity, not equality: a shim that re-declared its own copy would keep
    # equal behavior today but fork the definition the next time one side moves.
    assert enum_shim.check_enum is enum_guard.check_enum
    assert enum_shim.__all__ == enum_guard.__all__
    assert coercion_shim.__all__ == config_coercion.__all__
    for name in config_coercion.__all__:
        assert getattr(coercion_shim, name) is getattr(config_coercion, name), name
    assert margin.DECIMAL_CONTEXT is decimal_context.DECIMAL_CONTEXT


def test_common_imports_nothing_from_the_rest_of_the_package():
    # Structural check on the import statements themselves (not runtime state,
    # which depends on what happens to be imported first): a relative import
    # reaching above common/ (level >= 2) or an absolute import of the contrib
    # package both violate the bottom-of-the-import-graph rule. Sibling
    # imports inside common/ (level 1) stay legal.
    offenders = []
    for source in Path(common_pkg.__path__[0]).glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level >= 2 or (node.module or "").startswith("contrib."):
                    offenders.append(f"{source.name}: from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{source.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("contrib.")
                )
    assert not offenders, offenders
