"""The optional-input collector (#399): what it keys on and when it listens."""

from __future__ import annotations

import logging

import pytest

from contrib.hyperliquid_perp.integration.unavailable_inputs import (
    ROUTER_LOGGER,
    UnavailableInputCollector,
    collect_unavailable_inputs,
)
from tradingagents.dataflows.interface import UNAVAILABLE_CATEGORY_ATTR


def _warn(category: str | None, message: str = "Optional x unavailable for y: z") -> None:
    extra = {} if category is None else {UNAVAILABLE_CATEGORY_ATTR: category}
    logging.getLogger(ROUTER_LOGGER).warning(message, extra=extra)


def test_the_collector_keys_on_the_record_attribute_not_the_words():
    with collect_unavailable_inputs() as collector:
        _warn("news_data")
        _warn("news_data")  # once per run, however many tool calls ended there
        _warn("macro_data", message="some other wording entirely")
        _warn(None, message="Optional news_data unavailable for get_news: the words alone")
        _warn("", message="an empty attribute is no category")
    assert collector.categories == {"news_data", "macro_data"}
    assert collector.recorded == ("macro_data", "news_data")
    assert not collector.deaf


def test_a_run_that_went_without_nothing_records_the_empty_set_not_none():
    with collect_unavailable_inputs() as collector:
        pass
    assert collector.recorded == ()


def test_the_collector_listens_only_for_the_block_and_detaches_on_a_raise():
    router = logging.getLogger(ROUTER_LOGGER)
    before = list(router.handlers)
    _warn("options_data")  # before the block: not this run's
    with pytest.raises(RuntimeError), collect_unavailable_inputs() as collector:
        _warn("news_data")
        raise RuntimeError("the engine raised")
    assert collector.categories == {"news_data"}
    assert router.handlers == before
    _warn("macro_data")  # after the block: not this run's either
    assert collector.categories == {"news_data"}


def test_the_collector_hears_the_line_even_when_the_routers_logger_is_silenced():
    # A logging setup above WARNING on the router's logger would silence the
    # count with it; the block holds the level and puts it back after.
    router = logging.getLogger(ROUTER_LOGGER)
    before = router.level
    router.setLevel(logging.ERROR)
    try:
        with collect_unavailable_inputs() as collector:
            assert router.isEnabledFor(logging.WARNING)
            _warn("news_data")
        assert collector.recorded == ("news_data",)
        assert router.level == logging.ERROR
    finally:
        router.setLevel(before)


def test_a_collector_the_logger_would_not_feed_records_unknown_and_says_so(caplog):
    # What a level cannot repair — ``logging.disable`` here — makes the
    # collector deaf: the run's set is "not recorded", never the empty set
    # that reads as every input having answered, and the journal says why
    # while ERROR is still audible. WARNING, not ERROR: a disable at ERROR
    # would silence the very line this asserts on, and pytest's caplog
    # lowers a global disable to just below the level it captures anyway,
    # so that case would be exercised as this one; said plainly.
    logging.disable(logging.WARNING)
    try:
        with (
            caplog.at_level(
                logging.ERROR, logger="contrib.hyperliquid_perp.integration.unavailable_inputs"
            ),
            collect_unavailable_inputs() as collector,
        ):
            _warn("news_data")
    finally:
        logging.disable(logging.NOTSET)
    assert collector.deaf and collector.recorded is None
    assert collector.categories == set()
    assert "recorded as unknown, not as none" in caplog.text


def test_the_router_logger_name_is_the_modules_own():
    # Read off the module, so the next upstream rename of interface.py
    # moves the collector with it instead of leaving it on a dead name.
    from tradingagents.dataflows import interface

    assert interface.logger.name == ROUTER_LOGGER
    assert UnavailableInputCollector().level == logging.WARNING
