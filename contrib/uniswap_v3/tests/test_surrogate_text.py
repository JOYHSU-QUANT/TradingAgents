"""Words that cannot be written as UTF-8 (issue #429): recorded, where they used to end a visit.

A judge's answer — its decision, a report, the line that says why news was
missing — or the words of whatever did not answer can hold a lone
surrogate. Neither a digest, a UTF-8 file nor SQLite can take one. An
answer holding one ended the ``verdict`` visit in a traceback: the judge
had been paid, the token was not recorded, and the next visit asked again.
A decline in such words ended in a traceback where it should have been
"try again later".

These tests hand the visit such an answer through a scripted judge. With
the real graph only the final decision gets this far: any other report is
read by a later node first, whose request cannot be built with such a
character in it (issue #441).

Every such character here is built with ``chr``: a source file cannot hold
one, and an escape for one is easy to get wrong without noticing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.agent import graph as _graph
from contrib.uniswap_v3.agent.errors import JudgeUnavailable
from contrib.uniswap_v3.agent.graph import Answer
from contrib.uniswap_v3.agent.record import (
    sidecar_record,
    storable_answer,
    verdict_record,
    write_sidecar,
)
from contrib.uniswap_v3.domain.text import REPLACEMENT_CHARACTER, replace_lone_surrogates
from contrib.uniswap_v3.domain.verdicts import JudgeDecline, Rating, text_digest
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.node import DAY, DEFAULT_TICK, FIRST_DAY, put_day
from contrib.uniswap_v3.tests.fakes.verdicts import (
    ScriptedJudge,
    judged_config,
    record as _stored,
)

HIGH = chr(0xD83D)  # the first half of a UTF-16 pair, on its own
LOW = chr(0xDE00)  # the second half, on its own
FFFD = chr(0xFFFD)
_SOURCE = "judge-v1"
_DAY2 = FIRST_DAY + 2 * DAY
_NOW = _DAY2 + 600
_LOGGER = "contrib.uniswap_v3.agent.verdicts"


def test_the_characters_these_tests_are_about_are_what_they_are_meant_to_be():
    # No product code: a test about a lone surrogate that held something
    # else would pass for the wrong reason.
    for lone in (HIGH, LOW):
        assert len(lone) == 1 and 0xD800 <= ord(lone) <= 0xDFFF
        with pytest.raises(UnicodeEncodeError):
            lone.encode("utf-8")
    assert FFFD.encode("utf-8") == b"\xef\xbf\xbd"


# -- the helper -----------------------------------------------------------------------------------


def test_each_lone_surrogate_becomes_the_replacement_character_and_is_counted():
    assert REPLACEMENT_CHARACTER == FFFD
    assert replace_lone_surrogates(f"a{HIGH}b{LOW}{LOW}") == (f"a{FFFD}b{FFFD}{FFFD}", 3)
    assert replace_lone_surrogates(HIGH) == (FFFD, 1)
    # What comes back can be written.
    replace_lone_surrogates(f"x{HIGH}")[0].encode("utf-8")


def test_every_surrogate_code_point_is_replaced_and_its_neighbours_are_not():
    # The shared floor's helper has a test of its two ends; this copy cannot
    # be set beside it here (the package imports no other), so the whole
    # range is walked.
    for code in range(0xD800, 0xE000):
        assert replace_lone_surrogates(chr(code)) == (FFFD, 1), hex(code)
    for code in (0xD7FF, 0xE000):
        assert replace_lone_surrogates(chr(code)) == (chr(code), 0)
    # Two halves that happen to be adjacent are two characters, and become two.
    assert replace_lone_surrogates(HIGH + LOW) == (FFFD + FFFD, 2)


def test_text_without_one_comes_back_as_the_same_object():
    # A well-formed pair is one code point above U+FFFF, never two
    # surrogates; the code points either side of the range are not
    # surrogates; and U+FFFD a judge wrote itself is left as it is.
    clean = f"plain, {chr(0x1F600)}, the edges {chr(0xD7FF)}{chr(0xE000)}, and {FFFD}"
    replaced, count = replace_lone_surrogates(clean)
    assert replaced is clean and count == 0
    assert replace_lone_surrogates("")[0] == ""


# -- an answer made storable ------------------------------------------------------------------------


def _answer(**changes) -> Answer:
    fields = {
        "decision": "**Rating**: Buy\n\nBecause.",
        "rating": Rating.BUY,
        "reports": {"market_report": "a report", "selected_analysts": ["market"]},
        "elapsed_seconds": 1.5,
    }
    return Answer(**{**fields, **changes})


def test_an_answer_with_no_such_character_is_handed_back_as_it_is():
    for answer in (_answer(), _answer(reports=None)):
        made, count = storable_answer(answer)
        assert made is answer and count == 0


def test_every_word_of_an_answer_is_made_storable_and_counted():
    answer = _answer(
        decision=f"**Rating**: Buy\n\nMomentum {HIGH} holds.",
        reports={
            "market_report": f"up {HIGH}",
            "investment_debate_state": {"history": f"bull {HIGH}", f"key {LOW}": 2, "count": 2},
            "selected_analysts": ["market", f"news {HIGH}"],
            "news_report": None,
        },
        news_missing=True,
        news_missing_reason=f"the router said {LOW}",
    )
    made, count = storable_answer(answer)
    # The decision, three report strings, one key, the reason.
    assert count == 6
    assert made.decision == f"**Rating**: Buy\n\nMomentum {FFFD} holds."
    assert made.reports == {
        "market_report": f"up {FFFD}",
        "investment_debate_state": {"history": f"bull {FFFD}", f"key {FFFD}": 2, "count": 2},
        "selected_analysts": ["market", f"news {FFFD}"],
        "news_report": None,
    }
    assert made.news_missing_reason == f"the router said {FFFD}"
    assert (made.rating, made.elapsed_seconds, made.news_missing) == (Rating.BUY, 1.5, True)
    # The answer it was handed is not changed.
    assert HIGH in answer.decision and HIGH in answer.reports["market_report"]


def test_reports_are_walked_through_lists_and_tuples_however_they_nest():
    answer = _answer(
        reports={"turns": [{"said": f"a {HIGH}"}, (f"b {LOW}", [f"c {HIGH}"])], "count": 3}
    )
    made, count = storable_answer(answer)
    assert count == 3
    assert made.reports == {
        "turns": [{"said": f"a {FFFD}"}, [f"b {FFFD}", [f"c {FFFD}"]]],
        "count": 3,
    }


def test_the_sidecar_the_row_and_a_reader_s_digest_are_of_the_same_words(tmp_path):
    decision = f"**Rating**: Buy\n\nMomentum {HIGH} holds."
    raw = _answer(decision=decision, reports={"final_trade_decision": decision})
    # As it came, neither digest could be taken: the sidecar's (a judge
    # that keeps words) nor the row's own (one that keeps none).
    arguments = {"source": _SOURCE, "symbol": "WETH", "time": _DAY2, "model": "m"}
    with pytest.raises(UnicodeEncodeError):
        text_digest(raw.decision)
    with pytest.raises(UnicodeEncodeError):
        verdict_record(raw, asked_at=_NOW, **arguments)
    answer, count = storable_answer(raw)
    assert count == 2
    record = sidecar_record(
        answer,
        ticker="ETH-USD",
        trade_date="2024-01-03",
        context="the spot context",
        settings={"llm_provider": "vendor"},
        asked_at=_NOW,
        **arguments,
    )
    digest = write_sidecar(tmp_path, "verdicts/j/WETH.json", record)
    data = (tmp_path / "verdicts" / "j" / "WETH.json").read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    written = json.loads(data)
    row = verdict_record(answer, asked_at=_NOW, **arguments)
    assert written["decision_digest"] == row.verdict.digest == text_digest(written["decision"])
    assert written["decision"] == f"**Rating**: Buy\n\nMomentum {FFFD} holds."
    assert written["reports"]["final_trade_decision"] == written["decision"]


def test_write_sidecar_writes_a_record_that_was_handed_to_it_raw(tmp_path, caplog):
    # One odd value must not lose the record (its docstring): what
    # ``default=str`` writes for one, a key, and a record whose answer was
    # not made storable first, are all written — and said, since nothing
    # upstream of the dump counted them.
    class Odd:
        def __str__(self) -> str:
            return f"odd {HIGH}"

    record = {"decision": f"raw {HIGH}", "odd": Odd(), f"key {LOW}": 1}
    with caplog.at_level("WARNING", logger="contrib.uniswap_v3.agent.record"):
        digest = write_sidecar(tmp_path, "verdicts/j/x.json", record)
    assert [r.getMessage() for r in caplog.records] == [
        "3 lone surrogate(s) in the sidecar at verdicts/j/x.json cannot be written as UTF-8; "
        "each is written as U+FFFD"
    ]
    data = (tmp_path / "verdicts" / "j" / "x.json").read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    assert json.loads(data) == {"decision": f"raw {FFFD}", "odd": f"odd {FFFD}", f"key {FFFD}": 1}


def test_a_sidecar_that_could_be_written_before_is_byte_for_byte_what_it_was(tmp_path, caplog):
    # The other way to get past the encoding (``ensure_ascii=True``) would
    # have rewritten every non-ASCII character of every sidecar from here on.
    record = {"decision": f"a dash {chr(0x2014)} and {chr(0x898F)}{chr(0x5247)}", "n": 1}
    with caplog.at_level("WARNING", logger="contrib.uniswap_v3.agent.record"):
        write_sidecar(tmp_path, "verdicts/j/x.json", record)
    assert caplog.records == []
    data = (tmp_path / "verdicts" / "j" / "x.json").read_bytes()
    assert data == json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert chr(0x2014).encode("utf-8") in data


# -- the visit, end to end ----------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return judged_config(tmp_path, _SOURCE)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "store.db"
    path.parent.mkdir()
    with open_store(path) as store:
        for day in range(3):
            put_day(store, day, eth_tick=DEFAULT_TICK + day * 100)
    return path


def _visit(config: Path, db: Path, *, now: int = _NOW) -> int:
    lines: list[str] = []
    return cli.main(
        ["verdict", "--config", str(config), "--db", str(db)],
        out=lines.append,
        now=lambda: float(now),
    )


def _judging(monkeypatch, judge) -> None:
    monkeypatch.setattr(_graph, "TradingAgentsJudge", lambda settings, home, **kwargs: judge)


class _Says(ScriptedJudge):
    """The scripted judge, its answer on one ticker passed through ``change``."""

    def __init__(self, ticker: str, change) -> None:
        super().__init__()
        self.ticker = ticker
        self.change = change

    def ask(self, ticker, trade_date, context, *, accept_news_less=False):
        answer = super().ask(ticker, trade_date, context, accept_news_less=accept_news_less)
        return self.change(answer) if ticker == self.ticker else answer


def _in_the_decision(answer: Answer) -> Answer:
    # As the real judge builds it: the decision is also the last report.
    decision = f"{answer.decision} Momentum {HIGH} holds."
    return replace(
        answer, decision=decision, reports={**answer.reports, "final_trade_decision": decision}
    )


def _in_a_report(answer: Answer) -> Answer:
    return replace(answer, reports={**answer.reports, "market_report": f"a market report {HIGH}"})


def _in_a_nested_report(answer: Answer) -> Answer:
    state = {"history": f"bull {HIGH}", "count": 2}
    return replace(answer, reports={**answer.reports, "investment_debate_state": state})


def _in_the_news_line(answer: Answer) -> Answer:
    return replace(answer, news_missing=True, news_missing_reason=f"the router said {HIGH}")


def _from_a_judge_that_keeps_no_words(answer: Answer) -> Answer:
    decision = f"{answer.decision} Momentum {HIGH} holds."
    return Answer(decision=decision, rating=answer.rating, reports=None, elapsed_seconds=1.5)


@pytest.mark.parametrize(
    ("ticker", "change", "replaced"),
    [
        ("ETH-USD", _in_the_decision, 2),
        ("BTC-USD", _in_the_decision, 2),
        ("ETH-USD", _in_a_report, 1),
        ("ETH-USD", _in_a_nested_report, 1),
        ("ETH-USD", _in_the_news_line, 1),
        ("ETH-USD", _from_a_judge_that_keeps_no_words, 1),
    ],
    ids=["decision", "decision-of-the-first-token", "report", "nested-report", "news-line", "no-words"],
)
def test_a_visit_records_a_judge_whose_words_hold_a_lone_surrogate(
    config, db, monkeypatch, caplog, ticker, change, replaced
):
    judge = _Says(ticker, change)
    _judging(monkeypatch, judge)
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert _visit(config, db) == cli.EXIT_OK
    symbol = "WBTC" if ticker == "BTC-USD" else "WETH"
    with open_store(db, create=False) as store:
        # Both tokens, the odd one too.
        assert [r.verdict.symbol for r in store.verdicts_at(_SOURCE, _DAY2)] == ["WBTC", "WETH"]
        row = store.verdict(_SOURCE, symbol, _DAY2)
    kept = (db.parent / "verdicts" / _SOURCE).glob(f"{symbol}-*.json")
    if row.sidecar_path is None:
        assert list(kept) == []
    else:
        data = (db.parent / row.sidecar_path).read_bytes()
        assert row.sidecar_digest == hashlib.sha256(data).hexdigest()
        sidecar = json.loads(data)  # decodes as UTF-8: no surrogate was written
        assert sidecar["decision_digest"] == row.verdict.digest == text_digest(sidecar["decision"])
        assert FFFD in data.decode("utf-8")
    # Said once, of that token, with how many.
    (warning,) = (r.getMessage() for r in caplog.records if r.name == _LOGGER)
    assert warning == (
        f"{replaced} lone surrogate(s) in the judge's words on {symbol} at 2024-01-03 (its "
        "decision, and each report that repeats it, counted where they stand) cannot be "
        "written as UTF-8; each is replaced with U+FFFD"
    )
    # The next visit has nothing left to ask, so nothing is paid for twice.
    asked = len(judge.asked)
    assert _visit(config, db, now=_NOW + 60) == cli.EXIT_OK
    assert len(judge.asked) == asked == 2


def test_an_answer_dropped_for_another_visit_s_is_not_said_to_be_replaced(
    config, db, monkeypatch, caplog
):
    # Another visit recorded the token while this one's judge was thinking:
    # nothing of this answer is recorded, so nothing of it is replaced.
    class Overtaken(_Says):
        def ask(self, ticker, trade_date, context, *, accept_news_less=False):
            symbol = "WBTC" if ticker == "BTC-USD" else "WETH"
            with open_store(db, create=False) as store:
                store.insert_verdict(_stored(symbol, 2, source=_SOURCE))
            return super().ask(ticker, trade_date, context, accept_news_less=accept_news_less)

    _judging(monkeypatch, Overtaken("ETH-USD", _in_the_decision))
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert _visit(config, db) == cli.EXIT_OK
    assert [r for r in caplog.records if "surrogate" in r.getMessage()] == []
    with open_store(db, create=False) as store:
        assert store.verdict(_SOURCE, "WETH", _DAY2).model == "synthetic"


def test_a_clean_visit_says_nothing_of_it(config, db, monkeypatch, caplog):
    _judging(monkeypatch, ScriptedJudge())
    with caplog.at_level("WARNING", logger=_LOGGER):
        assert _visit(config, db) == cli.EXIT_OK
    assert [r for r in caplog.records if r.name == _LOGGER] == []


def test_a_judge_that_declines_in_such_words_is_still_try_again_later(
    config, db, monkeypatch, capsys
):
    # The decline is telemetry that "does not change the visit's outcome";
    # it turned "try again later" into a traceback.
    said = f"the judge did not answer on ETH-USD (the news vendor said {HIGH})"
    _judging(monkeypatch, ScriptedJudge({"ETH-USD": JudgeUnavailable(said)}))
    assert _visit(config, db) == cli.EXIT_RETRY
    assert "try again later: the judge did not answer on ETH-USD" in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert [r.verdict.symbol for r in store.verdicts_at(_SOURCE, _DAY2)] == ["WBTC"]
        (decline,) = store.judge_declines_at(_SOURCE, _DAY2)
    assert (decline.symbol, decline.reason) == ("WETH", said.replace(HIGH, FFFD))


def test_the_store_takes_a_decline_whose_reason_holds_one(tmp_path):
    with open_store(tmp_path / "store.db") as store:
        for asked_at, reason in ((_NOW, f"vendor said {HIGH}{LOW}"), (_NOW + 60, "the gateway")):
            store.insert_judge_decline(
                JudgeDecline(
                    source=_SOURCE, symbol="WETH", time=_DAY2, asked_at=asked_at, reason=reason
                )
            )
        kept = [decline.reason for decline in store.judge_declines_at(_SOURCE, _DAY2)]
    # Each replaced one for one; a reason without one is stored as it was.
    assert kept == [f"vendor said {FFFD}{FFFD}", "the gateway"]
