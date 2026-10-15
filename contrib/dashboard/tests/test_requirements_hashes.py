"""requirements.txt lists every wheel's sha256, and the installer and the CI job insist on them.

Text only (no import of pip): a pin without a hash, or a hash line that
pip would read as a requirement of its own, is a refusal on the host
(``--require-hashes``, issue #366) and must fail here first.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1]
REQUIREMENTS = DASHBOARD / "requirements.txt"
INSTALLER = DASHBOARD / "schedule" / "install-web.sh"
PIN_HASHES = DASHBOARD / "schedule" / "pin-hashes.py"
CI = DASHBOARD.parents[1] / ".github" / "workflows" / "ci.yml"


def _load_pin_hashes():
    spec = importlib.util.spec_from_file_location("pin_hashes", PIN_HASHES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pin_hashes = _load_pin_hashes()


def _pins(text: str) -> dict[str, list[str]]:
    """Each pin's hashes, by the script's own grammar (the round-trip test holds the shape)."""
    pins: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        if match := pin_hashes.PIN.match(raw):
            current = f"{match['name']}=={match['version']}"
            assert current not in pins, f"{current} is pinned twice"
            pins[current] = []
            continue
        match = pin_hashes.HASH_LINE.match(raw)
        assert match and current, f"not a pin or a hash line: {raw!r}"
        pins[current].append(match["digest"])
    return pins


def test_every_pin_has_wheel_hashes_and_the_file_is_what_pin_hashes_writes_for_them():
    """The file's shape is the script's (continuations, indentation), not a hand edit's."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    pins = _pins(text)
    # the three the page imports, and their resolved set, are all here
    names = {pin.split("==")[0].lower().replace("_", "-") for pin in pins}
    assert {
        "fastapi",
        "uvicorn",
        "jinja2",
        "starlette",
        "pydantic",
        "pydantic-core",
        "markupsafe",
    } <= names
    assert pin_hashes.rewrite(text, lambda name, version: pins[f"{name}=={version}"]) == text


def test_the_installer_and_the_ci_job_install_with_require_hashes():
    installer = INSTALLER.read_text(encoding="utf-8")
    assert installer.count("-m pip install") == 1
    assert (
        '--require-hashes \\\n    -r "$APP_DIR.new/contrib/dashboard/requirements.txt"' in installer
    )

    ci = CI.read_text(encoding="utf-8")
    step = ci.split("Install the dashboard page's requirements", 1)[1].split("- name:", 1)[0]
    installs = [
        line.strip() for line in step.splitlines() if line.strip().startswith("pip install")
    ]
    pinned = [line for line in installs if "contrib/dashboard/requirements.txt" in line]
    assert pinned == ["pip install --require-hashes -r contrib/dashboard/requirements.txt"]
    # the pinned install is the last one: whatever the test client's httpx
    # moved is put back, and pip check then reports the conflict
    assert installs[-1] == pinned[0]
    assert "pip check" in step.split(pinned[0], 1)[1]


def test_pin_hashes_rewrite_keeps_comments_replaces_hashes_and_is_idempotent():
    fake = {("fastapi", "0.1"): ["a" * 64, "b" * 64], ("h11", "0.2"): ["c" * 64]}
    asked: list[tuple[str, str]] = []

    def hashes_for(name: str, version: str) -> list[str]:
        asked.append((name, version))
        return fake[(name, version)]

    before = "# header\nfastapi==0.1\n# h11's\nh11==0.2\n"
    once = pin_hashes.rewrite(before, hashes_for)
    assert asked == [("fastapi", "0.1"), ("h11", "0.2")]
    assert _pins(once) == {"fastapi==0.1": ["a" * 64, "b" * 64], "h11==0.2": ["c" * 64]}
    assert once.splitlines()[0] == "# header"
    assert "# h11's" in once
    # a second run reads its own output: old hash lines are dropped, not
    # doubled, an unindented one (pip accepts it) included
    fake[("fastapi", "0.1")] = ["d" * 64]
    unindented = once.replace("    --hash=sha256:" + "c" * 64, "--hash=sha256:" + "c" * 64)
    twice = pin_hashes.rewrite(unindented, hashes_for)
    assert _pins(twice) == {"fastapi==0.1": ["d" * 64], "h11==0.2": ["c" * 64]}
    assert pin_hashes.rewrite(twice, hashes_for) == twice


@pytest.mark.parametrize(
    "line",
    [
        "uvicorn==0.34.2  # bumped",
        'exceptiongroup==1.3.0; python_version < "3.11"',
        "uvicorn==0.34.2 --hash=sha256:" + "e" * 64,
        "uvicorn>=0.34,==0.34.*",
        "-r other.txt",
    ],
)
def test_a_line_the_script_would_not_have_written_is_a_refusal_not_a_pass_through(line):
    with pytest.raises(SystemExit, match="line 2 is not a bare name==version pin"):
        pin_hashes.rewrite("# header\n" + line + "\n", lambda name, version: ["a" * 64])


def _serving(payload: dict | Exception):
    def urlopen(url, timeout):
        assert url == "https://pypi.org/pypi/fastapi/0.1/json"
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(json.dumps(payload).encode())

    return urlopen


def test_wheel_hashes_takes_every_wheel_sorted_by_filename_and_no_sdist_or_yanked_file():
    release = {
        "urls": [
            {
                "filename": "fastapi-0.1-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "digests": {"sha256": "b" * 64},
            },
            {
                "filename": "fastapi-0.1.tar.gz",
                "packagetype": "sdist",
                "digests": {"sha256": "s" * 64},
            },
            {
                "filename": "fastapi-0.1-cp312-cp312-win_amd64.whl",
                "packagetype": "bdist_wheel",
                "yanked": True,
                "digests": {"sha256": "y" * 64},
            },
            {
                "filename": "fastapi-0.1-cp312-cp312-manylinux_2_17_x86_64.whl",
                "packagetype": "bdist_wheel",
                "yanked": False,
                "digests": {"sha256": "a" * 64},
            },
        ]
    }
    assert pin_hashes.wheel_hashes("fastapi", "0.1", urlopen=_serving(release)) == [
        "a" * 64,
        "b" * 64,
    ]


def test_wheel_hashes_refuses_a_release_without_a_wheel_and_names_the_pin_on_a_404():
    with pytest.raises(SystemExit, match="fastapi==0.1 has no wheel"):
        pin_hashes.wheel_hashes("fastapi", "0.1", urlopen=_serving({"urls": []}))
    not_found = urllib.error.HTTPError(
        "https://pypi.org/pypi/fastapi/0.1/json", 404, "Not Found", {}, None
    )
    with pytest.raises(SystemExit, match="fastapi==0.1: HTTP Error 404"):
        pin_hashes.wheel_hashes("fastapi", "0.1", urlopen=_serving(not_found))
