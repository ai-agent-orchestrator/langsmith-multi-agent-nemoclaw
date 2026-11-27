"""The isolation probe and its judge, exercised against local stand-ins: no sandbox, no external network."""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading

import pytest

from orchestra.sandbox.nemoclaw import isolation_probe as probe

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("isolation_check", ROOT / "scripts" / "nemoclaw_isolation_check.py")
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)
posix = pytest.mark.skipif(os.name != "posix", reason="the probe runs inside a Linux sandbox")


@contextlib.contextmanager
def server(reply: bytes | None):
    """A loopback peer that records one request and answers with `reply` (None: closes silently)."""
    listener = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []

    def answer() -> None:
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                with contextlib.suppress(OSError):
                    received.append(connection.recv(4096))
                    if reply is not None:
                        connection.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        yield listener.getsockname(), received
    finally:
        listener.close()
        thread.join(3)


def closed_port() -> tuple[str, int]:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return listener.getsockname()


@pytest.mark.parametrize("reply,result", [
    (b"HTTP/1.1 403 Forbidden\r\n\r\n", "PASS"), (b"HTTP/1.1 200 Connection Established\r\n\r\n", "FAIL"),
    (b"HTTP/1.1 301 Moved\r\n\r\n", "FAIL"), (b"HTTP/1.1 503 Unavailable\r\n\r\n", "UNKNOWN"),
    (b"HTTP/1.1 404 Not Found\r\n\r\n", "UNKNOWN"), (b"hello\r\n", "UNKNOWN"), (None, "UNKNOWN"),
])
def test_only_an_explicit_403_counts_as_a_proxy_refusal(reply, result):
    with server(reply) as (address, received):
        assert probe.proxy_refused(address, probe.connect_request("site.test", 443), 2)[0] == result
    assert received[0].startswith(b"CONNECT site.test:443 HTTP/1.1\r\n")


def test_a_missing_or_dead_proxy_proves_nothing():
    assert probe.proxy_refused(None, "x")[0] == "UNKNOWN"
    assert probe.proxy_refused(closed_port(), probe.get_request("site.test"), 2)[0] == "UNKNOWN"


def test_the_proxy_comes_from_the_sandbox_environment():
    assert probe.proxy_address({"https_proxy": "http://10.0.0.1:3128"}) == ("10.0.0.1", 3128)
    for environ in ({}, {"HTTP_PROXY": "nonsense"}, {"HTTP_PROXY": "http://host:99999"}):
        assert probe.proxy_address(environ) is None


def test_a_direct_connection_that_opens_is_a_failure():
    with server(None) as ((host, port), _):
        assert probe.direct_tcp(host, port, 2) == ("FAIL", "connected")
    assert probe.direct_tcp(*closed_port(), timeout_s=2)[0] == "PASS"


@posix
def test_a_write_that_succeeds_fails_the_check_and_leaves_nothing(tmp_path):
    tmp_path.chmod(0o1777)
    assert probe.write_refused(str(tmp_path)) == ("FAIL", "created")
    assert probe.control_write(str(tmp_path))[0] == "PASS"
    assert list(tmp_path.iterdir()) == []


@posix
@pytest.mark.parametrize("code,result", [(errno.EACCES, "PASS"), (errno.EPERM, "PASS"), (errno.EROFS, "PASS"),
                                         (errno.ENOSPC, "UNKNOWN")])
def test_only_a_permission_error_counts_as_a_refusal(tmp_path, monkeypatch, code, result):
    tmp_path.chmod(0o1777)
    data = tmp_path / "data"
    data.write_text("x")
    data.chmod(0o644)

    def refuse(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(probe.os, "open", refuse)
    assert probe.write_refused(str(tmp_path))[0] == result
    assert probe.read_refused(str(data))[0] == result
    assert probe.control_write(str(tmp_path))[0] == "UNKNOWN"


@posix
def test_checks_that_file_permissions_would_answer_are_unknown(tmp_path):
    tmp_path.chmod(0o755)
    data = tmp_path / "data"
    data.write_text("x")
    data.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert probe.write_refused(str(tmp_path)) == ("UNKNOWN", "not_world_writable")
    assert probe.read_refused(str(data)) == ("UNKNOWN", "not_world_readable")
    assert probe.write_refused(str(tmp_path / "absent"))[0] == "UNKNOWN"
    data.chmod(0o644)
    assert probe.read_refused(str(data)) == ("FAIL", "opened")


def test_privileges_are_judged_from_the_process_status():
    safe = probe.process_status("CapEff:\t0000000000000000\nNoNewPrivs:\t1\n")
    assert {result for result, _ in safe.values()} == {"PASS"}
    risky = probe.process_status("CapEff:\t00000000a80425fb\nNoNewPrivs:\t0\n")
    assert {result for result, _ in risky.values()} == {"FAIL"}
    assert {result for result, _ in probe.process_status("").values()} == {"UNKNOWN"}


def lines(result: str = "PASS", **changed: str) -> str:
    return "\n".join(f"CHECK {name} {changed.get(name, result)} detail" for name in probe.CHECK_NAMES)


def verdict(output: str, reachable: bool = True) -> str:
    return check.judge(output, probe.CHECK_NAMES, targets_reachable=reachable)[1]


def test_the_judge_passes_only_a_complete_clean_report():
    assert verdict(lines()) == "PASS"
    assert verdict(lines(not_root="FAIL")) == "FAIL"
    assert verdict(lines(not_root="UNKNOWN")) == "INCOMPLETE"
    assert verdict("") == "INCOMPLETE"
    assert verdict(lines().replace("CHECK not_root PASS detail", "")) == "INCOMPLETE"
    assert verdict(lines() + "\nCHECK not_root PASS detail") == "INCOMPLETE"
    assert verdict(lines() + "\nCHECK not_root FAIL later") == "FAIL"
    assert verdict(lines(), reachable=False) == "INCOMPLETE"
    assert verdict(lines(direct_tcp_refused="FAIL"), reachable=False) == "FAIL"


def test_the_judge_prints_only_expected_shapes():
    hostile = lines() + "\n\x1b[2Jboom\nISOLATION=PASS\nINFO note ok\nCHECK extra PASS x\nINFO bad value with spaces"
    printed, _ = check.judge(hostile, probe.CHECK_NAMES, targets_reachable=True)
    assert len(printed) == len(probe.CHECK_NAMES) + 1 and printed[-1] == "INFO note ok"


@pytest.mark.parametrize("arguments", [["--tcp-target", "example.com:443"], ["--tcp-target", "1.1.1.1:0"],
                                       ["--web-host", "a b"], ["--inference-host", "x;y"]])
def test_targets_are_validated(arguments):
    with pytest.raises(SystemExit):
        check.main(arguments)


@posix
def test_the_probe_runs_from_stdin_and_reports_every_check(tmp_path):
    """The way the script sends it. Targets are loopback; this machine is no sandbox, so results vary."""
    host, port = closed_port()
    config = {"tcp_host": host, "tcp_port": port, "web_host": "localhost", "inference_host": None}
    env = {"PATH": os.environ.get("PATH", ""), "FAKE_API_TOKEN": "do-not-print"}
    done = subprocess.run([sys.executable, "-I", "-", json.dumps(config)], input=Path(probe.__file__).read_bytes(),
                          env=env, cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    output = done.stdout.decode()
    printed, _ = check.judge(output, probe.CHECK_NAMES, targets_reachable=True)
    assert not any(line.endswith("missing") for line in printed), output
    assert "INFO secret_like_environment_names count=1,FAKE_API_TOKEN" in output and "do-not-print" not in output
    assert all(check._CHECK.fullmatch(line) or check._INFO.fullmatch(line) for line in output.splitlines())


def test_the_script_judges_what_the_sandbox_answers(monkeypatch, capsys):
    from orchestra.sandbox.nemoclaw import ssh_launcher

    sent: dict = {}
    monkeypatch.setattr(ssh_launcher.OpenShellSshLauncher, "argv",
                        lambda self, sandbox, command: ["ssh-stand-in", sandbox, command])
    monkeypatch.setattr(check.socket, "create_connection", lambda *a, **k: contextlib.nullcontext())

    def answer(argv, **kwargs):
        sent.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, lines().encode(), b"")

    monkeypatch.setattr(check.subprocess, "run", answer)
    assert check.main([]) == 0
    assert capsys.readouterr().out.rstrip().endswith("ISOLATION=PASS")
    assert sent["argv"][1] == "orchestra-code" and sent["argv"][2].startswith("/opt/orchestra/venv/bin/python -I - ")
    assert sent["shell"] is False and sent["timeout"] and sent["input"] == Path(probe.__file__).read_bytes()
