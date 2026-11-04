"""Which optional inputs an engine run went without (issue #399).

The router degrades an optional category's failed vendor chain to the
``DATA_UNAVAILABLE`` sentinel: the analyst reads the section as absent and
the cycle decides as usual. That is the right ending for one cycle and the
wrong silence for forty of them — StockTwits answered the paper host a 403
on 227 cycles in a row before anyone looked (#395), because nothing counted
the sentinel across cycles. This module is the count's first hop: one
engine run's set of categories whose tool call ended in that sentinel,
read off the router's own WARNING. The router stamps the record with the
category (``interface.UNAVAILABLE_CATEGORY_ATTR``) so the collector keys on
a field, never on the line's words. The handler sits on the router's
logger — a process-wide object — for exactly the run's duration: the
daemon drives one engine run at a time (the busy gate serialises them),
so the set is that run's; a second run in the same process during the
block would be merged into it, which no deployment does today.

From here the set rides the parsed decision (``ParsedDecision
.unavailable_inputs``) into the ``ai_outputs`` row (schema v15,
``unavailable_inputs``: the sorted categories joined with commas, the empty
string when the run recorded none, NULL where no run recorded a set — a
cycle resumed from its stored text, the guardrail's own no-decision),
where ``status --json`` carries it
per recent cycle and the dashboard's ``input_unavailable`` rule counts a
category's streak. A run that raised before deciding leaves no row and no
count, as it leaves no verdict.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from tradingagents.dataflows import interface
from tradingagents.dataflows.interface import UNAVAILABLE_CATEGORY_ATTR

__all__ = ["ROUTER_LOGGER", "UnavailableInputCollector", "collect_unavailable_inputs"]

#: The logger the router's optional-category WARNING is written to — read off
#: the module's own logger, so the next upstream sync (which renames
#: ``interface.py`` to ``router.py``) moves this with it instead of leaving
#: the collector listening on a name nothing writes to.
ROUTER_LOGGER = interface.logger.name


class UnavailableInputCollector(logging.Handler):
    """Collect the categories the router's optional-lane WARNING names."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.categories: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        category = getattr(record, UNAVAILABLE_CATEGORY_ATTR, None)
        if isinstance(category, str) and category:
            self.categories.add(category)


@contextmanager
def collect_unavailable_inputs() -> Iterator[set[str]]:
    """Attach a collector to the router's logger for the block; yield its set.

    The set is live during the block and complete after it. Attached to the
    named logger, not the root, so an application that configures no
    logging still feeds it (the handler is consulted on the logger before
    propagation). The line is a WARNING: a logging setup that silences the
    router's warnings would silence the count with them, so the logger's
    level is held at WARNING for the block and put back after (as the
    uniswap judge's news watch does). Detached in ``finally`` so a run that
    raises leaves no handler behind to collect the next run's categories
    into this one's.
    """
    collector = UnavailableInputCollector()
    router = logging.getLogger(ROUTER_LOGGER)
    level_before = router.level
    if not router.isEnabledFor(logging.WARNING):
        router.setLevel(logging.WARNING)
    router.addHandler(collector)
    try:
        yield collector.categories
    finally:
        router.removeHandler(collector)
        router.setLevel(level_before)
