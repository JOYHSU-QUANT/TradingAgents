"""The histories: what is harvested, how a series is carried on, the file, and what never enters it."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest

from contrib.dashboard.config import HistorySettings, Settings
from contrib.dashboard.exporter import STATE_FILE, history_lines, run_once, summary_line
from contrib.dashboard.history import (
    HISTORY_SCHEMA_VERSION,
    HOST_KIND,
    HarvestError,
    History,
    HistoryError,
    Series,
    advance,
    dump,
    harvest,
    history_file,
    load_history,
    parse_point,
    read_history,
)
from contrib.dashboard.readers import CommandResult
from contrib.dashboard.times import iso, parse_when

from .support import (
    HL_KEY,
    NOW,
    FakeRunner,
    build_config,
    carry_day,
    carry_document,
    hl_instant,
    host_reader_for,
    hyperliquid_document,
    quiet_documents,
    read_lines_and_host,
    uniswap_document,
)

OFF = Settings(mail=None, healthchecks_url=None)
# The quiet perp document's one snapshot, an hour before NOW, as a history spells it.
AT = "2026-10-14T04:00:00+00:00"
TICK = "2026-10-14T05:00:00+00:00"


def _snapshot(moment, equity: str) -> dict:
    return {
        "timestamp": hl_instant(moment),
        "account_equity": equity,
        "available_balance": "7000",
        "unrealized_pnl": "0",
        "total_pnl": "0",
        "total_position_notional": "0",
        "effective_leverage": "0",
        "margin_ratio": "0",
    }


def _with_snapshots(*snapshots: dict) -> dict:
    """A perp document whose tail is ``snapshots``, given oldest first (the document keeps newest first)."""
    document = hyperliquid_document()
    newest_first = list(reversed(snapshots))
    document["account"]["snapshot"] = newest_first[0]
    document["account"]["recent_snapshots"] = newest_first
    return document


def _points(n: int, *, start=NOW, step=timedelta(hours=1)) -> list[tuple[str, str]]:
    """``n`` points an hour apart from ``start``, valued 10000, 10001, ..."""
    return [(iso(start + i * step), str(10000 + i)) for i in range(n)]


def _normalised(points):
    return [(iso(parse_when(at)), value) for at, value in points]


def _series(harvested):
    """Bare point lists as the harvest's ``Series`` (the label is the name)."""
    return {
        name: value if isinstance(value, Series) else Series(name, tuple(value))
        for name, value in harvested.items()
    }


def _advance(history, harvested, *, run="r", kind="hyperliquid", max_points=10, **kwargs):
    return advance(
        history,
        line="hl",
        kind=kind,
        run=run,
        harvested=_series(harvested),
        now=NOW,
        max_points=max_points,
        **kwargs,
    )


def _tick(tmp_path, documents, *, now=NOW, config=None):
    config = config or build_config(tmp_path)
    report = run_once(
        config,
        OFF,
        now=now,
        runner=FakeRunner(documents),
        host_reader=host_reader_for(tmp_path),
    )
    return config, report


def _history(config, name: str) -> dict:
    return json.loads((config.status_dir / history_file(name)).read_text(encoding="utf-8"))


def _exporter(config) -> dict:
    return json.loads((config.status_dir / "exporter.json").read_text(encoding="utf-8"))


# --- harvesting ---------------------------------------------------------------------


def test_the_perp_tail_is_harvested_oldest_first_from_its_newest_first_document():
    document = _with_snapshots(
        _snapshot(NOW - timedelta(hours=5), "10000"),
        _snapshot(NOW - timedelta(hours=1), "10234.56"),
    )
    series = harvest("hyperliquid", document)
    assert series["equity"].points == (("2026-10-14T00:00:00+00:00", "10000"), (AT, "10234.56"))
    assert series["equity"].label == "account equity (USDC)"
    assert set(series) == {"equity", "notional"}
    assert series["notional"].points[-1] == (AT, "0")


def test_the_uniswap_valuations_and_the_carry_days_are_harvested_with_their_instants_normalised():
    uni = harvest("uniswap", uniswap_document())
    assert uni == {
        "value": Series("value after the bar (quote)", (("2026-10-14T00:00:00+00:00", "10012.34"),))
    }
    carry = harvest("carry", carry_document())
    assert [at for at, _ in carry["net"].points] == [
        "2026-10-11T00:00:00+00:00",
        "2026-10-12T00:00:00+00:00",
        "2026-10-13T00:00:00+00:00",
    ]
    assert carry["perp_equity"].points[0] == ("2026-10-11T00:00:00+00:00", "10000.00")
    assert set(carry) == {"net", "perp_equity", "spot_equity", "net_cumulative"}


def test_the_carry_harvest_adds_the_running_total_of_the_net():
    days = [carry_day(NOW - timedelta(days=n)) for n in (3, 2, 1)]
    days[0] = dict(days[0], net="1.50")
    days[2] = dict(days[2], net="-0.25")
    carry = harvest("carry", carry_document(days))
    assert carry["net_cumulative"].label == "net, cumulative (USDC)"
    assert [value for _, value in carry["net_cumulative"].points] == ["1.50", "1.50", "1.25"]
    assert harvest("carry", {"days": []})["net_cumulative"].points == ()


def test_the_host_is_harvested_as_one_sample_at_the_ticks_instant(tmp_path):
    _, _, host = read_lines_and_host(tmp_path, quiet_documents())
    series = harvest(HOST_KIND, asdict(host))
    assert set(series) == {"disk_used_pct", "memory_available_mb", "load_1m"}
    assert series["memory_available_mb"].points == ((TICK, "1269"),)
    assert len(series["disk_used_pct"].points) == 1


def test_a_document_without_its_tail_harvests_an_empty_series_and_a_kind_unknown_nothing():
    assert harvest("uniswap", {"run": None})["value"].points == ()
    empty = harvest("hyperliquid", {"account": {"recent_snapshots": []}})
    assert set(empty) == {"equity", "notional"}
    assert all(series.points == () for series in empty.values())
    with pytest.raises(HarvestError, match="kind 'other' is not one this build harvests"):
        harvest("other", {})


@pytest.mark.parametrize(
    "document",
    [
        {"account": {"recent_snapshots": "no"}},
        {"account": {"recent_snapshots": [{"timestamp": None, "account_equity": "1"}]}},
        {"account": {"recent_snapshots": [{"timestamp": AT, "account_equity": "NaN"}]}},
        {"account": {"recent_snapshots": [{"timestamp": AT, "account_equity": "ten"}]}},
        {"account": {"recent_snapshots": [{"timestamp": "yesterday", "account_equity": "1"}]}},
        {"account": {"recent_snapshots": [{"timestamp": AT, "account_equity": True}]}},
        {"account": {"recent_snapshots": [{"timestamp": AT, "account_equity": "1E+400"}]}},
    ],
)
def test_a_tail_not_shaped_as_this_build_harvests_it_raises(document):
    with pytest.raises(HarvestError, match="^(TypeError|ValueError)$"):
        harvest("hyperliquid", document)


# --- advancing ------------------------------------------------------------------------


def test_a_tail_seen_twice_adds_nothing_and_an_overlapping_one_fills_the_gap():
    first = _advance(None, {"equity": _points(3)})
    assert [p[1] for p in first.series["equity"].points] == ["10000", "10001", "10002"]
    again = _advance(first, {"equity": _points(3)})
    assert again.series["equity"].points == first.series["equity"].points
    later = _advance(again, {"equity": _points(5)[1:]})
    assert [p[1] for p in later.series["equity"].points] == [
        "10000",
        "10001",
        "10002",
        "10003",
        "10004",
    ]
    assert later.series["equity"].label == "equity"  # the helper labels by name
    real = _advance(None, harvest("hyperliquid", hyperliquid_document()))
    assert real.series["equity"].label == "account equity (USDC)"


def test_the_oldest_points_fall_off_past_max_points():
    history = _advance(None, {"equity": _points(6)}, max_points=4)
    assert [p[1] for p in history.series["equity"].points] == ["10002", "10003", "10004", "10005"]


def test_a_new_run_starts_the_history_afresh_and_a_series_not_harvested_is_kept():
    history = _advance(None, {"equity": _points(2), "notional": _points(2)}, run="paper-BTC-8")
    kept = _advance(history, {"equity": []}, run="paper-BTC-8")
    assert len(kept.series["notional"].points) == 2
    fresh = _advance(history, {"equity": _points(1)}, run="paper-BTC-9")
    assert fresh.run == "paper-BTC-9"
    assert set(fresh.series) == {"equity"} and len(fresh.series["equity"].points) == 1


def test_a_carry_series_is_replaced_by_the_document_so_a_revised_day_shows_revised():
    days = [carry_day(NOW - timedelta(days=n)) for n in (3, 2, 1)]
    first = _advance(None, harvest("carry", carry_document(days)), kind="carry")
    days[-1] = dict(days[-1], net="-1.50")
    revised = _advance(first, harvest("carry", carry_document(days)), kind="carry")
    assert [p[1] for p in revised.series["net"].points] == ["0.00", "0.00", "-1.50"]


def test_the_host_is_thinned_to_one_point_per_interval():
    history = None
    for minute in (0, 2, 4, 10, 12):
        moment = NOW + timedelta(minutes=minute)
        history = advance(
            history,
            line=HOST_KIND,
            kind=HOST_KIND,
            run="tokyo",
            harvested={"load_1m": Series("load average, 1 min", ((iso(moment), "0.10"),))},
            now=moment,
            max_points=10,
            min_interval=timedelta(minutes=10),
        )
    assert history is not None
    assert [at for at, _ in history.series["load_1m"].points] == [
        TICK,
        "2026-10-14T05:10:00+00:00",
    ]
    assert history.series["load_1m"].label == "load average, 1 min"


def test_a_series_refuses_two_points_at_one_instant_and_the_append_keeps_one():
    with pytest.raises(ValueError, match="points do not rise"):
        Series("v", ((iso(NOW), "1"), (iso(NOW), "2")))
    once = _advance(None, {"value": [(iso(NOW), "1")]}, kind="uniswap")
    again = _advance(once, {"value": [(iso(NOW), "2")]}, kind="uniswap")
    assert again.series["value"].points == ((TICK, "1"),)


# --- the file ----------------------------------------------------------------------


def test_the_file_round_trips_and_a_point_reads_back_as_instant_and_amount(tmp_path):
    history = History(
        "hl",
        "hyperliquid",
        "paper-BTC-8",
        TICK,
        10,
        {"equity": Series("account equity (USDC)", tuple(_points(2)))},
    )
    document = dump(history)
    assert document["schema_version"] == HISTORY_SCHEMA_VERSION
    assert document["source"] == "dashboard"
    assert read_history(json.loads(json.dumps(document))) == history
    assert parse_point(history.series["equity"].points[1]) == (NOW + timedelta(hours=1), 10001)
    path = tmp_path / "h.json"
    assert load_history(path) is None
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_history(path) == history


_WHOLE = (
    '"max_points": 2, "line": "l", "kind": "k", "run": "r", '
    '"generated_at": "2026-10-14T05:00:00+00:00"'
)


@pytest.mark.parametrize(
    "text, words",
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        ('{"schema_version": 2}', "schema_version is not 1"),
        ('{"schema_version": 1, "series": [], "max_points": 2}', "series is not a mapping"),
        (
            '{"schema_version": 1, "series": {}, "line": "l", "kind": "k", "run": "r", '
            '"generated_at": "2026-10-14T05:00:00+00:00", "max_points": "2"}',
            "max_points is not an integer",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [["2026-13-45T25:61:61+00:00", "1"]]}}, '
            + _WHOLE
            + "}",
            "not a calendar instant",
        ),
        ('{"schema_version": 1, "series": {}, "max_points": 2, "line": ""}', "line is not text"),
        (
            '{"schema_version": 1, "series": {}, "max_points": 2, "line": "l", "kind": "k", '
            '"run": "r", "generated_at": "2026-10-14T05:00:00Z"}',
            "generated_at is not an instant",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [["x"]]}}, '
            + _WHOLE
            + "}",
            "a point is",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [["soon", "1"]]}}, '
            + _WHOLE
            + "}",
            "not an instant as this build writes one",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [["2026-10-14T05:00:00+00:00", "x"]]}}, '
            + _WHOLE
            + "}",
            "not decimal text",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [["2026-10-14T05:00:00+00:00", "1'
            + "0" * 400
            + '"]]}}, '
            + _WHOLE
            + "}",
            "past float's range",
        ),
        (
            '{"schema_version": 1, "series": {"v": {"label": "v", "points": [[1, "2"]]}}, '
            + _WHOLE
            + "}",
            "not an instant as this build writes one",
        ),
    ],
)
def test_a_file_this_build_cannot_read_is_named_not_guessed(tmp_path, text, words):
    path = tmp_path / "h.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HistoryError, match=words):
        load_history(path)


def test_the_model_holds_its_invariants():
    with pytest.raises(ValueError, match="at least 2"):
        History("l", "k", "r", AT, 1, {})
    with pytest.raises(ValueError, match="line is not text"):
        History("", "k", "r", AT, 2, {})
    with pytest.raises(ValueError, match="generated_at is not an instant"):
        History("l", "k", "r", "2026-10-14T05:00:00Z", 2, {})
    with pytest.raises(ValueError, match="a series label is not text"):
        Series("", ())
    with pytest.raises(ValueError, match="not a calendar instant"):
        Series("v", (("2026-13-45T25:61:61+00:00", "1"),))
    with pytest.raises(ValueError, match="past float's range"):
        Series("v", ((TICK, "1" + "0" * 400),))
    with pytest.raises(ValueError, match="more than max_points"):
        History("l", "k", "r", AT, 2, {"v": Series("v", tuple(_points(3)))})


# --- one tick, then another ---------------------------------------------------------


def test_a_tick_writes_one_history_per_line_and_one_for_the_host(tmp_path):
    config, report = _tick(tmp_path, quiet_documents())
    directory = config.status_dir / "history"
    assert sorted(p.name for p in directory.iterdir()) == [
        "carry-perp.json",
        "carry.json",
        "hl-paper.json",
        "host.json",
        "uniswap-ai.json",
        "uniswap-carry.json",
        "uniswap-trend.json",
    ]
    assert report.history_errors == () and history_lines(report) == []
    assert all(history_file(name) in report.written for name in ("hl-paper", "host", "carry"))
    hl = _history(config, "hl-paper")
    assert hl["line"] == "hl-paper" and hl["kind"] == "hyperliquid" and hl["run"] == "paper-BTC-8"
    assert hl["series"]["equity"]["points"] == [[AT, "10234.56"]]
    host = _history(config, "host")
    assert host["run"] == "tokyo"
    assert host["series"]["memory_available_mb"]["points"] == [[TICK, "1269"]]
    exporter = _exporter(config)
    assert exporter["lines"][0]["history_error"] is None
    assert exporter["host_history_error"] is None
    text = (config.status_dir / history_file("hl-paper")).read_text(encoding="utf-8")
    assert text.count("\n") == 1 and text.endswith("}\n")


def test_the_next_tick_carries_the_history_on_and_the_host_is_thinned(tmp_path):
    documents = quiet_documents()
    config, _ = _tick(tmp_path, documents)
    documents[HL_KEY] = _with_snapshots(
        _snapshot(NOW - timedelta(hours=1), "10234.56"),
        _snapshot(NOW + timedelta(hours=3), "10300"),
    )
    _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=config)
    hl = _history(config, "hl-paper")
    assert hl["series"]["equity"]["points"] == [
        [AT, "10234.56"],
        ["2026-10-14T08:00:00+00:00", "10300"],
    ]
    assert hl["generated_at"] == "2026-10-14T05:02:00+00:00"
    # The host's memory is sampled every tick (its load average only where the OS gives one),
    # thinned to the 30-minute sample: the second tick changed nothing and wrote nothing.
    host = _history(config, "host")
    assert len(host["series"]["memory_available_mb"]["points"]) == 1
    assert host["generated_at"] == TICK
    _, report = _tick(tmp_path, documents, now=NOW + timedelta(minutes=32), config=config)
    assert history_file("host") in report.written
    assert len(_history(config, "host")["series"]["memory_available_mb"]["points"]) == 2


def test_a_tick_that_adds_no_point_rewrites_nothing(tmp_path):
    documents = quiet_documents()
    config, first = _tick(tmp_path, documents)
    assert sum(1 for name in first.written if name.startswith("history/")) == 7
    _, again = _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=config)
    assert not [name for name in again.written if name.startswith("history/")]
    assert again.history_errors == ()
    assert _history(config, "carry")["generated_at"] == TICK


def test_a_line_that_was_not_read_keeps_its_history_as_it_was(tmp_path):
    documents = quiet_documents()
    config, _ = _tick(tmp_path, documents)
    path = config.status_dir / history_file("hl-paper")
    before = path.read_text(encoding="utf-8")
    documents[HL_KEY] = CommandResult(5, "", "store damaged")
    _, report = _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=config)
    assert path.read_text(encoding="utf-8") == before
    assert history_file("hl-paper") not in report.written and report.history_errors == ()
    assert _exporter(config)["lines"][0]["history_error"] is None


def test_a_history_this_build_cannot_read_is_left_as_it_is_and_named(tmp_path):
    config, _ = _tick(tmp_path, quiet_documents())
    path = config.status_dir / history_file("carry")
    path.write_text("{broken", encoding="utf-8")
    _, report = _tick(tmp_path, quiet_documents(), now=NOW + timedelta(minutes=2), config=config)
    assert path.read_text(encoding="utf-8") == "{broken"
    why = "carry.json is not a history this build reads (not JSON); move it aside"
    assert report.history_errors == (("carry", why),)
    assert history_lines(report) == [f"carry: {why}"]
    assert summary_line(report).endswith("; ping off; 1 history file(s) not kept")
    carry = next(line for line in _exporter(config)["lines"] if line["name"] == "carry")
    assert carry["history_error"] == why
    assert (config.status_dir / STATE_FILE).exists()


def test_a_document_the_harvest_cannot_read_leaves_the_history_and_names_the_class(tmp_path):
    documents = quiet_documents()
    config, _ = _tick(tmp_path, documents)
    broken = hyperliquid_document()
    broken["account"]["recent_snapshots"] = "no"
    documents[HL_KEY] = broken
    _, report = _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=config)
    assert report.history_errors == (
        ("hl-paper", "the document is not shaped as this build harvests it (TypeError)"),
    )
    assert _history(config, "hl-paper")["generated_at"] == TICK


def test_a_segment_switch_starts_the_lines_history_afresh(tmp_path):
    documents = quiet_documents()
    config, _ = _tick(tmp_path, documents)
    switched = replace(
        config, lines=(replace(config.lines[0], run_id="paper-BTC-9"), *config.lines[1:])
    )
    documents["contrib.hyperliquid_perp:paper-BTC-9"] = hyperliquid_document(run_id="paper-BTC-9")
    _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=switched)
    hl = _history(config, "hl-paper")
    assert hl["run"] == "paper-BTC-9" and len(hl["series"]["equity"]["points"]) == 1


def test_the_settings_bound_the_history(tmp_path):
    config = replace(
        build_config(tmp_path), history=HistorySettings(max_points=2, host_sample_minutes=1)
    )
    documents = quiet_documents()
    documents[HL_KEY] = _with_snapshots(
        *(_snapshot(NOW - timedelta(hours=h), str(h)) for h in (4, 3, 2, 1))
    )
    _tick(tmp_path, documents, config=config)
    hl = _history(config, "hl-paper")
    assert [p[1] for p in hl["series"]["equity"]["points"]] == ["2", "1"]
    assert hl["max_points"] == 2


# --- what never enters a history ----------------------------------------------------


def test_a_history_holds_instants_and_numbers_and_nothing_else(tmp_path):
    config, _ = _tick(tmp_path, quiet_documents())
    instant = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
    number = re.compile(r"^-?\d+(\.\d+)?$")
    for path in (config.status_dir / "history").iterdir():
        text = path.read_text(encoding="utf-8")
        assert text.isascii() and "0x" not in text, path
        document = json.loads(text)
        assert set(document) == {
            "schema_version",
            "source",
            "generated_at",
            "line",
            "kind",
            "run",
            "max_points",
            "series",
        }
        for series in document["series"].values():
            assert set(series) == {"label", "points"}
            for at, value in series["points"]:
                assert instant.match(at) and number.match(value), (path, at, value)


def test_the_harvest_keys_are_the_packages_field_names():
    """A renamed field would harvest an empty series; the fixtures carry the packages' names (test_schema_pin)."""
    for kind, document in (
        ("hyperliquid", hyperliquid_document()),
        ("uniswap", uniswap_document()),
        ("carry", carry_document()),
    ):
        assert all(series.points for series in harvest(kind, document).values()), kind


# --- the first review round -------------------------------------------------------------


def test_a_value_in_exponent_form_is_stored_as_plain_digits():
    series = harvest(
        "uniswap", {"run": {"recent_valuations": [{"time": AT, "total_value": "1E+4"}]}}
    )
    assert series["value"].points == ((AT, "10000"),)
    tiny = harvest("uniswap", {"run": {"recent_valuations": [{"time": AT, "total_value": 1e-07}]}})
    assert tiny["value"].points == ((AT, "0.0000001"),)


def test_points_that_do_not_rise_are_refused_by_the_reader(tmp_path):
    document = dump(
        History("l", "k", "r", TICK, 10, {"v": Series("v", (("2026-10-14T05:00:00+00:00", "1"),))})
    )
    document["series"]["v"]["points"].append(["2026-10-14T05:00:00+00:00", "2"])
    path = tmp_path / "h.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(HistoryError, match="points do not rise"):
        load_history(path)


def test_a_kept_series_the_harvest_does_not_name_is_cut_to_a_lowered_max_points():
    history = _advance(None, {"equity": _points(6), "notional": _points(6)}, max_points=10)
    lowered = _advance(history, {"equity": []}, max_points=3)
    assert len(lowered.series["notional"].points) == 3
    assert lowered.series["notional"].points[0][1] == "10003"


def test_a_history_directory_that_cannot_be_made_is_reported_and_the_tick_still_finishes(
    tmp_path,
):
    config = build_config(tmp_path)
    config.status_dir.mkdir(parents=True)
    (config.status_dir / "history").write_text("a file where the directory goes", encoding="utf-8")
    _, report = _tick(tmp_path, quiet_documents(), config=config)
    assert len(report.history_errors) == 7
    assert all(why.startswith("history/ could not be made (") for _, why in report.history_errors)
    assert "exporter.json" in report.written and (config.status_dir / "exporter.json").exists()
    assert _exporter(config)["host_history_error"].startswith("history/ could not be made")


def test_a_history_file_that_cannot_be_written_is_reported_not_raised(tmp_path, monkeypatch):
    from contrib.dashboard import exporter as module

    def refuse(path, text):
        if path.name == "hl-paper.json" and path.parent.name == "history":
            raise PermissionError("read-only")
        return real(path, text)

    real = module.write_private
    monkeypatch.setattr(module, "write_private", refuse)
    _, report = _tick(tmp_path, quiet_documents())
    assert report.history_errors == (
        ("hl-paper", "hl-paper.json could not be written (PermissionError)"),
    )
    assert history_file("host") in report.written


def test_history_file_names_are_bare_under_the_directory():
    assert history_file("hl-paper") == "history/hl-paper.json"
    assert Path(history_file("host")).name == "host.json"


# --- the first review-loop round ------------------------------------------------------


def test_a_regex_shaped_instant_that_is_no_date_is_refused_and_the_tick_reports_it(tmp_path):
    documents = quiet_documents()
    config, _ = _tick(tmp_path, documents)
    path = config.status_dir / history_file("hl-paper")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["series"]["equity"]["points"][-1][0] = "2026-13-45T25:61:61+00:00"
    text = json.dumps(document)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HistoryError, match="not a calendar instant"):
        load_history(path)
    _, report = _tick(tmp_path, documents, now=NOW + timedelta(minutes=2), config=config)
    assert [item.name for item in report.history_errors] == ["hl-paper"]
    assert "not a calendar instant" in report.history_errors[0].why
    assert path.read_text(encoding="utf-8") == text
    assert "exporter.json" in report.written


def test_the_host_load_is_harvested_to_two_decimals_and_a_missing_fact_is_an_empty_series():
    with_load = harvest(HOST_KIND, {"generated_at": TICK, "load_average": [0.5, 0.4, 0.3]})
    assert with_load["load_1m"].points == ((TICK, "0.50"),)
    assert with_load["disk_used_pct"].points == () and with_load["memory_available_mb"].points == ()
    bare = harvest(
        HOST_KIND, {"generated_at": TICK, "disk": None, "memory": None, "load_average": None}
    )
    assert all(series.points == () for series in bare.values())
    some = harvest(
        HOST_KIND,
        {"generated_at": TICK, "disk": {"used_pct": 41.26}, "memory": None, "load_average": None},
    )
    assert some["disk_used_pct"].points == ((TICK, "41.3"),)


def test_a_carry_leg_never_read_is_skipped_not_refused():
    days = [carry_day(NOW - timedelta(days=n)) for n in (3, 2, 1)]
    days[0] = dict(days[0], perp_equity=None, spot_equity=None)
    carry = harvest("carry", carry_document(days))
    assert len(carry["perp_equity"].points) == 2 and len(carry["net"].points) == 3


def test_the_uniswap_valuations_keep_their_oldest_first_order():
    from .support import uni_instant, uniswap_run

    older = {"time": uni_instant(NOW - timedelta(days=2)), "total_value": "10000"}
    newer = {"time": uni_instant(NOW - timedelta(days=1)), "total_value": "10012.34"}
    run = uniswap_run(recent_valuations=[older, newer])
    series = harvest("uniswap", uniswap_document(run))
    assert [value for _, value in series["value"].points] == ["10000", "10012.34"]
    twice = _advance(_advance(None, series, kind="uniswap"), series, kind="uniswap")
    assert [value for _, value in twice.series["value"].points] == ["10000", "10012.34"]


def test_a_history_of_another_kind_under_the_same_run_starts_afresh():
    first = _advance(None, {"value": _points(2)}, kind="uniswap", run="r")
    second = _advance(first, {"equity": _points(1)}, kind="hyperliquid", run="r")
    assert set(second.series) == {"equity"} and second.kind == "hyperliquid"


def test_the_kinds_harvested_are_the_configs_kinds_and_the_host():
    from contrib.dashboard.config import _KINDS
    from contrib.dashboard.history import HARVESTED_KINDS

    assert set(_KINDS) | {HOST_KIND} == HARVESTED_KINDS


# --- the second review-loop round -----------------------------------------------------


def test_two_perp_snapshots_in_one_second_are_the_later_one_and_a_clock_step_is_skipped():
    same_second = _with_snapshots(
        _snapshot(NOW - timedelta(hours=1, microseconds=900000), "10000"),
        _snapshot(NOW - timedelta(hours=1, microseconds=300000), "10001"),
        _snapshot(NOW - timedelta(hours=1, microseconds=100000), "10002"),
        _snapshot(NOW, "10003"),
    )
    series = harvest("hyperliquid", same_second)
    assert [value for _, value in series["equity"].points] == ["10002", "10003"]
    stepped = _with_snapshots(
        _snapshot(NOW - timedelta(hours=2), "1"),
        _snapshot(NOW, "3"),
        _snapshot(NOW - timedelta(hours=1), "2"),
    )
    assert [value for _, value in harvest("hyperliquid", stepped)["equity"].points] == ["1", "3"]


def test_a_history_the_model_refuses_is_reported_with_its_reason(tmp_path, monkeypatch):
    from contrib.dashboard import exporter as module

    def refuse(*args, **kwargs):
        raise ValueError("points do not rise")

    monkeypatch.setattr(module, "advance_history", refuse)
    _, report = _tick(tmp_path, quiet_documents())
    assert (
        report.history_errors[0].why == "the history could not be carried on (points do not rise)"
    )
