"""List what is installed at another version than a constraints file pins.

    python .github/constraints/drift.py .github/constraints/py3.13.txt

Run by CI's ``test-latest`` job, which installs without the constraints
file, after its suite and whatever the suite did: the list is what an index
has moved since the file was written, and so where to look when that job's
suite is red and the required one's is green.
A report and never a verdict: it exits 0 whatever it lists. What it cannot
read (no file, a line that is no pin) is the one thing it exits non-zero on,
since a list drawn from half a file would read as "nothing moved".

A package installed and not pinned is listed too: a release that gains a
dependency installs something the file has never named.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

try:
    from packaging.version import InvalidVersion, Version
except ModuleNotFoundError:  # not the standard library's; the job's pytest brings it
    Version = None

_PIN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#]+)\s*(#.*)?")
# The distribution CI is testing, installed from the checkout: no file pins it.
PROJECT = "tradingagents"
# What an environment holds before anything is asked for; no constraints file
# written by ``uv pip compile`` for a set of requirements names them.
_PRESENT_ANYWAY = frozenset({"pip", "wheel"})


def normalized(name: str) -> str:
    """A distribution's name as an index compares it: case and ``-``/``_``/``.`` runs folded."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(text: str) -> dict[str, str]:
    """Each ``name==version`` line of a constraints file, by normalized name.

    Blank lines and comments are passed; any other line is a ``ValueError``
    naming it, and so is a name pinned twice.
    """
    found: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN.fullmatch(stripped)
        if match is None:
            raise ValueError(f"line {number} is not a name==version pin: {stripped!r}")
        name = normalized(match["name"])
        if name in found:
            raise ValueError(f"line {number} pins {name} a second time")
        found[name] = match["version"]
    return found


def same_release(pinned: str, installed: str) -> bool:
    """Whether two version strings name one release, however each spells it.

    A constraints file holds the normalized spelling (``2026.1.5``) and a
    distribution's metadata whatever its build wrote (``2026.01.05``). Where
    ``packaging`` is not importable, or one of the two is no version it
    reads, only the same text is the same release.
    """
    if pinned == installed or Version is None:
        return pinned == installed
    try:
        return Version(pinned) == Version(installed)
    except InvalidVersion:
        return False


def drift(pinned: dict[str, str], installed: dict[str, str], *, project: str) -> list[str]:
    """One line per installed distribution the file pins elsewhere or does not pin, sorted.

    ``project`` is the distribution being tested, which no constraints file
    names. A pin nothing installed is not drift: the file serves several jobs.
    """
    lines = []
    for name in sorted(installed):
        if name == project or name in _PRESENT_ANYWAY:
            continue
        version = installed[name]
        if name not in pinned:
            lines.append(f"{name}: installed {version}, not pinned")
        elif not same_release(pinned[name], version):
            lines.append(f"{name}: installed {version}, pinned {pinned[name]}")
    return lines


def installed_versions() -> dict[str, str]:
    """Every distribution this interpreter can import, by normalized name.

    A metadata directory with no name in it (what an interrupted uninstall
    leaves behind) is no installed package and is passed.
    """
    found = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name is not None:
            found[normalized(name)] = distribution.version
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: drift.py <constraints file>")
        return 2
    path = Path(argv[0])
    try:
        pinned = pins(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path} cannot be read as a constraints file: {exc}")
        return 2
    lines = drift(pinned, installed_versions(), project=PROJECT)
    if not lines:
        print(f"every installed package is at the version {path} pins")
        return 0
    print(f"{len(lines)} installed package(s) differ from {path}:")
    for line in lines:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
