"""The strategies a config can name, and the factory behind each name.

A config says ``strategy: {name, params}``; :func:`build_strategy` hands the
params to the factory registered under the name, and the factory checks
them. To add a strategy, implement :class:`~..ports.Strategy` and add its
factory to ``_FACTORIES``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from ..ports import Strategy
from .fixed_weights import FixedWeights
from .trend_vol_weights import TrendVolWeights

__all__ = ["build_strategy", "strategy_names"]

_FACTORIES: Final[Mapping[str, Callable[[Mapping[str, object]], Strategy]]] = MappingProxyType(
    {
        "fixed_weights": FixedWeights.from_params,
        "trend_vol_weights": TrendVolWeights.from_params,
    }
)


def strategy_names() -> tuple[str, ...]:
    """Every name :func:`build_strategy` accepts, sorted."""
    return tuple(sorted(_FACTORIES))


def build_strategy(name: str, params: Mapping[str, object]) -> Strategy:
    """The strategy registered as ``name``, built from ``params``."""
    if not isinstance(name, str) or name not in _FACTORIES:
        raise ValueError(f"unknown strategy {name!r}; known: {list(strategy_names())}")
    return _FACTORIES[name](params)
