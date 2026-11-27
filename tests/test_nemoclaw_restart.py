"""A worker that has to be replaced: stuck filters are bounded, and the journal survives the restart."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from langchain_core.tools import tool

from orchestra.filters.base import FilterContext, FilterDecision
from orchestra.filters.chain import FilterChain
from orchestra.sandbox.base import ExecRequest
from orchestra.sandbox.nemoclaw import worker as worker_module
from orchestra.sandbox.nemoclaw.persistent import PersistentTransport
from orchestra.sandbox.nemoclaw.runner import REPLY_MARGIN_S
from orchestra.sandbox.nemoclaw.worker import HARD_EXIT_GRACE_S, PRE_TOOL_LIMIT_S, Worker
from orchestra.security.capability import CapabilityAuthority
from orchestra.specs import FilterSpec, SideEffect, ToolSpec

pytestmark = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="the worker only runs on POSIX")

ROOT = Path(__file__).resolve().parents[1]
BOX = "test-box"
KEY = b"k" * 32
GRANTS = {"reader": {"note"}}


def signed(key: str = "one") -> ExecRequest:
    request = ExecRequest(sandbox=BOX, tool="note", args={"text": "hello"}, agent="reader", idempotency_key=key)
    request.capability_token = CapabilityAuthority(KEY, GRANTS).issue(request.model_dump())
    return request


def make_worker(tmp_path: Path, function, check) -> Worker:
    """One RESERVE tool behind one pre-tool filter that runs `check`."""
    class Gate:
        name = "gate"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            check()
            return FilterDecision.allow()

    spec = ToolSpec(name="note", side_effect=SideEffect.RESERVE, sandbox_name=BOX)
    gate = FilterSpec(name="gate", stages=["pre_tool"], applies_to=["*"])
    return Worker(BOX, {"note": (tool("note", description="test tool")(function), spec)},
                  FilterChain([(gate, Gate())]), journal=tmp_path / "journal.sqlite", grants=GRANTS,
                  authority=CapabilityAuthority(KEY, GRANTS))


def test_the_pre_tool_limit_fits_inside_the_host_reply_margin():
    assert PRE_TOOL_LIMIT_S + HARD_EXIT_GRACE_S < REPLY_MARGIN_S


@pytest.mark.parametrize("stall", ["sleep", "swallow"])
def test_a_stuck_pre_tool_filter_is_stopped_before_anything_runs(tmp_path, monkeypatch, stall):
    monkeypatch.setattr(worker_module, "PRE_TOOL_LIMIT_S", 0.3)
    runs: list[str] = []
    stuck = [True]

    def check() -> None:
        if not stuck[0]:
            return
        try:
            time.sleep(30)
        except Exception:
            if stall != "swallow":
                raise

    def note(text: str) -> str:
        runs.append(text)
        return "saved"

    worker = make_worker(tmp_path, note, check)
    started = time.monotonic()
    assert json.loads(worker.handle(signed().model_dump_json()))["error"] == "worker_timeout"
    assert time.monotonic() - started < 5
    assert runs == []
    # Nothing was journalled, so the same idempotency key is free to run once the filter answers.
    stuck[0] = False
    assert json.loads(worker.handle(signed().model_dump_json()))["output"] == "saved"
    assert runs == ["hello"]


# The real Worker and serve loop; the argument chooses a healthy filter or one that nothing can interrupt.
SCRIPT = textwrap.dedent('''
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
    from test_nemoclaw_restart import make_worker
    from orchestra.sandbox.nemoclaw import worker as worker_module
    from orchestra.sandbox.nemoclaw.worker import serve, take_protocol_streams

    root, mode = Path(sys.argv[2]), sys.argv[3]

    def check():
        while mode == "unstoppable":
            try:
                time.sleep(30)
            except BaseException:
                pass

    def note(text: str) -> str:
        with open(root / "runs", "a") as handle:
            handle.write(text + "\\n")
        return "saved"

    source, sink = take_protocol_streams()
    worker_module.PRE_TOOL_LIMIT_S = 0.3
    worker = make_worker(root, note, check)
    worker.hard_exit_after_s = 0.5
    raise SystemExit(serve(worker, source, sink))
''')


def transport_for(tmp_path: Path, modes: list[str]) -> PersistentTransport:
    """Each new session takes the next mode; the last one repeats."""
    script = tmp_path / "worker_script.py"
    script.write_text(SCRIPT, encoding="utf-8")

    def launch(sandbox: str) -> list[str]:
        mode = modes.pop(0) if len(modes) > 1 else modes[0]
        return [sys.executable, str(script), str(ROOT), str(tmp_path), mode]

    return PersistentTransport(launch)


def test_a_worker_stuck_in_a_filter_exits_and_the_retry_runs_on_a_new_one(tmp_path):
    transport = transport_for(tmp_path, ["unstoppable", "healthy"])
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="^worker_exited$"):
            transport.exchange(BOX, signed().model_dump_json(), 20)
        assert time.monotonic() - started < 15  # the worker ended itself well before the host's limit
        assert not (tmp_path / "runs").exists()
        assert json.loads(transport.exchange(BOX, signed().model_dump_json(), 20))["output"] == "saved"
        assert (tmp_path / "runs").read_text() == "hello\n"
    finally:
        transport.close()


def test_a_replaced_worker_answers_a_repeated_key_from_the_journal(tmp_path):
    transport = transport_for(tmp_path, ["healthy"])
    try:
        first = json.loads(transport.exchange(BOX, signed().model_dump_json(), 20))
        session = transport._slots[BOX].session
        assert session is not None
        old = session.process.pid
        os.killpg(old, signal.SIGKILL)
        session.process.wait(timeout=5)
        again = json.loads(transport.exchange(BOX, signed().model_dump_json(), 20))
        replacement = transport._slots[BOX].session
        assert replacement is not None and replacement.process.pid != old
        assert first == again and again["output"] == "saved"
        assert (tmp_path / "runs").read_text() == "hello\n"
    finally:
        transport.close()
