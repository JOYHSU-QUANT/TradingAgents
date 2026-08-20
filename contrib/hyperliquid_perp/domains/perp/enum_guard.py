"""Compatibility re-export — :func:`check_enum` now lives in :mod:`...common.enum_guard`.

The guard was extracted to the dependency-free ``common`` layer (it was never
domain logic — persistence and paper both import it). This module stays as a
re-export so existing imports keep working; new code should import from
``common.enum_guard`` directly.
"""

from __future__ import annotations

from ...common.enum_guard import check_enum

__all__ = ["check_enum"]
