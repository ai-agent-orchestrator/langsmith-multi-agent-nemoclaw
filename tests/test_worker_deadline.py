"""A tool that overruns its limit is stopped inside the worker; a worker that cannot stop it exits."""
from __future__ import annotations

import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from langchain_core.tools import tool

from orchestra.filters.chain import FilterChain
from orchestra.sandbox.base import ExecRequest, ExecResult
from orchestra.sandbox.nemoclaw.cli import NemoClawCli
from orchestra.sandbox.nemoclaw.runner import REPLY_MARGIN_S, NemoClawRunner
from orchestra.sandbox.nemoclaw.worker import HARD_EXIT_GRACE_S, Worker
from orchestra.security.capability import CapabilityAuthority
from orchestra.specs import SideEffect, ToolSpec

pytestmark = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="the worker only runs on POSIX")

ROOT = Path(__file__).resolve().parents[1]
BOX = "test-box"
KEY = b"k" * 32
GRANTS = {"reader": {"slow"}}
LIMIT = 0.3
TIMED_OUT = ExecResult(ok=False, error="worker_timeout").model_dump()


def make_worker(tmp_path: Path, function, *, side_effect: SideEffect = SideEffect.READ,
                timeout_s: float = LIMIT) -> Worker:
    spec = ToolSpec(name="slow", side_effect=side_effect, sandbox_name=BOX, timeout_s=timeout_s)
    return Worker(BOX, {"slow": (tool("slow", description="test tool")(function), spec)}, FilterChain([]),
                  journal=tmp_path / "journal.sqlite", grants=GRANTS,
                  authority=CapabilityAuthority(KEY, GRANTS))


def ask(worker: Worker, mode: str = "stall", **fields) -> dict:
    request = ExecRequest(sandbox=BOX, tool="slow", args={"mode": mode}, agent="reader", **fields)
    request.capability_token = CapabilityAuthority(KEY, GRANTS).issue(request.model_dump())
    return json.loads(worker.handle(request.model_dump_json()))


@pytest.mark.parametrize("stall", ["sleep", "spin", "swallow"])
def test_an_overrunning_tool_is_stopped_and_the_worker_stays_usable(tmp_path, stall):
    reached: list[str] = []

    def slow(mode: str) -> str:
        if mode == "quick":
            return "done"
        end = time.monotonic() + 30
        if stall == "sleep":
            time.sleep(30)
        elif stall == "spin":
            while time.monotonic() < end:
                pass
        else:
            try:
                time.sleep(30)
            except Exception:  # a tool's own error handling must not swallow the stop
                reached.append("swallowed")
        reached.append("finished")
        return "late"

    worker = make_worker(tmp_path, slow)
    started = time.monotonic()
    assert ask(worker) == TIMED_OUT
    assert time.monotonic() - started < 5
    assert reached == []
    assert ask(worker, "quick")["output"] == "done"


def test_the_shorter_of_request_and_tool_limit_applies(tmp_path):
    def slow(mode: str) -> str:
        time.sleep(30)
        return "late"

    worker = make_worker(tmp_path, slow, timeout_s=30.0)
    started = time.monotonic()
    assert ask(worker, timeout_s=LIMIT)["error"] == "worker_timeout"
    assert time.monotonic() - started < 5


def test_an_interrupted_side_effect_is_not_run_again(tmp_path):
    runs: list[str] = []

    def slow(mode: str) -> str:
        runs.append(mode)
        time.sleep(30)
        return "late"

    worker = make_worker(tmp_path, slow, side_effect=SideEffect.RESERVE)
    first = ask(worker, idempotency_key="one")
    second = ask(worker, idempotency_key="one")
    assert first["error"] == second["error"] == "worker_timeout"
    assert runs == ["stall"]


def test_the_alarm_handler_and_timer_are_restored(tmp_path):
    def slow(mode: str) -> str:
        if mode == "stall":
            time.sleep(30)
        return "done"

    marker = lambda *_: None  # noqa: E731
    previous = signal.signal(signal.SIGALRM, marker)
    try:
        worker = make_worker(tmp_path, slow)
        for mode in ("quick", "stall"):
            ask(worker, mode)
            assert signal.getsignal(signal.SIGALRM) is marker
            assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_a_timer_someone_else_armed_is_left_alone(tmp_path):
    def slow(mode: str) -> str:
        time.sleep(0.4)
        return "late"

    previous = signal.signal(signal.SIGALRM, lambda *_: None)
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        assert ask(make_worker(tmp_path, slow))["error"] == "worker_timeout"
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 30
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_off_the_main_thread_a_late_result_is_still_refused(tmp_path):
    def slow(mode: str) -> str:
        time.sleep(0.4)
        return "late"

    worker = make_worker(tmp_path, slow)
    replies: list[dict] = []
    thread = threading.Thread(target=lambda: replies.append(ask(worker)))
    thread.start()
    thread.join(10)
    assert replies[0]["error"] == "worker_timeout"


# A tool no Python-level interrupt can stop: it blocks inside C or refuses every exception.
UNSTOPPABLE = textwrap.dedent('''
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
    from test_worker_deadline import make_worker
    from orchestra.sandbox.nemoclaw.worker import serve, take_protocol_streams
    from orchestra.specs import SideEffect

    def slow(mode: str) -> str:
        if mode == "c-loop":
            sum(range(10 ** 12))
        while True:
            try:
                time.sleep(30)
            except BaseException:
                pass

    source, sink = take_protocol_streams()
    worker = make_worker(Path(sys.argv[2]), slow, side_effect=SideEffect.RESERVE)
    worker.hard_exit_after_s = 0.5
    raise SystemExit(serve(worker, source, sink))
''')


@pytest.mark.parametrize("mode", ["c-loop", "refuse"])
def test_a_worker_that_cannot_stop_a_tool_exits_and_reports_indeterminate_later(tmp_path, mode):
    script = tmp_path / "unstoppable.py"
    script.write_text(UNSTOPPABLE, encoding="utf-8")
    request = ExecRequest(sandbox=BOX, tool="slow", args={"mode": mode}, agent="reader", idempotency_key="one")
    request.capability_token = CapabilityAuthority(KEY, GRANTS).issue(request.model_dump())
    line = json.dumps({"id": "1", "request": request.model_dump_json()}).encode() + b"\n"
    process = subprocess.Popen([sys.executable, str(script), str(ROOT), str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdin is not None and process.stdout is not None
        assert json.loads(process.stdout.readline())["ready"] is True
        started = time.monotonic()
        process.stdin.write(line)
        process.stdin.flush()
        assert process.wait(timeout=20) != 0  # stdin stays open: only the deadline can end it
        assert time.monotonic() - started < 10
        assert process.stdout.read() == b""
    finally:
        process.kill()
        process.wait(timeout=5)

    def never(mode: str) -> str:
        raise AssertionError("must not run again")

    again = ask(make_worker(tmp_path, never, side_effect=SideEffect.RESERVE), mode, idempotency_key="one")
    assert again["error"] == "worker_execution_indeterminate"


def test_the_sandbox_entry_point_enables_the_hard_exit(tmp_path, monkeypatch):
    from orchestra.sandbox.nemoclaw import worker as module

    seen: list[float | None] = []

    def capture(self: Worker, payload: str) -> str:
        seen.append(self.hard_exit_after_s)
        return "{}"

    key = tmp_path / "key"
    key.write_bytes(KEY)
    monkeypatch.setattr(Worker, "handle", capture)
    monkeypatch.setenv("ORCHESTRA_WORKER_SANDBOX", "orchestra-code")
    monkeypatch.setenv("ORCHESTRA_WORKER_ORG", str(ROOT / "orgs" / "claude_dev.yaml"))
    monkeypatch.setenv("ORCHESTRA_WORKER_JOURNAL", str(tmp_path / "journal.sqlite"))
    monkeypatch.setenv("ORCHESTRA_WORKER_CAPABILITY_KEY_FILE", str(key))
    monkeypatch.setattr(module.sys, "stdin", type("In", (), {"buffer": io.BytesIO(b"{}")})())
    assert module.main([]) == 0
    assert seen == [HARD_EXIT_GRACE_S]


def test_the_host_waits_longer_than_the_worker_so_the_timeout_reply_arrives():
    waits: list[float] = []

    class Recorder:
        def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str:
            waits.append(timeout_s)
            return json.dumps(TIMED_OUT)

    runner = NemoClawRunner(NemoClawCli(openshell="openshell", dialect="modern"), {BOX: {}}, transport=Recorder())
    runner.status = lambda sandbox: {"ok": True, "policy_revision": "r"}  # type: ignore[method-assign]
    result = runner.exec(ExecRequest(sandbox=BOX, tool="slow", timeout_s=7.0))
    assert result.error == "worker_timeout"
    assert waits == [7.0 + REPLY_MARGIN_S]
    assert REPLY_MARGIN_S > HARD_EXIT_GRACE_S
