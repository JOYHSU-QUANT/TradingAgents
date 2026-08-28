"""Shared YAML-coercion seam for config dataclasses.

Generic "YAML scalar → typed value" helpers with no decision-contract logic:
:func:`config_overrides` builds coerced kwargs for a config dataclass from a
raw YAML block, and :func:`decimal_from_yaml` / :func:`int_from_yaml` /
:func:`bool_from_yaml` / :func:`str_from_yaml` are the scalar converters it
dispatches to. Used by
``risk_gate.RiskConfig``, ``target_decision.DecisionConfig``,
``market_data_config.MarketDataConfig``, ``paper.config.PaperTradingConfig``,
and ``live.config.LiveConfig``, so they live here rather than in any one
domain module.

Everything here is pure (no I/O, no clock); amounts are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "bool_from_yaml",
    "config_overrides",
    "decimal_from_yaml",
    "int_from_yaml",
    "str_from_yaml",
]


def bool_from_yaml(value: object) -> bool:
    """Coerce a YAML scalar to bool, accepting only genuine YAML booleans.

    ``bool(value)`` would read any non-empty string — including ``"false"``
    quoted by accident — as True. For gate fields like ``allow_real_orders``
    that inversion is the difference between a dry run and real money, so
    anything that is not already a bool fails loud.
    """
    if isinstance(value, bool):
        return value
    raise ValueError(f"expected true/false, got {value!r}")


def decimal_from_yaml(value: object) -> Decimal:
    """Coerce a YAML scalar to Decimal via ``str`` so no float digits are lost.

    Booleans are rejected: YAML 1.1 reads ``yes``/``no`` as bools, and
    ``Decimal(str(True))`` would otherwise die as an opaque
    ``InvalidOperation`` instead of a config error naming the value.

    Non-finite values are rejected too: YAML ``.nan`` / ``.inf`` / ``-.inf``
    parse to floats that ``Decimal`` accepts without complaint, and the config
    dataclasses' own range checks (``leverage <= 0`` and the like) then raise
    ``decimal.InvalidOperation`` — an ``ArithmeticError`` no config-error
    handler catches, so the operator saw a traceback instead of the key name
    (issue #128). Refusing here keeps every downstream comparison finite.
    """
    if isinstance(value, bool):
        raise ValueError(f"expected a number, got a YAML boolean ({value!r})")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"expected a number, got {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"expected a finite number, got {value!r}")
    return result


def int_from_yaml(value: object) -> int:
    """Coerce a YAML scalar to int, rejecting bools and non-integral numbers.

    A bare ``int()`` would silently accept ``no``/``yes`` (YAML bools → 0/1)
    and truncate ``59.9`` to ``59`` — for risk-limit fields a config typo must
    fail loud, never silently shift the limit.
    """
    if isinstance(value, bool):
        raise ValueError(f"expected an integer, got a YAML boolean ({value!r})")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"expected an integer, got {value!r}")
        return int(value)
    try:
        return int(value)  # int/numeric string passes through
    except (TypeError, ValueError):
        # A non-numeric string raises ValueError; a list/dict (YAML indentation
        # slip) raises TypeError. Normalise both to ValueError so config_overrides
        # surfaces a named config error instead of leaking an unnamed TypeError.
        raise ValueError(f"expected an integer, got {value!r}") from None


def str_from_yaml(value: object) -> str:
    """Coerce a YAML scalar to str, accepting only genuine YAML strings.

    ``str(value)`` would render any scalar — YAML ``true`` becomes ``"True"``,
    a bare ``0x123`` parsed as an int becomes its decimal rendering — and a
    field validated by an *open* pattern (a regex or non-empty check, e.g.
    ``live.order_owner_prefix``) would accept the rendering as if the operator
    wrote it. A wrong YAML type must fail loud, not pass as its repr.
    """
    if isinstance(value, str):
        return value
    raise ValueError(f"expected a string, got {value!r}")


def config_overrides(
    cfg: dict | None, converters: Mapping[str, Callable[[Any], Any]]
) -> dict[str, Any]:
    """Coerced kwargs for a config dataclass from a raw YAML block.

    The single YAML-coercion seam shared by every config dataclass (see the
    module docstring for the consumer list — it is not duplicated here so the
    two can never drift). Only keys that are present *and* non-null are
    returned, so an absent or blank YAML key falls back to the dataclass field
    default — each default is declared exactly once, on the field. A value the
    converter rejects re-raises with the config key named, so the operator
    sees *which* setting is bad.

    An unrecognised key inside the block is *rejected*, not ignored: a typo like
    ``max_target_margin_pt`` would otherwise silently drop the intended value and
    leave a safety-critical limit on its permissive default with no signal.
    """
    cfg = cfg or {}
    if not isinstance(cfg, dict):
        # A truthy non-mapping block (``risk: 60``) would otherwise raise a
        # TypeError from ``set(cfg)`` that escapes main's ValueError config-error
        # handler and exits 2 — surface it as a named config error instead.
        raise ValueError(f"expected a mapping, got {cfg!r}")
    unknown = set(cfg) - set(converters)
    if unknown:
        raise ValueError(f"unknown config key(s): {', '.join(map(repr, sorted(unknown)))}")
    out: dict[str, Any] = {}
    for key, conv in converters.items():
        raw = cfg.get(key)
        if raw is None:
            continue
        try:
            out[key] = conv(raw)
        except ValueError as exc:
            raise ValueError(f"config key {key!r}: {exc}") from None
    return out
