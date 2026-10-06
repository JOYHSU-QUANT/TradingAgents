"""One real question to the judge: the engine, the gateway and its key, end to end.

::

    python -m dotenv run -- pytest -m smoke contrib/uniswap_v3/tests/test_agent_smoke.py -s

It runs the whole graph once on ETH-USD as of today, some fifteen to
twenty completions at the configured provider, and prints the rating and
how long it took. The provider's key (``OPENROUTER_API_KEY`` for the
default settings) must be in the environment; without it the test is
skipped. The engine's logs and cache go under the test's temporary
directory, and the judge's words are written as a sidecar there.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.uniswap_v3.agent.context import BARS_NEEDED, spot_context
from contrib.uniswap_v3.agent.graph import PROMPT_VERSION, TradingAgentsJudge
from contrib.uniswap_v3.agent.record import sidecar_path, sidecar_record, write_sidecar
from contrib.uniswap_v3.agent.settings import AgentSettings
from contrib.uniswap_v3.agent.verdicts import trade_date_of
from contrib.uniswap_v3.domain.verdicts import Rating

pytestmark = pytest.mark.smoke


def test_the_judge_answers_on_eth_usd_with_a_rating_and_its_reports(tmp_path):
    settings = AgentSettings()
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY is not set")
    now = int(datetime.now(tz=timezone.utc).timestamp())
    time = now - now % 86_400
    # A flat series stands in for the store's closes: the plumbing is what is tried here.
    closes = [Decimal("2000") + Decimal(step) for step in range(BARS_NEEDED)]
    context = spot_context(
        closes,
        symbol="WETH",
        ticker="ETH-USD",
        quote="USDC",
        traded=["WBTC", "WETH"],
        time=time,
        interval_seconds=86_400,
    )
    judge = TradingAgentsJudge(settings, tmp_path / "tradingagents")

    answer = judge.ask("ETH-USD", trade_date_of(time), context)

    print(
        f"\nETH-USD as of {trade_date_of(time)}: {answer.rating.value} by {judge.model} in "
        f"{answer.elapsed_seconds:.0f} s; {len(answer.decision)} characters of decision"
    )
    assert isinstance(answer.rating, Rating)
    assert answer.decision.strip()
    assert answer.reports is not None
    assert answer.reports["selected_analysts"] == list(settings.selected_analysts)
    assert isinstance(answer.reports["market_report"], str) and answer.reports["market_report"]
    assert answer.reports["final_trade_decision"] == answer.decision
    # The engine wrote its run log where it was pointed, not under the home directory.
    assert any((tmp_path / "tradingagents" / "logs").rglob("*.json"))
    relative = sidecar_path("smoke", "WETH", time)
    digest = write_sidecar(
        tmp_path,
        relative,
        sidecar_record(
            answer,
            source="smoke",
            symbol="WETH",
            ticker="ETH-USD",
            time=time,
            trade_date=trade_date_of(time),
            context=context,
            model=judge.model,
            prompt_version=PROMPT_VERSION,
            asked_at=now,
        ),
    )
    written = json.loads((tmp_path / relative).read_text(encoding="utf-8"))
    assert written["rating"] == answer.rating.value and len(digest) == 64
    print(f"sidecar: {tmp_path / relative}")
