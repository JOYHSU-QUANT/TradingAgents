"""One visit's asking: which tokens, what is recorded, what is kept when the judge fails."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from contrib.uniswap_v3.agent import tickers as _tickers, verdicts as _verdicts
from contrib.uniswap_v3.agent.errors import BarNotStored, JudgeUnavailable
from contrib.uniswap_v3.agent.graph import FAKE_MODEL, PROMPT_VERSION, FakeJudge
from contrib.uniswap_v3.agent.verdicts import ask_verdicts, trade_date_of
from contrib.uniswap_v3.config import ConfigError
from contrib.uniswap_v3.domain.verdicts import Rating, text_digest
from contrib.uniswap_v3.store.bar_source import load_bar
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.engine import DAY, FIRST_DAY, config as _plain_config
from contrib.uniswap_v3.tests.fakes.node import DEFAULT_TICK, put_day
from contrib.uniswap_v3.tests.fakes.verdicts import (
    SOURCE,
    ScriptedJudge,
    config as _config,
    record as _record,
)

_CONFIG = _config()
_DAY2 = FIRST_DAY + 2 * DAY
_NOW = _DAY2 + 600


@pytest.fixture
def store(tmp_path):
    """A store with the bars of three days, the WETH price falling a little each day."""
    with open_store(tmp_path / "store.db") as opened:
        for day in range(3):
            put_day(opened, day, eth_tick=DEFAULT_TICK + day * 100)
        yield opened


def _ask(store, judge, tmp_path, *, time=_DAY2, config=_CONFIG, reported=None):
    return ask_verdicts(
        store,
        config,
        judge,
        time=time,
        home=tmp_path,
        now=_NOW,
        report=lambda asked: None if reported is None else reported.append(asked.symbol),
    )


def test_every_traded_token_is_asked_in_symbol_order_and_recorded_with_its_words(store, tmp_path):
    judge = ScriptedJudge({"ETH-USD": Rating.BUY, "BTC-USD": Rating.SELL})
    reported: list[str] = []

    summary = _ask(store, judge, tmp_path, reported=reported)

    assert (summary.time, summary.source, summary.suspect) == (_DAY2, SOURCE, False)
    assert [asked.symbol for asked in summary.verdicts] == ["WBTC", "WETH"] == reported
    assert [asked.ticker for asked in summary.verdicts] == ["BTC-USD", "ETH-USD"]
    assert len(summary.asked) == 2 and summary.already_stored == ()
    assert [(ticker, date) for ticker, date, _ in judge.asked] == [
        ("BTC-USD", "2024-01-03"),
        ("ETH-USD", "2024-01-03"),
    ]
    weth = store.verdict(SOURCE, "WETH", _DAY2)
    assert weth.verdict.rating is Rating.BUY
    assert (weth.model, weth.prompt_version, weth.asked_at) == (
        "scripted-model",
        PROMPT_VERSION,
        _NOW,
    )
    assert weth.sidecar_path == "verdicts/test-judge/WETH-20240103T000000Z.json"
    sidecar = json.loads((tmp_path / weth.sidecar_path).read_text(encoding="utf-8"))
    assert sidecar["rating"] == "Buy" and sidecar["ticker"] == "ETH-USD"
    assert sidecar["decision_digest"] == weth.verdict.digest == text_digest(sidecar["decision"])
    assert sidecar["reports"]["market_report"] == "a market report on ETH-USD"
    assert sidecar["spot_context"] == judge.asked[1][2]
    assert store.verdict(SOURCE, "WBTC", _DAY2).verdict.rating is Rating.SELL
    assert summary.verdicts[1].elapsed_seconds == 1.5


def test_the_context_holds_the_stored_closes_up_to_the_bar(store, tmp_path):
    judge = ScriptedJudge()
    _ask(store, judge, tmp_path)
    _, _, context = judge.asked[1]
    assert "ETH-USD is traded as WETH against USDC" in context
    assert "the bar being judged closed at 2024-01-03T00:00:00Z" in context
    assert "- Close: 1960.44 USDC." in context
    assert "over 1 bar(s): -0.99%" in context
    assert "over 7 bar(s): not measured (no bar 7 bar(s) back)" in context
    assert "% annualised (2 of 20 returns measured)" in context
    assert "rebalance between USDC, WBTC and WETH" in context


def test_a_verdict_the_store_holds_is_not_asked_for_again(store, tmp_path):
    held = _record("WETH", 2, Rating.HOLD)
    store.insert_verdict(held)
    judge = ScriptedJudge({"BTC-USD": Rating.BUY})

    summary = _ask(store, judge, tmp_path)

    assert [ticker for ticker, _, _ in judge.asked] == ["BTC-USD"]
    assert [asked.symbol for asked in summary.already_stored] == ["WETH"]
    assert summary.already_stored[0].record == held
    assert [asked.symbol for asked in summary.asked] == ["WBTC"]
    # Asked again, nothing is asked at all.
    again = ScriptedJudge()
    assert _ask(store, again, tmp_path).asked == () and again.asked == []


def test_a_judge_that_fails_on_a_later_token_keeps_the_earlier_verdict(store, tmp_path):
    judge = ScriptedJudge({"BTC-USD": Rating.BUY, "ETH-USD": JudgeUnavailable("gateway down")})
    with pytest.raises(
        JudgeUnavailable, match=r"gateway down; 1 verdict\(s\) at 2024-01-03 stay recorded"
    ):
        _ask(store, judge, tmp_path)
    assert store.verdict(SOURCE, "WBTC", _DAY2).verdict.rating is Rating.BUY
    assert store.verdict(SOURCE, "WETH", _DAY2) is None
    # The next visit asks about what is missing alone.
    later = ScriptedJudge({"ETH-USD": Rating.HOLD})
    summary = _ask(store, later, tmp_path)
    assert [ticker for ticker, _, _ in later.asked] == ["ETH-USD"]
    assert [asked.symbol for asked in summary.asked] == ["WETH"]


def test_a_judge_that_fails_on_the_first_token_records_nothing(store, tmp_path):
    judge = ScriptedJudge({"BTC-USD": JudgeUnavailable("gateway down")})
    with pytest.raises(JudgeUnavailable, match=r"0 verdict\(s\) at 2024-01-03 stay recorded"):
        _ask(store, judge, tmp_path)
    assert store.verdicts_at(SOURCE, _DAY2) == []
    assert not (tmp_path / "verdicts").exists()


def test_a_failure_of_another_kind_is_not_dressed_up(store, tmp_path):
    with pytest.raises(RuntimeError, match="boom"):
        _ask(store, ScriptedJudge({"BTC-USD": RuntimeError("boom")}), tmp_path)


def test_review_is_recorded_as_a_verdict_with_its_words(store, tmp_path):
    judge = ScriptedJudge({"ETH-USD": Rating.REVIEW, "BTC-USD": Rating.HOLD})
    summary = _ask(store, judge, tmp_path)
    weth = store.verdict(SOURCE, "WETH", _DAY2)
    assert weth.verdict.rating is Rating.REVIEW and weth.sidecar_path is not None
    assert [asked.record.verdict.rating for asked in summary.asked] == [Rating.HOLD, Rating.REVIEW]


def test_the_bar_must_be_in_the_store_before_the_judge_is_asked(store, tmp_path):
    judge = ScriptedJudge()
    with pytest.raises(BarNotStored, match=r"no bar at 2024-01-04 \(2024-01-04T00:00:00Z\)"):
        _ask(store, judge, tmp_path, time=FIRST_DAY + 3 * DAY)
    assert judge.asked == []


def test_a_suspect_bar_is_not_judged(store, tmp_path, monkeypatch):
    real = load_bar

    def suspect(store, config, time):
        stored = real(store, config, time)
        if stored is not None and time == _DAY2:
            return replace(stored, bar=replace(stored.bar, suspect=True))
        return stored

    monkeypatch.setattr(_verdicts, "load_bar", suspect)
    judge = ScriptedJudge()
    summary = _ask(store, judge, tmp_path)
    assert summary.suspect and summary.verdicts == () and judge.asked == []
    assert store.verdicts_at(SOURCE, _DAY2) == []


def test_suspect_bars_are_left_out_of_the_closes(store, tmp_path, monkeypatch):
    real = load_bar

    def suspect(store, config, time):
        stored = real(store, config, time)
        if stored is not None and time == FIRST_DAY + DAY:
            return replace(stored, bar=replace(stored.bar, suspect=True))
        return stored

    monkeypatch.setattr(_verdicts, "load_bar", suspect)
    judge = ScriptedJudge()
    _ask(store, judge, tmp_path)
    # The bar before the latest is the suspect one: no change over one bar, and the two
    # returns that touch it are left out.
    assert "over 1 bar(s): not measured (no bar 1 bar(s) back)" in judge.asked[1][2]
    assert "not measured (0 of 20 returns measured)" in judge.asked[1][2]


def test_a_config_that_reads_no_verdicts_is_refused(store, tmp_path):
    judge = ScriptedJudge()
    with pytest.raises(ConfigError, match="the config has no verdicts section"):
        _ask(store, judge, tmp_path, config=_plain_config())
    assert judge.asked == []


def test_a_traded_token_without_a_ticker_is_refused_before_any_is_asked(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(_tickers, "TICKERS", {"WETH": "ETH-USD"})
    judge = ScriptedJudge()
    with pytest.raises(ConfigError, match=r"no ticker is known for \['WBTC'\]"):
        _ask(store, judge, tmp_path)
    assert judge.asked == []


def test_a_judge_that_is_not_point_in_time_is_asked_about_the_latest_bar_alone(store, tmp_path):
    judge = ScriptedJudge()
    with pytest.raises(
        ConfigError, match=r"the bar at 2024-01-02 \(2024-01-02T00:00:00Z\) is not the latest"
    ):
        _ask(store, judge, tmp_path, time=FIRST_DAY + DAY)
    assert judge.asked == []
    # The fake judge, which is point in time, may judge a past bar.
    assert len(_ask(store, FakeJudge(Rating.BUY), tmp_path, time=FIRST_DAY + DAY).asked) == 2


def test_a_fake_judge_keeps_no_words_and_is_refused_once_real_verdicts_exist(store, tmp_path):
    summary = _ask(store, FakeJudge(Rating.OVERWEIGHT), tmp_path, time=FIRST_DAY + DAY)
    for asked in summary.asked:
        assert asked.record.model == FAKE_MODEL
        assert asked.record.verdict.rating is Rating.OVERWEIGHT
        assert asked.record.sidecar_path is None
    assert not (tmp_path / "verdicts").exists()
    # Fake rows alone do not stand in the way of another rehearsal.
    assert len(_ask(store, FakeJudge(Rating.BUY), tmp_path).asked) == 2
    # A real verdict of the source, at any bar, does.
    store.insert_verdict(_record("WETH", 0, Rating.HOLD))
    judge = FakeJudge(Rating.BUY)
    with pytest.raises(ConfigError, match=r"given by \['synthetic'\].*rehearse on a store"):
        _ask(store, judge, tmp_path, time=FIRST_DAY)
    assert judge.asked == [] and store.verdict(SOURCE, "WBTC", FIRST_DAY) is None
    assert store.verdict_models(SOURCE) == ["fake", "synthetic"]


def test_a_verdict_another_visit_recorded_meanwhile_is_kept_and_this_ones_dropped(
    store, tmp_path
):
    class Meanwhile(ScriptedJudge):
        def ask(self, ticker, trade_date, context):
            # Another visit lands the same token's verdict while this judge thinks.
            store.insert_verdict(_record("WBTC" if ticker == "BTC-USD" else "WETH", 2))
            return super().ask(ticker, trade_date, context)

    summary = _ask(store, Meanwhile(), tmp_path)
    assert summary.asked == () and [a.symbol for a in summary.already_stored] == ["WBTC", "WETH"]
    assert all(a.record.model == "synthetic" and a.superseded for a in summary.verdicts)
    assert summary.verdicts[0].elapsed_seconds == 1.5
    assert not (tmp_path / "verdicts").exists()


def test_the_record_types_hold_their_invariants():
    from contrib.uniswap_v3.agent.verdicts import Asked, AskSummary

    held = _record("WETH", 2)
    with pytest.raises(ValueError, match="the record is of WETH, and the token is WBTC"):
        Asked(symbol="WBTC", ticker="BTC-USD", record=held, asked_now=False)
    with pytest.raises(ValueError, match="took this visit no time"):
        Asked(symbol="WETH", ticker="ETH-USD", record=held, asked_now=False, elapsed_seconds=1)
    with pytest.raises(ValueError, match="a superseded answer is not the verdict"):
        Asked(symbol="WETH", ticker="ETH-USD", record=held, asked_now=True, superseded=True)
    found = Asked(symbol="WETH", ticker="ETH-USD", record=held, asked_now=False)
    with pytest.raises(ValueError, match="a suspect bar has no verdicts"):
        AskSummary(time=_DAY2, source=SOURCE, verdicts=(found,), suspect=True)


def test_the_trade_date_is_the_boundarys_utc_date():
    assert trade_date_of(_DAY2) == "2024-01-03"
    assert trade_date_of(_DAY2 + 86_399) == "2024-01-03"
