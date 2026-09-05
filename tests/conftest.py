"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture(autouse=True)
def _clear_throttle_latch():
    """Clear the process-global vendor throttle latch around each test.

    ``yf_retry`` and ``route_to_vendor`` remember a vendor's throttle for
    minutes so a cycle's other tool calls stop re-discovering it (#86, #114).
    Without this, one test that drives a vendor into the rate-limit lane would
    send every later test in the same process down the skip path instead of
    the code it means to exercise.
    """
    from tradingagents.dataflows.stockstats_utils import reset_yf_throttle_latch
    from tradingagents.dataflows.throttle import VENDOR_THROTTLE_LATCH

    reset_yf_throttle_latch()
    VENDOR_THROTTLE_LATCH.reset()
    yield
    reset_yf_throttle_latch()
    VENDOR_THROTTLE_LATCH.reset()


def _budget_must_not_sleep(seconds):
    raise AssertionError(
        f"the SoSoValue request budget tried to sleep {seconds:.1f}s inside a test; "
        "drive the budget with a stepped clock instead of wall time"
    )


@pytest.fixture(autouse=True)
def _fresh_sosovalue_budget(monkeypatch):
    """Give every test a fresh SoSoValue request budget that cannot sleep.

    The budget is process-global and remembers the last minute of sends and
    any 429 (#189). Without this, twenty tests that each push one request
    through ``_request`` would put the twenty-first to sleep for real, and a
    single 429 test would park every later one for a minute. The sleep guard
    turns any such leak into a failure at the point it happens.
    """
    from tradingagents.dataflows import sosovalue_common

    monkeypatch.setattr(sosovalue_common, "_BUDGET", sosovalue_common._RequestBudget())
    monkeypatch.setattr(sosovalue_common, "_sleep", _budget_must_not_sleep)


@pytest.fixture()
def frozen_clock(monkeypatch):
    """A settable monotonic clock, so latch windows are stepped, not slept.

    Patched on the ``time`` module itself, which is the one object the latch
    and ``yf_retry``'s backoff both read; ``sleep`` is a no-op for the same
    reason.
    """
    import time

    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return clock


def repo_text(name: str) -> str:
    """Text of a file at the repository root (``README.md``, ``.env.example``).

    For pins that assert a rendered constant is quoted in a root document;
    anchored here so no pin re-derives its own ``parents[N]`` index.
    """
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")


def provider_kwargs_for(config: dict) -> dict:
    """``TradingAgentsGraph._get_provider_kwargs`` for ``config``, on a bare
    instance — no graph (and no LLM client) is built.

    The one spelling of the ``__new__``-without-``__init__`` recipe; the method
    reads only ``self.config`` today, and if that ever changes this is the one
    place to fix.
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = config
    return TradingAgentsGraph._get_provider_kwargs(graph)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
