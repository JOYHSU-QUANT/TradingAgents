"""The targets a run's view carries at a bar, picked from the store by its config.

:func:`load_targets` is to targets what :func:`.verdict_source.load_verdicts`
is to verdicts: it reads what the config's source set at one boundary,
keeps the targets on the tokens the config trades, and hands them to
whoever builds the :class:`~..domain.types.MarketView`. A config that
reads no targets (no ``targets`` section) gets none, and the view's
``target_source``, set from the same config, is what says whether the run
reads any.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ..config import UniswapConfig
from ..domain.targets import Target
from .repository import Store

__all__ = ["load_targets"]

_NONE: Final[Mapping[str, Target]] = MappingProxyType({})


def load_targets(store: Store, config: UniswapConfig, time: int) -> Mapping[str, Target]:
    """What the config's source set at the bar ``time``, by token; empty when nothing.

    Only the tokens the config trades (:attr:`~..config.UniswapConfig.traded_symbols`)
    are kept: a target on another token is nothing a strategy under this
    config could hold. The mapping is empty when the config reads no
    targets, or its source set nothing at this bar or nothing on those
    tokens. A stored row that no longer reads as a target raises
    :class:`~.repository.StoreError`.
    """
    if config.targets is None:
        return _NONE
    traded = config.traded_symbols
    return MappingProxyType(
        {
            record.target.symbol: record.target
            for record in store.targets_at(config.targets.source, time)
            if record.target.symbol in traded
        }
    )
