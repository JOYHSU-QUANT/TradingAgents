"""``python -m contrib.dashboard.app``: the refusals need no web library; a start binds a fake server."""

from __future__ import annotations

import pytest

from contrib.dashboard.app.cli import DEFAULT_PORT, main

from .support import need_web


def test_a_host_that_is_not_loopback_is_refused_before_anything_is_imported(tmp_path, capsys):
    for host in ("0.0.0.0", "10.0.0.5", "::", "100.64.0.1"):
        assert main(["--status-dir", str(tmp_path), "--host", host]) == 1
        assert "not a loopback address" in capsys.readouterr().err
    assert main(["--status-dir", str(tmp_path), "--host", "tokyo"]) == 1
    assert "not an IP address" in capsys.readouterr().err


def test_a_status_dir_that_is_not_a_directory_is_refused(tmp_path, capsys):
    assert main(["--status-dir", str(tmp_path / "none")]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_a_bad_port_or_threshold_is_usage(tmp_path):
    bad = (
        ["--port", "0"],
        ["--port", "70000"],
        ["--stale-minutes", "0"],
        ["--refresh-seconds", "-5"],
    )
    for argv in bad:
        with pytest.raises(SystemExit) as raised:
            main(["--status-dir", str(tmp_path), *argv])
        assert raised.value.code == 2


def test_the_server_is_started_with_the_settings_given(tmp_path):
    need_web()
    started = []
    code = main(
        ["--status-dir", str(tmp_path), "--port", "9001", "--stale-minutes", "4", "--title", "t"],
        serve=lambda app, host, port: started.append((app, host, port)),
    )
    assert code == 0
    ((app, host, port),) = started
    assert (host, port) == ("127.0.0.1", 9001)
    assert app.title == "t"


def test_the_defaults_are_the_documented_ones(tmp_path):
    need_web()
    started = []
    main(
        ["--status-dir", str(tmp_path)], serve=lambda app, host, port: started.append((host, port))
    )
    assert started == [("127.0.0.1", DEFAULT_PORT)]
