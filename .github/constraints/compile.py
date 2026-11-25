"""Write the constraints files CI's required jobs install from (issue #442).

    python .github/constraints/compile.py                         # fill in what changed
    python .github/constraints/compile.py --upgrade-package NAME  # move one package
    python .github/constraints/compile.py --upgrade               # move everything

One file per Python version of the test matrix in ``ci.yml``, each the set of
versions ``uv pip compile`` resolves on that Python, for Linux, from every
requirement a CI job installs under ``-c``: the project's own with the
``dev`` and ``discovery-tools`` extras, the root and the uniswap_v3
requirements files, and ``ci.in`` (what a workflow step installs by name:
declared nowhere else, or held for CI at a version of its own). The
dashboard's requirements are not among them: that file pins itself, by
hash. A job then installs with
``-c .github/constraints/py<version>.txt``, so what it runs on changes when
one of these files changes and not when an index publishes a release.

One file serves every job of its Python version, since the union resolves. A
line for a package a job never installs does nothing in that job — but the
package was part of the resolution, and what it requires decided the version
of packages the job does install (the README names the ones held back today:
a job alone would resolve them newer). A file per Python because the
versions differ between them (a release that has dropped 3.10 is still the
newest one 3.13 can take).

With no option a version already in a file is kept wherever it still
satisfies the requirements (uv reads the existing file back as its
preference), so a run after a dependency was added or a range moved writes
that change and nothing else. Taking what the index has published since is a
separate act, asked for by name: one package, or all of them. The command in
each file's header is therefore not the whole of how the file came to be;
the file's history is.

Needs ``uv`` on PATH, and nothing else: it runs on any platform, since the
platform resolved for is named and not the one it runs on.
"""

from __future__ import annotations

import argparse
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


def output(version: str) -> str:
    """The constraints file of one Python version, as a path from the repo root."""
    return f"{DIRECTORY}/py{version}.txt"


def command(version: str) -> list[str]:
    """The ``uv`` call that writes one version's file, as uv copies it into the file's header.

    uv leaves the upgrade options out of the header, so they are no part of this.
    """
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


def _moved(argv: list[str]) -> list[str]:
    """The upgrade options to hand uv: none, ``--upgrade``, or one ``--upgrade-package`` each."""
    parser = argparse.ArgumentParser(
        description="Write CI's constraints files, keeping the versions they hold unless asked."
    )
    asked = parser.add_mutually_exclusive_group()
    asked.add_argument(
        "--upgrade",
        action="store_true",
        help="take the newest release of every package the requirements allow",
    )
    asked.add_argument(
        "--upgrade-package",
        action="append",
        default=[],
        metavar="NAME",
        help="take the newest release of this package only (may be given more than once)",
    )
    args = parser.parse_args(argv)
    if args.upgrade:
        return ["--upgrade"]
    return [word for name in args.upgrade_package for word in ("--upgrade-package", name)]


def main(argv: list[str]) -> int:
    moved = _moved(argv)
    written: list[str] = []
    for version in PYTHON_VERSIONS:
        print(f"resolving for Python {version}", flush=True)
        try:
            # uv prints the file it wrote on stdout and what it has to say
            # (its warnings among it) on stderr: the first is dropped.
            done = subprocess.run(
                [*command(version), *moved], cwd=ROOT, check=False, stdout=subprocess.DEVNULL
            )
        except FileNotFoundError:
            print(
                "uv is not on PATH (the README beside this file names the version to "
                "pip install); nothing was written"
            )
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
    sys.exit(main(sys.argv[1:]))
