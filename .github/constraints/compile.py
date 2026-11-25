"""Write the constraints files CI's required jobs install from (issue #442).

    python .github/constraints/compile.py

One file per Python version of the test matrix in ``ci.yml``, each the set of
versions ``uv pip compile`` resolves on that Python, for Linux, from every
requirement a CI job installs: the project's own with the ``dev`` and
``discovery-tools`` extras, the two requirements files beside it, and
``ci.in`` (what only a workflow step names). A job then installs with
``-c .github/constraints/py<version>.txt``, so what it runs on changes when
one of these files changes and not when an index publishes a release.

One file serves every job of its Python version: the union resolves, and a
constraint on a package a job never installs does nothing. A file per Python
because the versions differ between them (a release that has dropped 3.10 is
still the newest one 3.13 can take).

Needs ``uv`` on PATH, and nothing else: it runs on any platform, since the
platform resolved for is named and not the one it runs on. ``--upgrade``
because an output file that already exists is otherwise read back as a
preference, and a second run would only confirm the first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
DIRECTORY: Final = ".github/constraints"

PYTHON_VERSIONS: Final = ("3.10", "3.11", "3.12", "3.13")
EXTRAS: Final = ("dev", "discovery-tools")
REQUIREMENTS: Final = (
    "requirements.txt",
    "contrib/uniswap_v3/requirements.txt",
    f"{DIRECTORY}/ci.in",
)
PLATFORM: Final = "linux"
# requirements.txt names the project itself as ".", which uv would write back
# as a line of the output; pip refuses a constraint that is a path.
PROJECT: Final = "tradingagents"
# What uv is passed and does not copy into the header it writes.
UNRECORDED: Final = ("--upgrade", "--quiet")


def output(version: str) -> str:
    """The constraints file of one Python version, as a path from the repo root."""
    return f"{DIRECTORY}/py{version}.txt"


def command(version: str) -> list[str]:
    """The ``uv`` call that writes one version's file, as uv copies it into the file's header."""
    extras = [word for extra in EXTRAS for word in ("--extra", extra)]
    return [
        "uv",
        "pip",
        "compile",
        "pyproject.toml",
        *extras,
        *REQUIREMENTS,
        "--python-version",
        version,
        "--python-platform",
        PLATFORM,
        "--no-emit-package",
        PROJECT,
        "--annotation-style",
        "line",
        "--output-file",
        output(version),
    ]


def main() -> int:
    written: list[str] = []
    for version in PYTHON_VERSIONS:
        print(f"resolving for Python {version}", flush=True)
        try:
            done = subprocess.run([*command(version), *UNRECORDED], cwd=ROOT, check=False)
        except FileNotFoundError:
            print("uv is not on PATH (pip install uv); nothing was written")
            return 1
        if done.returncode:
            # The files are one set: half of them moved is a set no run wrote.
            before = f"{', '.join(written)} already rewritten" if written else "none rewritten"
            print(
                f"uv refused Python {version} ({before}); the files after it were not written. "
                f"Put the directory back (git restore {DIRECTORY}/py*.txt) or run this again"
            )
            return done.returncode
        written.append(output(version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
