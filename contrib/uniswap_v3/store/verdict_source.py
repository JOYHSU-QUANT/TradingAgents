"""The verdicts a run's view carries at a bar, picked from the store by its config.

:func:`load_verdicts` is to verdicts what :func:`.bar_source.load_bar` is
to bars: it reads what the config's source said at one boundary, keeps the
verdicts on the tokens the config trades, and hands them to whoever builds
the :class:`~..domain.types.MarketView`. A config that reads no verdicts
(no ``verdicts`` section) gets none, and the view's ``verdict_source``,
set from the same config, is what says whether the run reads any.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ..config import UniswapConfig
from ..domain.verdicts import Verdict
from .repository import Store

__all__ = ["load_verdicts"]

_NONE: Final[Mapping[str, Verdict]] = MappingProxyType({})


def load_verdicts(store: Store, config: UniswapConfig, time: int) -> Mapping[str, Verdict]:
    """What the config's source said at the bar ``time``, by token; empty when nothing.

    Only the tokens the config trades (:attr:`~..config.UniswapConfig.traded_symbols`)
    are kept: a verdict on another token says nothing a strategy under this
    config could act on. The mapping is empty when the config reads no
    verdicts, or its source said nothing at this bar or nothing on those
    tokens. A stored row that no longer reads as a verdict raises
    :class:`~.repository.StoreError`.
    """
    if config.verdicts is None:
        return _NONE
    traded = config.traded_symbols
    return MappingProxyType(
        {
            record.verdict.symbol: record.verdict
            for record in store.verdicts_at(config.verdicts.source, time)
            if record.verdict.symbol in traded
        }
    )
