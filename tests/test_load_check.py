"""The load and restart check, run end to end against local processes standing in for the sandbox."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import textwrap
import time
from uuid import uuid4

import pytest

from orchestra.sandbox.nemoclaw import process_probe

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="reads Linux /proc")

_spec = importlib.util.spec_from_file_location("nemoclaw_load_check", ROOT / "scripts" / "nemoclaw_load_check.py")
assert _spec is not None and _spec.loader is not None
load = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load)

# A protocol peer. "stays" ignores the end of its input, like a worker stuck inside the sandbox.
WORKER = textwrap.dedent('''
    import json, sys, time
    print(json.dumps({"orchestra_worker": 1, "ready": True}), flush=True)
    for line in sys.stdin:
        reply = json.dumps({"ok": False, "error": "worker_tool_forbidden"})
        print(json.dumps({"id": json.loads(line)["id"], "response": reply}), flush=True)
    while sys.argv[2] == "stays":
        time.sleep(1)
''')
# Stands between host and worker like ssh: killing its process group does not reach the worker.
RELAY = textwrap.dedent('''
    import subprocess, sys
    child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2][1:], "stays"], start_new_session=True)
    child.wait()
''')


def running(marker: str) -> list[int]:
    found = [int(name) for name in os.listdir("/proc") if name.isdigit()
             and process_probe.is_worker(process_probe.command_line(int(name)), marker)]
    return [pid for pid in found if process_probe.memory_kb(pid) is not None]


def gone(marker: str) -> bool:
    deadline = time.monotonic() + 5
    while running(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not running(marker)


def check_for(tmp_path: Path, mode: str, said: list[str], inspect=None):
    marker = f"orchestra-load-test-{uuid4().hex}"
    worker, relay = tmp_path / "worker.py", tmp_path / "relay.py"
    worker.write_text(WORKER, encoding="utf-8")
    relay.write_text(RELAY, encoding="utf-8")

    def launch(sandbox: str) -> list[str]:
        if mode == "relay":
            return [sys.executable, str(relay), str(worker), "=" + marker]
        return [sys.executable, str(worker), marker, mode]

    probe = inspect or (lambda config: [sys.executable, "-I", "-", config])
    return load.Check("test-box", launch, probe, marker=marker, say=said.append, gone_wait_s=3), marker


def test_a_healthy_worker_passes_every_measurement(tmp_path):
    said: list[str] = []
    check, marker = check_for(tmp_path, "healthy", said)
    assert check.run(requests=12, callers=4, workers=3) == "PASS", said
    heads = [line.split()[0] for line in said]
    assert heads == ["BASELINE", "STARTUP", "SEQUENTIAL", "CONCURRENT", "ONE_WORKER", "AFTER_WORKER_CRASH",
                     "AFTER_CONNECTION_LOSS", "MANY_WORKERS", "CLEANUP"]
    assert "errors=0" in said[2] and "errors=0" in said[3]
    assert "processes=1" in said[4] and "new_worker=yes" in said[5] and "leftover=0" in said[6]
    assert "running=3" in said[7]
    assert gone(marker)


def test_a_worker_that_outlives_its_connection_fails_and_is_removed(tmp_path):
    said: list[str] = []
    check, marker = check_for(tmp_path, "relay", said)
    try:
        assert check.run(requests=3, callers=2, workers=2) == "FAIL", said
        assert any(line.startswith("AFTER_CONNECTION_LOSS leftover=") and line.endswith("FAIL") for line in said)
        assert said[-1].startswith("removed ")
        assert gone(marker)
    finally:
        for pid in running(marker):
            os.kill(pid, 9)


def test_workers_that_were_already_there_are_neither_counted_nor_killed(tmp_path):
    import subprocess

    said: list[str] = []
    check, marker = check_for(tmp_path, "healthy", said)
    other = subprocess.Popen(check.launch("test-box"), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert other.stdout is not None and other.stdout.readline()
        assert check.run(requests=3, callers=2, workers=2) == "PASS", said
        assert said[0].startswith("BASELINE workers=1 ")
        assert other.poll() is None
    finally:
        other.kill()
        other.communicate(timeout=5)


@pytest.mark.parametrize("program,code", [("print('hello')", "probe_output_unrecognized"),
                                          ("raise SystemExit(3)", "probe_failed")])
def test_an_unreadable_sandbox_is_incomplete(tmp_path, program, code):
    said: list[str] = []
    check, marker = check_for(tmp_path, "healthy", said,
                              inspect=lambda config: [sys.executable, "-I", "-c", program])
    assert check.run(requests=1, callers=1, workers=1) == "INCOMPLETE"
    assert said == [f"INSPECT {code} UNKNOWN"]
    assert gone(marker)


def test_a_refused_kill_is_reported_as_not_shown(tmp_path):
    said: list[str] = []
    check, _ = check_for(tmp_path, "healthy", said)
    check.snapshot = lambda kill=None: load.Snapshot("KILL 41 EPERM\nALL 3 100")
    check.crash(None, [41])
    assert said == ["AFTER_WORKER_CRASH kill_refused=EPERM UNKNOWN"]


def test_only_the_expected_probe_lines_are_read():
    snapshot = load.Snapshot("ALL 2 3000\nWORKER 7 1000 1500\nWORKER 8 x 1\nsecret=value\nKILL 7 OK\nKILL 9 $(id)")
    assert (snapshot.processes, snapshot.rss_kb) == (2, 3000)
    assert snapshot.workers == {7: (1000, 1500)} and snapshot.kills == {7: "OK"}


def test_the_probe_signals_only_workers():
    mine = os.getpid()
    lines = process_probe.report("orchestra-no-such-marker", [mine])
    assert lines[0] == f"KILL {mine} ESRCH"
    assert lines[1].startswith("ALL ") and len(lines) == 2
    assert process_probe.is_worker(["/opt/venv/bin/python", "-m", "pkg.worker", "--serve"], "pkg.worker")
    assert not process_probe.is_worker(["/bin/sh", "-c", "pkg.worker"], "pkg.worker")
    assert not process_probe.is_worker(["/usr/bin/python3", "-c", "run pkg.worker"], "pkg.worker")
    assert not process_probe.is_worker([], "pkg.worker")


def test_counts_are_bounded():
    parse = load.bounded(1, 16)
    assert parse("16") == 16
    for value in ("0", "17", "-1", "many"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse(value)
