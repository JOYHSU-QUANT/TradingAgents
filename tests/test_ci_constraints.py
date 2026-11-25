"""CI's required jobs install under committed constraints files (issue #442).

The workflow is read as text and the constraints files as the lists they
are: nothing here installs anything or reaches an index. What is pinned is
the wiring a later edit could quietly undo — an install line added without
``-c``, a dependency declared and never resolved into the files, a file
edited by hand, a job renamed away from the name branch protection waits on.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: pytest itself installs tomli there
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / ".github" / "constraints"
CI = ROOT / ".github" / "workflows" / "ci.yml"

LATEST = "test-latest"
# The checks develop's branch protection requires, as GitHub names them (the
# eleventh, gitleaks, is another workflow's). A job renamed here and not
# there leaves every pull request waiting for a check that never reports.
REQUIRED_NAMES = {
    "test": "tests (py${{ matrix.python-version }})",
    "smoke-install": "clean-install smoke",
    "lint": "ruff (strict, full repo)",
    "typecheck": "mypy (pyproject [tool.mypy] files)",
    "autoresearch": "autoresearch + replay + carry + dashboard tests (py3.12)",
    "discovery": "discovery tests (py3.12)",
    "uniswap-v3": "uniswap_v3 tests (py3.12)",
}
# The install lines of a required job that take no constraints file, and why.
UNCONSTRAINED = {
    "python -m pip install --upgrade pip": "pip itself, which no file pins",
    'pip install "mypy==2.3.1"': "pinned where it is installed, to another version than dev's",
    "pip install --require-hashes --force-reinstall -r contrib/dashboard/requirements.txt": (
        "the dashboard's own pins, every one by hash"
    ),
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"ci_constraints_{name}", CONSTRAINTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compile_files = _load("compile")
drift = _load("drift")


MATRIX = "${{ matrix.python-version }}"
# Any way a step could put packages into the job's environment, not only the
# spelling in use: a line this matches and UNCONSTRAINED does not list has to
# carry the constraints flag. pip by what it is asked to do; the tools that
# build an environment of their own, and read no -c, by being named at all.
_INSTALLER = re.compile(
    r"\bpip3?\b.*\b(?:install|sync|download|wheel)\b"
    r"|\b(?:pipx|uvx?|poetry|conda|mamba|tox|nox|hatch|pdm|pre-commit)\b"
)
_ROOT_REQUIREMENTS_ON_STDIN = "grep -v '^\\.$' requirements.txt | "
# The extras a declared requirement may carry: each was resolved by compile.py
# with what it brings, so those packages are in the files. An extra added to a
# requirement brings packages no file names, which a job then installs at the
# index's newest: run compile.py, see them appear in the files, record it here.
EXTRAS_RESOLVED = {"python-dotenv": {"cli"}}
# What stands above ``jobs:``, comments aside. A path or branch filter on the
# pull-request trigger leaves a filtered pull request waiting on every
# required check; a workflow-level ``env`` can constrain or redirect every
# install below it.
ABOVE_THE_JOBS = [
    "name: CI",
    "on:",
    "  push:",
    "    branches: [main, develop]",
    "  pull_request:",
    "concurrency:",
    "  group: ci-${{ github.ref }}",
    "  cancel-in-progress: true",
]
ACTIONS = ["- uses: actions/checkout@v4", "- uses: actions/setup-python@v5"]


def _jobs() -> dict[str, list[str]]:
    """The workflow's jobs, each as the stripped lines under its two-space header.

    A line ending in a backslash is joined to the one after it, so a command
    is one entry however it is wrapped.
    """
    lines = CI.read_text(encoding="utf-8").splitlines()
    jobs: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines[lines.index("jobs:") + 1 :]:
        header = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*", line)
        if header:
            current = jobs.setdefault(header[1], [])
        elif current is not None and current and current[-1].endswith("\\"):
            current[-1] = current[-1][:-1] + line.strip()
        elif current is not None:
            current.append(line.strip())
    return jobs


def _installs(job: list[str]) -> list[str]:
    """The commands of a job that install packages, without a leading ``run:``."""
    return [
        line.removeprefix("run: ")
        for line in job
        if _INSTALLER.search(line) and not line.startswith("#")
    ]


def _sources(line: str) -> tuple[set[str], set[str], set[str]]:
    """What one pip install line installs from: project extras, -r files, requirements by name.

    The root requirements file reaches pip on stdin, less its own "." line
    (the workflow says why); that one pipe is read as the file it carries.
    """
    words = shlex.split(line.replace(MATRIX, "MATRIX").rsplit("|", 1)[-1])
    words = words[words.index("install") + 1 :]
    extras: set[str] = set()
    files: set[str] = set()
    named: set[str] = set()
    after_option = ""
    for word in words:
        if after_option == "-r":
            if word == "/dev/stdin":
                assert line.startswith(_ROOT_REQUIREMENTS_ON_STDIN), line
                word = "requirements.txt"
            files.add(word)
        elif after_option == "-c":
            pass
        elif word.startswith("-"):
            # -rFILE, --requirement=FILE, --group and the like would name a
            # source this reading passes over.
            assert word in ("-e", "-r", "-c"), f"an option this file does not read: {line}"
        elif word == "." or word.startswith(".["):
            extras.update(extra for extra in word[2:-1].split(",") if extra)
        else:
            requirement = Requirement(word)
            assert not requirement.extras, f"an extra brings packages no file names: {line}"
            named.add(drift.normalized(requirement.name))
        after_option = word if word in ("-r", "-c") else ""
    return extras, files, named


def _matrix(job: list[str]) -> tuple[str, ...]:
    (line,) = [line for line in job if line.startswith("python-version: [")]
    return tuple(re.findall(r'"([0-9.]+)"', line))


def _python(job: list[str]) -> str:
    """The Python a job sets up: a version, or the matrix expression as written."""
    (line,) = [line for line in job if line.startswith("python-version: ") and "[" not in line]
    return line.removeprefix("python-version: ").strip('"')


def _flag(job: list[str]) -> str:
    return f"-c .github/constraints/py{_python(job)}.txt"


def _files() -> dict[str, str]:
    return {
        path.name.removeprefix("py").removesuffix(".txt"): path.read_text(encoding="utf-8")
        for path in CONSTRAINTS.glob("py*.txt")
    }


def _requirements(lines: list[str]) -> list[Requirement]:
    """The requirements among the lines of a requirements file, or of a pyproject list."""
    found = []
    for line in lines:
        stripped = line.split(" #", 1)[0].strip()
        if stripped and not stripped.startswith("#") and stripped != ".":
            found.append(Requirement(stripped))
    return found


def _declared() -> list[Requirement]:
    """Every requirement compile.py resolves from: the project's, its extras', each file's."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lines = list(project["dependencies"])
    for extra in compile_files.EXTRAS:
        lines += project["optional-dependencies"][extra]
    for path in compile_files.REQUIREMENTS:
        lines += (ROOT / path).read_text(encoding="utf-8").splitlines()
    return _requirements(lines)


def test_there_is_one_file_for_each_python_of_the_test_matrix_and_no_other():
    matrix = _matrix(_jobs()["test"])
    assert matrix == compile_files.PYTHON_VERSIONS
    assert sorted(_files()) == sorted(matrix)


def test_the_required_jobs_keep_the_names_branch_protection_waits_on():
    jobs = _jobs()
    assert set(jobs) == {*REQUIRED_NAMES, LATEST}
    for job, name in REQUIRED_NAMES.items():
        assert f"name: {name}" in jobs[job], job
    (latest,) = [line for line in jobs[LATEST] if line.startswith("name: ")]
    assert latest.removeprefix("name: ") not in REQUIRED_NAMES.values()


def test_no_job_can_pass_without_running_stop_reporting_or_install_another_way():
    text = CI.read_text(encoding="utf-8")
    lines = text.splitlines()
    above = [line for line in lines[: lines.index("jobs:")] if line and not line.startswith("#")]
    assert above == ABOVE_THE_JOBS
    # -c on a line is what the other tests read; the variable would constrain
    # (or, in the latest job, quietly pin) with no line saying so.
    assert not [line for line in lines if "PIP_CONSTRAINT" in line and "#" not in line]
    for name, job in _jobs().items():
        # The files are resolved for Linux: another runner installs packages they do not name.
        assert "runs-on: ubuntu-latest" in job, name
        # An action can install too; these two do not.
        assert [line for line in job if line.startswith("- uses:")] == ACTIONS, name
        # A job allowed to fail reports green whatever it did, and GitHub
        # reports a job its own ``if:`` skipped as passed.
        assert not [line for line in job if line.startswith("continue-on-error")], name
        conditions = [line for line in job if line.startswith("if:")]
        assert conditions == (["if: always()"] if name == LATEST else []), name


def test_every_install_line_of_a_required_job_names_its_python_s_constraints_file():
    unconstrained = set()
    for name, job in _jobs().items():
        if name == LATEST:
            continue
        for line in _installs(job):
            if "-c " in line:
                assert line.endswith(_flag(job)), f"{name}: {line}"
            else:
                unconstrained.add(line)
    assert unconstrained == set(UNCONSTRAINED)


def test_a_job_that_installs_nothing_from_the_index_would_not_pass_as_constrained():
    for name, job in _jobs().items():
        assert _installs(job), f"{name} installs nothing: this file would read it as constrained"


def test_the_latest_job_installs_without_a_constraints_file_and_lists_what_moved():
    jobs = _jobs()
    latest = jobs[LATEST]
    assert _installs(latest) == ["python -m pip install --upgrade pip", 'pip install -e ".[dev]"']
    assert set(_matrix(latest)) < set(_matrix(jobs["test"]))
    # The list comes after the suite and whatever the suite did: a fault in
    # the report then cannot keep the suite from running.
    report = f"run: python .github/constraints/drift.py .github/constraints/py{_python(latest)}.txt"
    assert latest.index("run: pytest -q") < latest.index("if: always()") < latest.index(report)


@pytest.mark.parametrize("version", compile_files.PYTHON_VERSIONS)
def test_a_file_is_what_compile_writes_and_holds_only_pins(version):
    text = _files()[version]
    header = text.splitlines()[1]
    assert header == "#    " + " ".join(compile_files.command(version))
    pinned = drift.pins(text)
    assert len(pinned) > 100
    assert compile_files.PROJECT not in pinned


def _runner(version: str) -> dict[str, str]:
    """The marker environment the files are resolved for, not the one running this suite."""
    return {
        "python_version": version,
        "python_full_version": f"{version}.0",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
    }


def test_the_project_is_named_as_the_two_scripts_name_it():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == compile_files.PROJECT == drift.PROJECT


def test_every_declared_requirement_is_pinned_in_every_file_inside_its_range():
    declared = _declared()
    names = {drift.normalized(requirement.name) for requirement in declared}
    assert {"langchain-openai", "ruff", "mcp", "web3", "hyperliquid-python-sdk", "httpx"} <= names
    for version, text in _files().items():
        pinned = drift.pins(text)
        for requirement in declared:
            if requirement.marker and not requirement.marker.evaluate(_runner(version)):
                continue
            name = drift.normalized(requirement.name)
            again = f"py{version}.txt: run compile.py"
            assert requirement.extras <= EXTRAS_RESOLVED.get(name, set()), (
                f"{name} is declared with {sorted(requirement.extras)}: run compile.py, see "
                "what the extra brings appear in the files, then record it in EXTRAS_RESOLVED"
            )
            assert name in pinned, f"no pin for {name} in {again}"
            assert requirement.specifier.contains(pinned[name], prereleases=True), (
                f"{name}=={pinned[name]} is outside the declared {requirement.specifier} in {again}"
            )


def test_a_required_job_installs_only_from_what_compile_resolved_and_from_all_of_it():
    ci_in = compile_files.REQUIREMENTS[-1]
    assert ci_in == ".github/constraints/ci.in"
    pinned = {version: set(drift.pins(text)) for version, text in _files().items()}
    seen_extras: set[str] = set()
    seen_files: set[str] = set()
    seen_named: set[str] = set()
    for job_name, job in _jobs().items():
        if job_name == LATEST:
            continue
        versions = pinned if _python(job) == MATRIX else {_python(job): pinned[_python(job)]}
        for line in _installs(job):
            if line in UNCONSTRAINED:
                continue
            extras, files, named = _sources(line)
            # -c does nothing for a package the file does not name: an extra
            # or a requirements file compile.py never read would float.
            assert extras <= set(compile_files.EXTRAS), f"{job_name}: {line}"
            assert files <= set(compile_files.REQUIREMENTS), f"{job_name}: {line}"
            for version, names in versions.items():
                assert named <= names, f"{job_name}: py{version}.txt pins no {named - names}"
            seen_extras |= extras
            seen_files |= files
            seen_named |= named
    assert seen_extras == set(compile_files.EXTRAS)
    assert seen_files == set(compile_files.REQUIREMENTS) - {ci_in}
    in_ci_in = _requirements((ROOT / ci_in).read_text(encoding="utf-8").splitlines())
    for requirement in in_ci_in:
        name = drift.normalized(requirement.name)
        assert name in seen_named, f"ci.in names {name}, which no step installs by name"


@pytest.mark.parametrize(
    ("argv", "moved"),
    [
        # No option: uv reads each existing file back and keeps what still fits.
        ([], []),
        (["--upgrade"], ["--upgrade"]),
        (
            ["--upgrade-package", "ruff", "--upgrade-package", "httpx"],
            ["--upgrade-package", "ruff", "--upgrade-package", "httpx"],
        ),
    ],
)
def test_compile_keeps_what_the_files_hold_unless_asked_to_move_it(monkeypatch, argv, moved):
    calls = []

    def run(command, *, cwd, check, stdout):
        calls.append((command, cwd, check, stdout))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(compile_files.subprocess, "run", run)
    assert compile_files.main(argv) == 0
    # stdout dropped and nothing asked to be quiet: uv's warnings come on stderr.
    assert calls == [
        ([*compile_files.command(version), *moved], ROOT, False, subprocess.DEVNULL)
        for version in compile_files.PYTHON_VERSIONS
    ]


def test_compile_refuses_everything_and_one_package_asked_for_together(capsys):
    with pytest.raises(SystemExit) as stopped:
        compile_files.main(["--upgrade", "--upgrade-package", "ruff"])
    assert stopped.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_compile_stops_at_a_refusal_and_says_which_files_it_had_rewritten(monkeypatch, capsys):
    calls = []

    def run(command, *, cwd, check, stdout):
        calls.append(command)
        return subprocess.CompletedProcess(command, 3 if len(calls) == 2 else 0)

    monkeypatch.setattr(compile_files.subprocess, "run", run)
    assert compile_files.main([]) == 3
    first, second = compile_files.PYTHON_VERSIONS[:2]
    assert calls == [compile_files.command(first), compile_files.command(second)]
    # Which files of the set a stopped run has already moved is said, not left to find.
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"uv refused Python {second} (.github/constraints/py{first}.txt already rewritten); "
        "the files after it were not written. Put the directory back "
        "(git restore .github/constraints/py*.txt) or run this again"
    )


def test_compile_without_uv_says_so_and_writes_nothing(monkeypatch, capsys):
    def run(command, *, cwd, check, stdout):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(compile_files.subprocess, "run", run)
    assert compile_files.main([]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        "uv is not on PATH (pip install uv); nothing was written"
    )


def test_pins_reads_names_as_an_index_compares_them_and_passes_comments():
    text = "# header\n\nTyping_Extensions==4.14.0   # via a, b\nzope.interface==7.2\n"
    assert drift.pins(text) == {"typing-extensions": "4.14.0", "zope-interface": "7.2"}


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (".\n", "line 1 is not a name==version pin: '.'"),
        ("a==1\nb>=2\n", "line 2 is not a name==version pin: 'b>=2'"),
        ("a==1 ; python_version < '3.11'\n", "line 1 is not a name==version pin"),
        ("-e .\n", "line 1 is not a name==version pin: '-e .'"),
        ("a_b==1\nA.B==2\n", "line 2 pins a-b a second time"),
    ],
)
def test_pins_refuses_a_line_that_is_no_pin_and_a_name_pinned_twice(text, said):
    with pytest.raises(ValueError, match=re.escape(said)):
        drift.pins(text)


def test_drift_lists_what_moved_and_what_is_not_pinned_and_passes_the_rest():
    pinned = {"moved": "1.0", "same": "2.0", "only-another-job-installs": "3.0"}
    installed = {
        "same": "2.0",
        "moved": "1.1",
        "new-dependency": "0.1",
        "pip": "26.0",
        "wheel": "0.45",
        "tradingagents": "0.4.0",
    }
    assert drift.drift(pinned, installed, project="tradingagents") == [
        "moved: installed 1.1, pinned 1.0",
        "new-dependency: installed 0.1, not pinned",
    ]


def test_one_release_spelled_two_ways_is_not_listed_as_moved():
    # A file holds the normalized spelling; metadata holds what the build wrote.
    pinned = {"dated": "2026.1.5", "post": "1.0.post1", "odd": "not-a-version"}
    installed = {"dated": "2026.01.05", "post": "1.0-1", "odd": "not-a-version"}
    assert drift.drift(pinned, installed, project="tradingagents") == []
    assert drift.drift({"odd": "not-a-version"}, {"odd": "other"}, project="tradingagents") == [
        "odd: installed other, pinned not-a-version"
    ]


def test_a_metadata_directory_with_no_name_or_no_version_is_passed(monkeypatch):
    class Found:
        def __init__(self, **metadata):
            self.metadata = metadata

    found = [Found(), Found(Name="half-removed"), Found(Name="Some_Package", Version="1.0")]
    monkeypatch.setattr(drift.metadata, "distributions", lambda: found)
    assert drift.installed_versions() == {"some-package": "1.0"}


def test_the_report_has_nothing_to_say_where_the_project_is_not_installed(
    tmp_path, monkeypatch, capsys
):
    # What a failed install step leaves: the job's suite never ran, this step
    # still does, and "every package is at its pin" would be read off it.
    path = tmp_path / "constraints.txt"
    path.write_text("a==1\n", encoding="utf-8")
    for left in ({}, {"pip": "26.0", "setuptools": "80.0"}):
        monkeypatch.setattr(drift, "installed_versions", lambda left=left: left)
        assert drift.main([str(path)]) == 2
        assert capsys.readouterr().out == (
            f"tradingagents is not installed here, so there is nothing to compare with {path}\n"
        )


def test_the_report_exits_zero_whatever_it_lists_and_two_on_a_file_it_cannot_read(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        drift, "installed_versions", lambda: {"a": "2", "b": "1", "tradingagents": "0.4.0"}
    )
    path = tmp_path / "constraints.txt"

    path.write_text("a==1\nb==1\n", encoding="utf-8")
    assert drift.main([str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"1 installed package(s) differ from {path}:",
        "  a: installed 2, pinned 1",
    ]

    path.write_text("a==2\nb==1\n", encoding="utf-8")
    assert drift.main([str(path)]) == 0
    assert capsys.readouterr().out == f"every installed package is at the version {path} pins\n"

    path.write_text("a==2\nb\n", encoding="utf-8")
    assert drift.main([str(path)]) == 2
    assert "cannot be read as a constraints file: line 2" in capsys.readouterr().out

    assert drift.main([str(tmp_path / "absent.txt")]) == 2
    assert "cannot be read as a constraints file" in capsys.readouterr().out
    assert drift.main([]) == 2


def test_the_interpreter_running_this_suite_can_be_read_for_the_report():
    installed = drift.installed_versions()
    assert "pytest" in installed
    assert all(name == drift.normalized(name) for name in installed)
