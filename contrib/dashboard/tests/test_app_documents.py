"""The page's view models over a status directory the exporter wrote (no web library needed)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from contrib.dashboard.app.documents import (
    BAD,
    OK,
    Card,
    Page,
    ago,
    load_line,
    load_page,
    read_document,
    sparkline,
)
from contrib.dashboard.readers import CommandResult

from .support import (
    AGENT_KEY,
    CARRY_KEY,
    HL_KEY,
    LINE_NAMES,
    NOW,
    STALE,
    UNI_AI_KEY,
    UNITS_QUIET,
    WALLET,
    carry_day,
    carry_document,
    hyperliquid_document,
    quiet_documents,
    status_dir,
    uniswap_document,
    uniswap_run,
    unit_text,
)


def page_at(tmp_path: Path, documents=None, *, later=timedelta(minutes=1), **kwargs) -> Page:
    return load_page(status_dir(tmp_path, documents, **kwargs), now=NOW + later, stale=STALE)


def _line(page: Page, name: str) -> Card:
    return next(card for card in page.lines if card.name == name)


def _texts(card: Card) -> Iterator[str]:
    yield card.title
    yield card.status
    if card.generated_at:
        yield card.generated_at
    for row in card.rows:
        yield row.label
        yield row.value
    for table in card.tables:
        yield table.title
        for row in table.rows:
            yield from row
    if card.sparkline:
        yield card.sparkline.label
        yield card.sparkline.first
        yield card.sparkline.last


def _page_texts(page: Page) -> Iterator[str]:
    if page.banner:
        yield page.banner
    for card in (page.exporter, page.alerts, *page.lines, page.host):
        yield from _texts(card)


def _rows(card: Card) -> dict[str, str]:
    return {row.label: row.value for row in card.rows}


# --- a quiet tick -----------------------------------------------------------------


def test_a_quiet_tick_gives_one_card_per_indexed_line_in_the_exporters_order(tmp_path):
    page = page_at(tmp_path)
    assert page.banner is None
    assert tuple(card.name for card in page.lines) == LINE_NAMES
    assert [card.kind for card in page.lines] == [
        "hyperliquid",
        "hyperliquid",
        "uniswap",
        "uniswap",
        "uniswap",
        "carry",
    ]
    assert all(card.tone == OK and not card.stale for card in page.lines)
    assert all(card.status == "quiet" for card in page.lines)
    assert all(card.link == f"/line/{card.name}" for card in page.lines)
    assert page.exporter.tone == OK and page.exporter.status == "ticking"
    assert page.alerts.tone == OK and page.alerts.status == "nothing holds"


def test_every_card_shows_when_its_document_was_generated(tmp_path):
    page = page_at(tmp_path)
    for card in (page.exporter, page.alerts, *page.lines, page.host):
        assert card.generated_at == "2026-10-14 05:00Z (1 min ago)", card.name


def test_the_titles_carry_the_run_ids_and_the_rows_the_numbers_an_operator_reads(tmp_path):
    page = page_at(tmp_path)
    hl = _line(page, "hl-paper")
    assert hl.title == "hl-paper · paper-BTC-8"
    rows = _rows(hl)
    assert rows["unit"].startswith("hl-paper.service: active (running)")
    assert rows["equity"].startswith("10234.56 (7234.56 available)")
    assert rows["position BTC"].startswith("0.0447 @ 67000.0")
    assert rows["next decision"] == "2026-10-14 08:00Z (in 2 h 59 min)"
    assert rows["last verdict"] == "set_target long 30%, approved, confidence 0.62"
    uni = _line(page, "uniswap-ai")
    assert uni.title == "uniswap-ai · paper-ai-1"
    rows = _rows(uni)
    assert rows["clock"].startswith("up_to_date: last decided")
    assert rows["value"].startswith("10012.34 at")
    assert rows["strategy"].startswith("return 0.0012, max drawdown 0.0040")
    carry = _line(page, "carry")
    assert carry.title == "carry · carry-ETH-1+paper-carry-1"
    rows = _rows(carry)
    assert rows["handoff"].startswith("stay_out: perp flat 0%, spot WETH weight 0")
    assert rows["lights"] == "none"
    assert rows["span"].endswith("0 light(s), 0 day(s) held by D5")


def test_the_overview_has_no_tables_and_the_line_page_has_them(tmp_path):
    directory = status_dir(tmp_path)
    page = load_page(directory, now=NOW, stale=STALE)
    assert all(card.tables == () for card in page.lines)
    hl = load_line(directory, "hl-paper", now=NOW, stale=STALE)
    assert hl is not None and hl.link is None
    assert [table.title for table in hl.tables] == [
        "recent cycles (newest first)",
        "recent fills (newest first)",
        "recent account snapshots (newest first)",
        "kill-switch events (newest first)",
        "safe-mode events (newest first)",
    ]
    assert len(hl.tables[3].rows) == 2
    carry = load_line(directory, "carry", now=NOW, stale=STALE)
    assert [t.title for t in carry.tables] == ["days (oldest first)", "weeks", "sidecar problems"]
    assert len(carry.tables[0].rows) == 3
    uni = load_line(directory, "uniswap-trend", now=NOW, stale=STALE)
    assert [t.title for t in uni.tables] == [
        "recent decisions (oldest first)",
        "reason codes",
        "store",
    ]
    store = dict(uni.tables[2].rows)
    assert store["pools"] == (
        "WETH/USDC 0.05%: 120 reading(s), 2026-06-01T00:00:00Z to 2026-10-14T05:00:00Z"
    )
    assert store["latest bars"] == "2026-10-14T00:00:00Z complete WETH 2567.89"
    assert store["coverage"] == "bars from chain: 120 of 120"


def test_a_line_the_index_does_not_know_has_no_page(tmp_path):
    directory = status_dir(tmp_path)
    assert load_line(directory, "nope", now=NOW, stale=STALE) is None
    assert load_line(directory, "../exporter", now=NOW, stale=STALE) is None


# --- what never reaches the page ---------------------------------------------------


def test_no_whole_address_or_key_reaches_any_text_of_the_page(tmp_path):
    documents = quiet_documents()
    documents[CARRY_KEY] = carry_document(handoff=None, handoff_error=f"cannot read {AGENT_KEY}")
    directory = status_dir(tmp_path, documents)
    page = load_page(directory, now=NOW, stale=STALE)
    texts = list(_page_texts(page))
    for name in LINE_NAMES:
        texts.extend(_texts(load_line(directory, name, now=NOW, stale=STALE)))
    joined = "\n".join(texts)
    assert WALLET not in joined and AGENT_KEY not in joined
    # The address the perp verdict quotes is there, folded, and so is the key the carry error quotes.
    assert "wallet 0xabab...abab fine" in joined
    assert "cannot read 0xcdcd...cdcd" in joined


def test_a_free_text_is_cut_to_the_limit(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    document["cycles"]["recent"][0]["verdict"]["decision_reason"] = "x" * 1000
    documents[HL_KEY] = document
    hl = load_line(status_dir(tmp_path, documents), "hl-paper", now=NOW, stale=STALE)
    reason = hl.tables[0].rows[0][-1]
    assert len(reason) == 300 and reason.endswith("...")


# --- freshness ---------------------------------------------------------------------


def test_a_document_older_than_the_threshold_marks_its_card_stale_and_the_page_banner(tmp_path):
    page = page_at(tmp_path, later=timedelta(minutes=7))
    assert page.exporter.stale and page.exporter.tone == BAD
    assert all(card.stale for card in page.lines)
    assert page.banner == (
        "The exporter's last tick was 2026-10-14 05:00Z (7 min ago), older than 6 min: "
        "every card may be out of date."
    )
    fresh = page_at(tmp_path, later=timedelta(minutes=6))
    assert not fresh.exporter.stale and fresh.banner is None


def test_a_line_whose_document_is_older_than_the_others_is_stale_alone(tmp_path):
    documents = quiet_documents()
    documents[UNI_AI_KEY] = uniswap_document(
        uniswap_run(run_id="paper-ai-1"), generated_at="2026-10-14T04:00:00Z"
    )
    page = page_at(tmp_path, documents)
    assert _line(page, "uniswap-ai").stale and not _line(page, "uniswap-trend").stale
    assert _line(page, "uniswap-ai").generated_at == "2026-10-14 04:00Z (1 h 1 min ago)"


def test_ago_reads_as_an_operator_would():
    assert ago(timedelta(seconds=30)) == "under a minute ago"
    assert ago(timedelta(minutes=3)) == "3 min ago"
    assert ago(timedelta(hours=2, minutes=10)) == "2 h 10 min ago"
    assert ago(timedelta(days=3)) == "3 d ago"
    assert ago(timedelta(minutes=-90)) == "in 1 h 30 min"


# --- the conditions ----------------------------------------------------------------


def test_a_condition_about_a_line_turns_its_card_bad_and_lists_it_on_the_alerts_card(tmp_path):
    down = unit_text(active="failed", sub="failed", result="exit-code")
    page = page_at(tmp_path, units=dict(UNITS_QUIET, **{"hl-paper.service": down}))
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD
    assert hl.status == "1 condition(s) hold: unit:hl-paper.service:down"
    alert_rows = [row for row in hl.rows if row.label == "alert"]
    assert len(alert_rows) == 1 and alert_rows[0].tone == BAD
    assert alert_rows[0].value.startswith("down since 2026-10-14 05:00Z (1 min ago): ")
    assert _line(page, "carry-perp").tone == OK
    assert page.alerts.tone == BAD
    assert page.alerts.tables[0].rows[0][0] == "unit:hl-paper.service:down"


def test_a_line_that_could_not_be_read_is_bad_with_the_exporters_reason(tmp_path):
    documents = quiet_documents()
    documents[HL_KEY] = CommandResult(5, "", f"validate: store damaged near {AGENT_KEY}")
    page = page_at(tmp_path, documents)
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD and hl.stale
    assert hl.status == (
        "1 condition(s) hold: hl-paper:store_damaged; not read: exit 5 — "
        "validate: store damaged near 0xcdcd...cdcd; hl-paper.json not written yet"
    )
    assert page.exporter.tone == BAD
    assert _rows(page.exporter)["lines read"] == "5 of 6"


def test_a_document_of_another_shape_takes_its_card_bad_not_the_page_down(tmp_path):
    documents = quiet_documents()
    documents[HL_KEY] = hyperliquid_document(account="not a block")
    page = page_at(tmp_path, documents)
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD
    assert hl.status.endswith("document not shaped as this build reads it (TypeError)")
    assert "not a block" not in "\n".join(_texts(hl))
    assert _line(page, "carry-perp").tone == OK


def test_the_carry_card_lights_held_by_d5_on_its_newest_day(tmp_path):
    documents = quiet_documents()
    today = NOW.replace(hour=0, minute=0)
    light = [{"kind": "held by D5", "text": "held"}]
    days = [carry_day(today - timedelta(days=n), light if n == 1 else ()) for n in (3, 2, 1)]
    documents[CARRY_KEY] = carry_document(days)
    carry = _line(page_at(tmp_path, documents), "carry")
    rows = {row.label: row for row in carry.rows}
    assert rows["lights"].value == "held by D5" and rows["lights"].tone == BAD


# --- the files themselves -----------------------------------------------------------


def test_a_missing_exporter_document_gives_a_page_with_a_banner_and_no_lines(tmp_path):
    directory = tmp_path / "status"
    directory.mkdir()
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.lines == ()
    assert page.banner == "No exporter tick to show: exporter.json not written yet."
    assert page.exporter.tone == BAD and page.exporter.status == "exporter.json not written yet"
    assert page.host.status == "host.json not written yet"
    assert page.alerts.status == "alerts.json not written yet"
    assert load_line(directory, "hl-paper", now=NOW, stale=STALE) is None


def test_a_corrupt_file_is_named_not_shown(tmp_path):
    directory = status_dir(tmp_path)
    (directory / "hl-paper.json").write_text("{not json", encoding="utf-8")
    (directory / "host.json").write_text("[1, 2]", encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD and hl.status == "hl-paper.json not JSON"
    assert page.host.status == "host.json not a JSON object"
    # The units are not known either: every line says so without failing.
    assert _rows(hl)["unit"] == "hl-paper.service: not in host.json"


def test_the_index_may_name_a_bare_file_only(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["lines"][0]["file"] = "../exporter.json"
    exporter["lines"][1]["file"] = "/etc/passwd"
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    for name in ("hl-paper", "carry-perp"):
        card = _line(page, name)
        assert card.tone == BAD and card.status == "the index names no file for this line"
    assert _line(page, "uniswap-trend").tone == OK


def test_read_document_tells_a_missing_file_from_a_broken_one(tmp_path):
    missing = read_document(tmp_path / "none.json")
    assert missing.document is None and missing.error == "not written yet"
    (tmp_path / "bad.json").write_text("nope", encoding="utf-8")
    assert read_document(tmp_path / "bad.json").error == "not JSON"
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    assert read_document(tmp_path / "ok.json").document == {"a": 1}


# --- the sparkline -----------------------------------------------------------------


def test_a_sparkline_needs_two_amounts_and_spans_the_box():
    assert sparkline(["1"], "x") is None
    assert sparkline(["a", None, "2"], "x") is None
    line = sparkline(["1", "3", "2"], "equity")
    assert line is not None
    assert line.first == "1" and line.last == "2" and line.count == 3
    assert line.viewbox == "0 0 240 48"
    assert line.points == "2.0,46.0 120.0,2.0 238.0,24.0"


def test_the_perp_curve_reads_oldest_first_though_the_document_is_newest_first(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    snapshots = document["account"]["recent_snapshots"]
    newest = dict(snapshots[0], account_equity="10300.00")
    older = dict(snapshots[0], account_equity="10100.00", timestamp="2026-10-14T00:00:00+00:00")
    document["account"]["recent_snapshots"] = [newest, older]
    documents[HL_KEY] = document
    hl = _line(page_at(tmp_path, documents), "hl-paper")
    assert hl.sparkline is not None
    assert hl.sparkline.first == "10100.00" and hl.sparkline.last == "10300.00"


# --- the rows the review asked to see ------------------------------------------------


def test_a_file_that_is_not_utf8_names_itself_and_the_page_stands(tmp_path):
    directory = status_dir(tmp_path)
    (directory / "hl-paper.json").write_bytes(b"\xff\xfe\x00 not text")
    page = load_page(directory, now=NOW, stale=STALE)
    assert _line(page, "hl-paper").status == "hl-paper.json not UTF-8"
    assert read_document(directory / "hl-paper.json").error == "not UTF-8"


def test_a_oneshot_that_failed_is_bad_by_its_active_state_not_its_result(tmp_path):
    failed = unit_text(type_="oneshot", active="failed", sub="failed", result="exit-code", status=1)
    page = page_at(tmp_path, units=dict(UNITS_QUIET, **{"uniswap-v3-paper.service": failed}))
    unit = next(row for row in _line(page, "uniswap-ai").rows if row.label == "unit")
    assert unit.tone == BAD and unit.value.startswith(
        "uniswap-v3-paper.service: failed (failed), last run exit-code"
    )
    states = {row[0]: row[1] for row in page.host.tables[0].rows}
    assert states["uniswap-v3-paper.service"].startswith("failed (failed), last run exit-code")
    assert states["carry-signal.service"].startswith("inactive (dead), last run success")


def test_the_alerts_card_shows_the_newest_events_newest_first(tmp_path):
    directory = status_dir(tmp_path)
    alerts = json.loads((directory / "alerts.json").read_text(encoding="utf-8"))
    alerts["recent_events"] = [
        {
            "key": f"hl-paper:event:k@{n}",
            "line": "hl-paper",
            "kind": "kill_switch_armed",
            "at": f"2026-10-{n:02d}T00:00:00+00:00",
            "text": f"event {n}",
        }
        for n in range(1, 14)
    ]
    (directory / "alerts.json").write_text(json.dumps(alerts), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    table = page.alerts.tables[1]
    assert table.title == "recent events (newest first)"
    assert [row[3] for row in table.rows] == [f"event {n}" for n in range(13, 3, -1)]


def test_the_exporter_card_is_bad_when_the_mail_or_the_ping_failed(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["mail"] = {
        "enabled": True,
        "sent": 0,
        "digest_sent": False,
        "pending": 2,
        "error": "SMTPAuthenticationError: refused",
    }
    exporter["deadman"] = {"enabled": True, "ping": "fail", "error": None}
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    rows = {row.label: row for row in page.exporter.rows}
    assert page.exporter.tone == BAD
    assert (
        rows["mail"].value == "sent 0, pending 2; SMTPAuthenticationError: refused"
        and rows["mail"].tone == BAD
    )
    assert rows["dead-man ping"].value == "fail" and rows["dead-man ping"].tone == BAD


def test_a_perp_run_in_safe_mode_with_a_failed_export_says_so_in_red(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document(
        safe_mode={
            "type": "manual",
            "reason": "reconcile mismatch",
            "entered_at": "2026-10-14T04:30:00+00:00",
        }
    )
    document["scheduler"]["export"] = {
        "status": "failed",
        "at": "2026-10-14T04:00:00+00:00",
        "error": "disk full",
    }
    document["scheduler"]["in_progress_attempt_id"] = "att-7"
    documents[HL_KEY] = document
    rows = {row.label: row for row in _line(page_at(tmp_path, documents), "hl-paper").rows}
    assert (
        rows["safe mode"].value == "manual since 2026-10-14 04:30Z (31 min ago): reconcile mismatch"
    )
    assert rows["safe mode"].tone == BAD
    assert (
        rows["export"].value == "failed 2026-10-14 04:00Z (1 h 1 min ago): disk full"
        and rows["export"].tone == BAD
    )
    assert rows["in progress"].value == "attempt att-7"


def test_a_uniswap_document_without_a_run_or_without_metrics_says_so(tmp_path):
    documents = quiet_documents()
    without_run = uniswap_document()
    without_run["run"] = None
    documents[UNI_AI_KEY] = without_run
    documents["contrib.uniswap_v3:paper-trend-1"] = uniswap_document(
        uniswap_run(metrics=None, metrics_error="fewer than two bars")
    )
    page = page_at(tmp_path, documents)
    ai = _line(page, "uniswap-ai")
    assert [row.value for row in ai.rows if row.label == "run"] == ["the document carries no run"]
    trend = {row.label: row for row in _line(page, "uniswap-trend").rows}
    assert trend["metrics"].value == "fewer than two bars" and "strategy" not in trend


def test_a_shared_document_of_another_shape_reddens_its_card_not_the_page(tmp_path):
    directory = status_dir(tmp_path)
    host = json.loads((directory / "host.json").read_text(encoding="utf-8"))
    host["disk"] = "full"
    (directory / "host.json").write_text(json.dumps(host), encoding="utf-8")
    alerts = json.loads((directory / "alerts.json").read_text(encoding="utf-8"))
    alerts["recent_events"] = "x"
    (directory / "alerts.json").write_text(json.dumps(alerts), encoding="utf-8")
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["mail"] = "x"
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.host.status == "host.json not shaped as this build reads it (TypeError)"
    assert page.alerts.status == "alerts.json not shaped as this build reads it (TypeError)"
    assert page.exporter.status == "exporter.json not shaped as this build reads it (TypeError)"
    assert all(card.tone == BAD and card.stale for card in (page.host, page.alerts, page.exporter))
    # The lines are still there: the index itself was readable.
    assert tuple(card.name for card in page.lines) == LINE_NAMES


def test_an_index_of_another_shape_gives_no_lines_and_names_itself(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["lines"] = "x"
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.lines == ()
    assert page.exporter.status == "exporter.json not shaped as this build reads it (TypeError)"
    assert page.banner == (
        "No exporter tick to show: exporter.json not shaped as this build reads it (TypeError)."
    )


def test_a_unit_that_is_not_installed_is_bad_on_its_line(tmp_path):
    from .support import NOT_FOUND

    page = page_at(tmp_path, units=dict(UNITS_QUIET, **{"carry-signal.service": NOT_FOUND}))
    unit = next(row for row in _line(page, "carry").rows if row.label == "unit")
    assert unit.value == "carry-signal.service: not installed" and unit.tone == BAD


def test_the_page_shows_every_kind_of_line_the_exporter_config_knows():
    from contrib.dashboard.app.documents import _BUILDERS
    from contrib.dashboard.config import _KINDS

    assert set(_BUILDERS) == set(_KINDS)


# --- the first review round -------------------------------------------------------


def test_a_red_row_without_a_rule_behind_it_reddens_the_card(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    document["cycles"]["recent"][0].update(
        status="api_failed", error_type="RateLimit", error_message="429", verdict=None
    )
    documents[HL_KEY] = document
    today = NOW.replace(hour=0, minute=0)
    light = [{"kind": "one-legged", "text": "perp in, spot out"}]
    days = [carry_day(today - timedelta(days=n), light if n == 1 else ()) for n in (3, 2, 1)]
    documents[CARRY_KEY] = carry_document(days)
    page = page_at(tmp_path, documents)
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD
    assert hl.status == "quiet by the rules, but 1 row(s) red: last cycle"
    carry = _line(page, "carry")
    assert carry.tone == BAD and carry.status.endswith("row(s) red: lights")
    assert _line(page, "carry-perp").tone == OK


def test_a_down_unit_reddens_its_line_even_when_alerts_json_is_unreadable(tmp_path):
    down = unit_text(active="failed", sub="failed", result="exit-code")
    directory = status_dir(tmp_path, units=dict(UNITS_QUIET, **{"hl-paper.service": down}))
    (directory / "alerts.json").write_text("{broken", encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    hl = _line(page, "hl-paper")
    assert hl.tone == BAD and hl.status == "quiet by the rules, but 1 row(s) red: unit"
    assert page.alerts.status == "alerts.json not JSON"


def test_a_closed_position_at_size_zero_reads_flat(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    document["positions"][0].update(size="0", entry_price=None, stop_loss_price=None)
    documents[HL_KEY] = document
    rows = _rows(_line(page_at(tmp_path, documents), "hl-paper"))
    assert rows["position"] == "flat" and "position BTC" not in rows


def test_an_instant_out_of_range_is_shown_as_text_not_a_500(tmp_path):
    documents = quiet_documents()
    documents[HL_KEY] = hyperliquid_document(generated_at="0001-01-01T00:00:00+14:00")
    hl = _line(page_at(tmp_path, documents), "hl-paper")
    assert hl.stale and hl.generated_at == "0001-01-01T00:00:00+14:00"


def test_a_line_document_without_generated_at_is_bad_not_merely_stale(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    del document["generated_at"]
    documents[HL_KEY] = document
    hl = _line(page_at(tmp_path, documents), "hl-paper")
    assert hl.tone == BAD and hl.stale
    assert hl.status == "the document carries no generated_at"


def test_notes_that_are_not_a_list_make_the_alerts_card_not_shaped(tmp_path):
    directory = status_dir(tmp_path)
    alerts = json.loads((directory / "alerts.json").read_text(encoding="utf-8"))
    alerts["notes"] = "abc"
    (directory / "alerts.json").write_text(json.dumps(alerts), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.alerts.status == "alerts.json not shaped as this build reads it (TypeError)"
    assert not any(row.label == "note" for row in page.alerts.rows)


def test_a_readable_exporter_document_of_another_shape_is_not_called_a_stale_tick(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["mail"] = "x"
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.banner == (
        "exporter.json not shaped as this build reads it (TypeError): "
        "every card may be out of date."
    )
    assert tuple(card.name for card in page.lines) == LINE_NAMES


def test_an_inactive_oneshot_whose_result_is_exit_code_is_not_red(tmp_path):
    odd = unit_text(type_="oneshot", active="inactive", sub="dead", result="exit-code", status=1)
    page = page_at(tmp_path, units=dict(UNITS_QUIET, **{"carry-signal.service": odd}))
    unit = next(row for row in _line(page, "carry").rows if row.label == "unit")
    assert unit.tone == "" and "last run exit-code" in unit.value


def test_the_exporter_card_says_what_failed_while_it_ticks(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["deadman"] = {"enabled": True, "ping": "fail", "error": "timed out"}
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.exporter.status == "ticking, with a failure: dead-man ping"


def test_the_view_models_hold_their_invariants():
    import pytest

    from contrib.dashboard.app.documents import Loaded, Sparkline, Table

    with pytest.raises(ValueError):
        Loaded("x.json", None, None)
    with pytest.raises(ValueError):
        Loaded("x.json", {}, "an error too")
    with pytest.raises(ValueError):
        Table("t", ("a", "b"), (("only one",),))
    assert Sparkline("0,0 1,1", "x", "1", "2", 2).viewbox == "0 0 240 48"


# --- the second review round ------------------------------------------------------


def test_a_light_in_the_spans_history_does_not_keep_the_carry_card_red(tmp_path):
    documents = quiet_documents()
    document = carry_document()
    document["span"]["alerts"] = 3
    document["problems"] = [{"path": "payloads/2026-10-01.json", "reason": "not JSON"}]
    documents[CARRY_KEY] = document
    carry = _line(page_at(tmp_path, documents), "carry")
    assert carry.tone == OK and carry.status == "quiet"
    rows = {row.label: row for row in carry.rows}
    assert rows["span"].tone == "warn" and rows["problems"].tone == "warn"


def test_letters_pending_redden_the_alerts_card(tmp_path):
    directory = status_dir(tmp_path)
    alerts = json.loads((directory / "alerts.json").read_text(encoding="utf-8"))
    alerts["pending_letters"] = 2
    (directory / "alerts.json").write_text(json.dumps(alerts), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.alerts.tone == BAD
    assert page.alerts.status == "nothing holds; letters pending"


def test_an_exporter_document_without_generated_at_says_so_in_the_banner(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    del exporter["generated_at"]
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.exporter.status == "exporter.json carries no generated_at this build reads"
    assert page.banner == (
        "exporter.json carries no generated_at this build reads: every card may be out of date."
    )


def test_a_generated_at_that_is_not_an_instant_is_bad_not_merely_stale(tmp_path):
    documents = quiet_documents()
    documents[HL_KEY] = hyperliquid_document(generated_at="yesterday-ish")
    hl = _line(page_at(tmp_path, documents), "hl-paper")
    assert hl.tone == BAD and hl.stale
    assert hl.status == "the document's generated_at is not an instant"


def test_an_exporter_generated_at_that_is_not_an_instant_says_so(tmp_path):
    directory = status_dir(tmp_path)
    exporter = json.loads((directory / "exporter.json").read_text(encoding="utf-8"))
    exporter["generated_at"] = "yesterday-ish"
    (directory / "exporter.json").write_text(json.dumps(exporter), encoding="utf-8")
    page = load_page(directory, now=NOW, stale=STALE)
    assert page.exporter.status == "exporter.json's generated_at is not an instant"
    assert page.banner == (
        "exporter.json's generated_at is not an instant: every card may be out of date."
    )


def test_a_replay_mismatch_reddens_the_card_and_a_config_drift_warns(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    document["scheduler"]["replay"] = {
        "status": "mismatch",
        "at": "2026-10-14T04:00:00+00:00",
        "error": "materialised state contradicted",
    }
    document["scheduler"]["config_drift"] = {
        "status": "drift",
        "at": "2026-10-14T04:00:00+00:00",
        "error": "risk block differs from genesis",
    }
    documents[HL_KEY] = document
    hl = _line(page_at(tmp_path, documents), "hl-paper")
    rows = {row.label: row for row in hl.rows}
    assert rows["replay"].tone == BAD and rows["config drift"].tone == "warn"
    assert hl.tone == BAD and hl.status == "quiet by the rules, but 1 row(s) red: replay"
