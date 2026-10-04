"""Pure value types and math: standard library only, no chain, no store, no clock.

Everything here is type-checked in CI by a job that installs mypy alone, so
a module in this package may import the standard library and its own
siblings and nothing else (``tests/test_isolation.py`` holds that).
"""
