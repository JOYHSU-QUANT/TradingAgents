#!/usr/bin/env python3
"""Write the sha256 of every wheel behind each pin in requirements.txt.

    python contrib/dashboard/schedule/pin-hashes.py [contrib/dashboard/requirements.txt]

The page's requirements (contrib/dashboard/requirements.txt) are installed
with ``pip install --require-hashes`` (schedule/install-web.sh and the CI
job): pip then installs a file only when its sha256 is one listed next to
the pin, so a wheel replaced on the index under the same version is refused
(issue #366). This script is how the list is made and remade: for each
``name==version`` line it asks PyPI's JSON API for the release's files and
writes every wheel's sha256 under the pin, dropping whatever hashes were
there. Every wheel of the release is listed, not only the host's (manylinux
x86_64 for the Lightsail host and CI, win_amd64 for a Windows checkout),
so one file serves every platform that installs it; sdists and yanked files
are left out — a platform without a wheel fails loudly instead of building
the package from source with build tools pip would fetch unpinned.

The file's grammar is four kinds of line: blank, comment, a bare
``name==version`` pin, a hash line. Anything else (a trailing comment, a
marker, an inline option, a range) is a refusal, not a pass-through, so the
file never holds a line this script would not have written. Standard
library only. To move a pin: edit the version, run this, commit both.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9.!+-]+)\s*\\?\s*$")
HASH_LINE = re.compile(r"^\s*--hash=sha256:(?P<digest>[0-9a-f]{64})\s*\\?\s*$")
INDEX = "https://pypi.org/pypi/{name}/{version}/json"


def wheel_hashes(name: str, version: str, *, urlopen=urllib.request.urlopen) -> list[str]:
    """The sha256 of each wheel PyPI serves for the release, sorted by filename."""
    try:
        with urlopen(INDEX.format(name=name, version=version), timeout=30) as response:
            release = json.load(response)
    except urllib.error.URLError as exc:  # HTTPError (a 404 for a version that is not there) too
        raise SystemExit(f"pin-hashes: {name}=={version}: {exc}") from None
    files = sorted(
        (f for f in release["urls"] if f["packagetype"] == "bdist_wheel" and not f.get("yanked")),
        key=lambda f: f["filename"],
    )
    if not files:
        raise SystemExit(f"pin-hashes: {name}=={version} has no wheel on PyPI")
    return [f["digests"]["sha256"] for f in files]


def rewrite(text: str, hashes_for=wheel_hashes) -> str:
    """The file with every pin followed by its wheels' hashes, old hash lines dropped."""
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if HASH_LINE.match(line):
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        match = PIN.match(line)
        if match is None:
            raise SystemExit(f"pin-hashes: line {number} is not a bare name==version pin: {line!r}")
        name, version = match["name"], match["version"]
        hashes = hashes_for(name, version)
        out.append(
            " \\\n".join([f"{name}=={version}", *(f"    --hash=sha256:{h}" for h in hashes)])
        )
        print(f"{name}=={version}: {len(hashes)} wheel(s)", file=sys.stderr)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    path = (
        Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "requirements.txt"
    )
    path.write_text(rewrite(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
