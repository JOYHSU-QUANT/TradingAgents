"""The no-decision policy's import-time guard on the cadence (issue #290)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from contrib.hyperliquid_perp.common.no_decision import _streak_hours

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_a_streak_renders_as_whole_hours_at_the_configured_cadence():
    # 3 x 4h. Exact, not floored: the cadence is pinned to whole hours at import.
    assert _streak_hours(3) == 12
    assert _streak_hours(0) == 0


def test_a_cadence_that_is_not_whole_hours_is_refused_at_import():
    # In a subprocess rather than via ``importlib.reload``: reloading would
    # rebind the module's classes under every test that already imported them.
    # The guard has to fire at IMPORT, before any wording could render "~0h".
    code = (
        "from datetime import timedelta\n"
        "from contrib.hyperliquid_perp.common import constants\n"
        "constants.CYCLE_INTERVAL = timedelta(minutes=30)\n"
        "import contrib.hyperliquid_perp.common.no_decision\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "CYCLE_INTERVAL must be a whole number of hours" in result.stderr
    assert "(got 0:30:00)" in result.stderr  # names the offending value (issue #290 anchor)
