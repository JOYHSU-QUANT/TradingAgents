"""Typed view of the YAML ``market_data:`` block.

The same ``config_overrides`` + frozen-dataclass seam as ``risk:`` /
``decision:`` / ``paper_trading:`` / ``live:``, so an unknown key is rejected
rather than silently falling back to a default (issue #96 — for
``funding_zscore_window_days`` that fallback was a z-score over the wrong
window that still looked like a normal number), and each default is declared
once, on its field.

Import budget: ``config.load_config`` parses this block on every load, and the
loader's structural rule (``tests/common/test_layering.py``) is that it drags
no compute module in. So this module imports only ``common`` and
:mod:`.schema` — the stdlib-only DTO module that owns the candle-interval
vocabulary — and the layering test pins that too.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...common.config_coercion import config_overrides, int_from_yaml, str_from_yaml
from ...common.constants import MIN_VOLUME_PROFILE_WINDOW
from .schema import interval_to_ms

__all__ = ["MarketDataConfig"]


def _signal_path_from_yaml(value: object) -> str:
    """The research-signal switch: a path to the handoff document, or ``""`` for off.

    Its own converter rather than :func:`str_from_yaml`, for one reason that
    is worth a function: YAML 1.1 reads a bare ``off`` — the obvious thing to
    write for a switch, and the shorthand the plan's own §7 uses — as the
    BOOLEAN false. ``str_from_yaml`` would refuse it with "expected a string,
    got False", which is true and says nothing about what to write instead.
    ``no`` and ``false`` are the same trap. So the boolean case is answered by
    name, with both legal spellings in the sentence.
    """
    if isinstance(value, bool):
        raise ValueError(
            f"expected a path to the research signal document, got a YAML boolean ({value!r}) — "
            f'YAML reads a bare off/no/false as one; leave the key out (or write "") to keep '
            f"the section off, and quote the path to turn it on"
        )
    if not isinstance(value, str):
        raise ValueError(f"expected a path to the research signal document, got {value!r}")
    return value


@dataclass(frozen=True)
class MarketDataConfig:
    """The ``market_data:`` block, parsed and cross-checked.

    ``volume_profile_window_candles`` is ``0`` (off) by default — see the
    module docstring of :mod:`.volume_profile` for why the feature ships
    switched off, and ``hyperliquid.example.yaml`` for the operator-facing
    contract on the value. ``autoresearch_signal`` is the same kind of
    switch, spelled as the empty string because its "on" value is a path (see
    :mod:`.research_signal`).
    """

    candle_interval: str = "4h"
    candle_lookback: int = 200
    funding_zscore_window_days: int = 30
    volume_profile_window_candles: int = 0
    autoresearch_signal: str = ""

    def __post_init__(self) -> None:
        # The same lookup the context and the fetch use, so the vocabulary
        # (and the message naming the legal set) lives once, in schema.py. A
        # mis-cased ``4H`` used to survive the load and raise a bare ValueError
        # from inside the market fetch.
        try:
            interval_to_ms(self.candle_interval)
        except ValueError as exc:
            raise ValueError(f"'market_data.candle_interval': {exc}") from None
        if self.candle_lookback < 1:
            raise ValueError(
                f"'market_data.candle_lookback' must be >= 1, got {self.candle_lookback}"
            )
        # ``PerpMarketContext`` refuses a sub-1-day window at construction (the
        # window filter would keep nothing and the z-score would degrade to
        # None, indistinguishable from a data shortage); refusing it here moves
        # the same failure from inside the cycle to the config load.
        if self.funding_zscore_window_days < 1:
            raise ValueError(
                f"'market_data.funding_zscore_window_days' must be >= 1, "
                f"got {self.funding_zscore_window_days}"
            )
        # Every way of getting the volume-profile window wrong fails SILENTLY at
        # runtime — the section simply never appears in the prompt, which is
        # indistinguishable from the feature being off on purpose (``0``) — so
        # each is a named load-time failure instead.
        window = self.volume_profile_window_candles
        if window < 0:
            raise ValueError(
                f"'market_data.volume_profile_window_candles' must be >= 0 "
                f"(0 disables the volume profile), got {window}"
            )
        if 0 < window < MIN_VOLUME_PROFILE_WINDOW:
            raise ValueError(
                f"'market_data.volume_profile_window_candles' must be 0 (off) or at "
                f"least {MIN_VOLUME_PROFILE_WINDOW}; a window of {window} candle(s) is too "
                f"short for a meaningful profile and would be skipped on every cycle"
            )
        # Cross-check against the history actually fetched: a window wider than
        # the lookback can never be filled, so the section would be silently
        # absent forever. Both fields are parsed by now, so the check reads the
        # same values the fetch and the profile cut will use.
        if window > self.candle_lookback:
            raise ValueError(
                f"'market_data.volume_profile_window_candles' ({window}) exceeds "
                f"'market_data.candle_lookback' ({self.candle_lookback}) — the window could "
                f"never be filled and the volume profile would be skipped on every cycle"
            )
        # Same rule as the window above, for the same reason: every way of
        # getting this key wrong fails SILENTLY at runtime — the prompt simply
        # has no research-signal section, which is exactly what ``""`` means on
        # purpose. A value that is only whitespace is the reachable case (a
        # trailing space after the colon, a key someone half-deleted), so it
        # is a named load-time failure rather than an accidental off switch.
        #
        # What is deliberately NOT checked here: whether the file exists. It is
        # written out of band by the research radar, so an operator who turns
        # the switch on before the first producer run has a legal config and a
        # per-cycle WARNING (``research_signal``) that names the path it looked
        # for — refusing to start would be refusing a normal order of
        # operations. The whitespace case has no such excuse: nothing will ever
        # write to a path made of spaces.
        if self.autoresearch_signal != self.autoresearch_signal.strip():
            raise ValueError(
                f"'market_data.autoresearch_signal' has leading or trailing whitespace "
                f"({self.autoresearch_signal!r}); write the path without it"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> MarketDataConfig:
        """Parse the raw YAML block; absent or null keys use the field defaults."""
        return cls(
            **config_overrides(
                cfg,
                {
                    "candle_interval": str_from_yaml,
                    "candle_lookback": int_from_yaml,
                    "funding_zscore_window_days": int_from_yaml,
                    "volume_profile_window_candles": int_from_yaml,
                    "autoresearch_signal": _signal_path_from_yaml,
                },
            )
        )
