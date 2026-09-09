"""The market analyst's indicator menu: which indicators that prompt offers, grouped.

The sentences belong to the data layer — ``dataflows.utils.INDICATOR_DESCRIPTIONS``
is the one table both report lanes end their reports with, and the menu is
rendered from it rather than carrying a third verbatim copy (#187). What
lives here is the part that is a fact about the PROMPT and about nothing
else: the five category titles, their order, and the decision that one
described indicator is not offered. That is agent-layer knowledge, and it
used to sit in ``dataflows.utils`` beside the sentences, where a reader
looking for what the analyst is asked to choose from had to go through the
data layer to find it (#219).

Moving it changed no text: the menu is byte-identical to the literal the
prompt carried before the derivation, and the golden test moved with it.
"""

from __future__ import annotations

from tradingagents.dataflows import utils as dataflows_utils

# The menu: the shared table, grouped and ordered the way this prompt has
# always listed them. The grouping is the prompt's only fact of its own —
# the sentences are the table's. ``mfi`` is described (the yfinance vendor
# computes it) but has never been offered to the analyst, and offering it
# would change what the analyst is asked to choose from — an input-side
# change to every run — so the omission is declared, and the partition below
# keeps a newly described indicator from silently joining or missing the menu.
INDICATOR_MENU = (
    ("Moving Averages", ("close_50_sma", "close_200_sma", "close_10_ema")),
    ("MACD Related", ("macd", "macds", "macdh")),
    ("Momentum Indicators", ("rsi",)),
    ("Volatility Indicators", ("boll", "boll_ub", "boll_lb", "atr")),
    ("Volume-Based Indicators", ("vwma",)),
)
INDICATOR_MENU_OMITS = frozenset({"mfi"})

# Checked at import, not only by the partition test: a described indicator
# placed in neither the menu nor the omissions would otherwise drop out of
# the prompt silently in an environment that never ran the tests.
_MENU_KEYS = [key for _, keys in INDICATOR_MENU for key in keys]
assert len(_MENU_KEYS) == len(set(_MENU_KEYS)), "INDICATOR_MENU lists an indicator twice"
assert set(_MENU_KEYS).isdisjoint(INDICATOR_MENU_OMITS), "INDICATOR_MENU lists an omitted indicator"
assert set(_MENU_KEYS) | INDICATOR_MENU_OMITS == set(dataflows_utils.INDICATOR_DESCRIPTIONS), (
    "every described indicator must be placed in INDICATOR_MENU or INDICATOR_MENU_OMITS"
)


def indicator_menu() -> str:
    """The analyst's indicator menu, rendered from ``INDICATOR_DESCRIPTIONS``.

    One block per ``INDICATOR_MENU`` category — its title, a colon, then one
    ``- name: description`` line per indicator — blocks separated by a blank
    line. Byte-identical to the menu the market analyst's prompt carried as
    a literal, which is what keeps this derivation from being an input-side
    change to the analyst (pinned by a golden test against that literal).
    Reads the table through the module name at call time, so a test can
    alter one sentence and watch the menu follow.
    """
    return "\n\n".join(
        f"{title}:\n"
        + "\n".join(f"- {key}: {dataflows_utils.INDICATOR_DESCRIPTIONS[key]}" for key in keys)
        for title, keys in INDICATOR_MENU
    )
