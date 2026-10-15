"""The three packages' documents, as this package reads them: versions and field names, off their sources.

This package imports none of the three (``contrib/tests/test_package_graph.py``
holds that), so it reads their ``status.py`` as text: the ``SCHEMA_VERSION``
each declares must be the one :data:`KNOWN_SCHEMA_VERSIONS` reads, and the
field names of the dataclasses the rules and the digest walk must be the
keys the test documents in ``support.py`` carry (as sets: the order is the
package's) — so a package that renames or drops a field fails here, in the PR that does it, not on the host.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from contrib.dashboard.alerts import HELD_BY_D5, KILL_SWITCH_REFRESH
from contrib.dashboard.readers import KNOWN_SCHEMA_VERSIONS

from . import support

_CONTRIB = Path(__file__).resolve().parents[2]
_STATUS = {
    "hyperliquid_perp": _CONTRIB / "hyperliquid_perp" / "status.py",
    "uniswap_v3": _CONTRIB / "uniswap_v3" / "status.py",
    "carry": _CONTRIB / "carry" / "status.py",
}


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _constant(module: ast.Module, name: str) -> object:
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no {name} in the module")


def _fields(module: ast.Module, class_name: str) -> list[str]:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and "ClassVar" not in ast.unparse(item.annotation)
            ]
    raise AssertionError(f"no class {class_name} in the module")


@pytest.mark.parametrize("source", sorted(KNOWN_SCHEMA_VERSIONS))
def test_the_schema_version_this_build_reads_is_the_one_the_package_declares(source):
    module = _module(_STATUS[source])
    assert _constant(module, "SCHEMA_VERSION") == KNOWN_SCHEMA_VERSIONS[source]
    assert _constant(module, "SOURCE") == source


def test_the_hyperliquid_document_carries_the_fields_of_the_perp_status_model():
    module = _module(_STATUS["hyperliquid_perp"])
    doc = support.hyperliquid_document()
    assert sorted(doc) == sorted(_fields(module, "RunStatus"))
    assert sorted(doc["scheduler"]) == sorted(_fields(module, "Scheduler"))
    assert sorted(doc["scheduler"]["export"]) == sorted(_fields(module, "Breadcrumb"))
    assert sorted(doc["cycles"]) == sorted(_fields(module, "Cycles"))
    assert sorted(doc["cycles"]["recent"][0]) == sorted(_fields(module, "Cycle"))
    assert sorted(doc["cycles"]["recent"][0]["verdict"]) == sorted(_fields(module, "Verdict"))
    assert sorted(doc["models"][0]) == sorted(_fields(module, "ModelBucket"))
    assert sorted(doc["account"]) == sorted(_fields(module, "Account"))
    assert sorted(doc["account"]["ledger"]) == sorted(_fields(module, "Ledger"))
    assert sorted(doc["account"]["snapshot"]) == sorted(_fields(module, "AccountSnapshot"))
    assert sorted(doc["positions"][0]) == sorted(_fields(module, "Position"))
    assert sorted(doc["recent_fills"][0]) == sorted(_fields(module, "Fill"))
    assert sorted(doc["kill_switch_events"][0]) == sorted(_fields(module, "KillSwitchEvent"))
    event = support.safe_mode_event(support.NOW, "safe_mode_entered", "manual")
    assert sorted(event) == sorted(_fields(module, "SafeModeEvent"))
    assert _fields(module, "SafeMode") == ["type", "reason", "entered_at"]


def test_the_uniswap_document_carries_the_fields_of_the_uniswap_status_model():
    module = _module(_STATUS["uniswap_v3"])
    doc = support.uniswap_document()
    assert sorted(doc) == sorted(_fields(module, "Status"))
    assert sorted(doc["run"]) == sorted(_fields(module, "RunStatus"))
    assert sorted(doc["run"]["clock"]) == sorted(_fields(module, "Clock"))
    assert sorted(doc["store"]) == sorted(_fields(module, "StoreStatus"))
    assert sorted(doc["run"]["holdings"]) == sorted(_fields(module, "Holdings"))
    assert sorted(doc["run"]["metrics"]) == sorted(_fields(module, "Metrics"))
    assert sorted(doc["run"]["metrics"]["strategy"]) == sorted(_fields(module, "CurveStatus"))
    assert sorted(doc["run"]["metrics"]["costs"]) == sorted(_fields(module, "CostsStatus"))
    assert sorted(doc["run"]["recent_decisions"][0]) == sorted(_fields(module, "DecisionStatus"))
    assert sorted(doc["run"]["recent_valuations"][0]) == sorted(_fields(module, "ValuationPoint"))
    assert _fields(module, "VerdictsSeen") == ["source", "every", "some", "none"]


def test_the_carry_document_carries_the_fields_of_the_carry_report_model():
    module = _module(_STATUS["carry"])
    doc = support.carry_document()
    assert sorted(doc) == sorted(_fields(module, "ReportStatus"))
    assert sorted(doc["handoff"]) == sorted(_fields(module, "HandoffStatus"))
    assert sorted(doc["days"][0]) == sorted(_fields(module, "DayStatus"))
    assert sorted(support.light("x")) == sorted(_fields(module, "AlertStatus"))
    assert sorted(support.problem()) == sorted(_fields(module, "ProblemStatus"))
    report = _module(_CONTRIB / "carry" / "report.py")
    assert sorted(doc["span"]) == sorted(_fields(report, "SpanTotals"))
    assert sorted(doc["weeks"][0]) == sorted(_fields(report, "WeekRow"))


def test_the_vocabulary_words_the_rules_match_are_the_packages_words():
    vocab = _CONTRIB / "hyperliquid_perp" / "persistence" / "repository" / "_vocab.py"
    assert f'"{KILL_SWITCH_REFRESH}"' in vocab.read_text(encoding="utf-8")
    carry_report = (_CONTRIB / "carry" / "report.py").read_text(encoding="utf-8")
    assert f'"{HELD_BY_D5}"' in carry_report
    uniswap = _STATUS["uniswap_v3"].read_text(encoding="utf-8")
    for state in ("up_to_date", "behind", "ahead"):
        assert f'"{state}"' in uniswap


def test_the_carry_lines_default_d5_days_is_the_carry_packages_own_default():
    from contrib.dashboard.config import CarryLine

    report = (_CONTRIB / "carry" / "report.py").read_text(encoding="utf-8")
    assert f"DEFAULT_D5_DAYS: Final = {CarryLine.d5_days}" in report
