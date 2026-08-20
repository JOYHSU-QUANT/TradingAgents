"""Compatibility re-export — the YAML-coercion seam now lives in :mod:`...common.config_coercion`.

The helpers were extracted to the dependency-free ``common`` layer (they carry
no decision-contract logic and are consumed by config dataclasses in every
layer). This module stays as a re-export so existing imports keep working; new
code should import from ``common.config_coercion`` directly.
"""

from __future__ import annotations

from ...common.config_coercion import (
    bool_from_yaml,
    config_overrides,
    decimal_from_yaml,
    int_from_yaml,
    str_from_yaml,
)

__all__ = [
    "bool_from_yaml",
    "config_overrides",
    "decimal_from_yaml",
    "int_from_yaml",
    "str_from_yaml",
]
