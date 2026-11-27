"""Egress isolation in the run path: repeated refusals close a sandbox without any extra setup."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.request

import pytest
from langchain_core.tools import StructuredTool, tool

from orchestra.filters.chain import FilterChain
from orchestra.filters.tool_wrapper import wrap_tool
from orchestra.runtime import Runtime
from orchestra.sandbox.base import ExecRequest
from orchestra.sandbox.fake import FakeSandboxRunner
from orchestra.sandbox.nemoclaw.cli import NemoClawCli
from orchestra.sandbox.nemoclaw.proxy_refusal import proxy_refused
from orchestra.sandbox.nemoclaw.runner import NemoClawRunner
from orchestra.sandbox.nemoclaw.worker import Worker
from orchestra.security.capability import CapabilityAuthority
from orchestra.security.decision import ReasonCode, from_tool_output
from orchestra.security.egress_isolation import EgressIsolationGuard
from orchestra.specs import SideEffect, ToolSpec

BOX = "orchestra-pay"
KEY = b"k" * 32
GRANTS = {"reader": {"fetch"}}


def spec(**extra) -> ToolSpec:
    return ToolSpec(name="fetch", side_effect=SideEffect.READ, sandbox="required", sandbox_name=BOX,
                    egress=[{"host": "pg.invalid"}], **extra)


def wrapped(runtime: Runtime):
    inner = StructuredTool.from_function(func=lambda q: {"q": q}, name="fetch", description="fetch")
    return wrap_tool(inner, spec(), FilterChain([]), runtime)


def test_a_runtime_isolates_a_sandbox_after_repeated_refusals():
    runner = FakeSandboxRunner(allowed={})
    runtime = Runtime(sandbox_mode="fake", sandbox=runner)
    call = wrapped(runtime)
    codes = [from_tool_output(call.invoke({"q": str(n)})).reason_code for n in range(4)]
    assert codes == [ReasonCode.EGRESS_DENIED] * 3 + [ReasonCode.EGRESS_DENIED_ISOLATED]
    assert len(runner.calls) == 3  # the fourth call never reached the runner
    assert runtime.sandbox is runner
    assert [item.sandbox for item in runtime.egress_guard.open_reviews()] == [BOX]
    assert [e.data["action"] for e in runtime.events.of("egress_isolation")] == ["denial"] * 3
    assert any(e.data.get("by") == "egress_isolation" for e in runtime.events.of("blocked"))
    runner.allow(BOX, "pg.invalid:443")
    assert from_tool_output(call.invoke({"q": "x"})).reason_code == ReasonCode.EGRESS_DENIED_ISOLATED
    assert runtime.egress_guard.release(BOX, by="operator", reason="reviewed")
    assert call.invoke({"q": "y"})["data"] == {"q": "y"}


def test_each_runtime_keeps_its_own_isolation_state():
    first, second = (Runtime(sandbox_mode="fake", sandbox=FakeSandboxRunner(allowed={})) for _ in range(2))
    first.egress_guard.record_denial(BOX, ["pg.invalid:443"])
    assert first.egress_guard is not second.egress_guard
    assert not second.events.of("egress_isolation") and first.events.of("egress_isolation")


def test_a_given_guard_keeps_its_settings_and_still_records_events():
    runtime = Runtime(sandbox_mode="fake", sandbox=FakeSandboxRunner(allowed={}),
                      egress_guard=EgressIsolationGuard(threshold=1))
    call = wrapped(runtime)
    call.invoke({"q": "1"})
    assert runtime.egress_guard.is_isolated(BOX)
    assert runtime.events.of("egress_isolation")


class _Refusing(BaseHTTPRequestHandler):
    """A proxy that refuses every tunnel, as the sandbox proxy does for unlisted hosts."""

    def do_CONNECT(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(int(self.server.status))  # type: ignore[attr-defined]
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def proxy():
    servers = []

    def start(status: int) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Refusing)
        server.status = status  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def fetch_through(address: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": address}))
    opener.open("https://pg.invalid/", timeout=5)


@pytest.mark.parametrize("status,refused", [(403, True), (502, False)])
def test_only_a_refused_tunnel_counts(proxy, status, refused):
    with pytest.raises(OSError) as caught:
        fetch_through(proxy(status))
    assert proxy_refused(caught.value) is refused


def test_a_refusal_is_found_behind_wrapping_errors_and_other_errors_are_not(proxy):
    httpx = pytest.importorskip("httpx")
    with pytest.raises(httpx.ProxyError) as caught:
        httpx.get("https://pg.invalid/", proxy=proxy(403), timeout=5, trust_env=False)
    assert proxy_refused(caught.value)
    try:
        try:
            raise OSError("Tunnel connection failed: 403 Forbidden")
        except OSError as error:
            raise RuntimeError("fetch failed") from error
    except RuntimeError as outer:
        assert proxy_refused(outer)
    loop = ValueError("a")
    loop.__context__ = loop
    for other in (loop, ValueError("403"), PermissionError("403 Forbidden"), TimeoutError()):
        assert not proxy_refused(other)


def make_worker(tmp_path: Path, function) -> Worker:
    return Worker(BOX, {"fetch": (tool("fetch", description="test tool")(function), spec())}, FilterChain([]),
                  journal=tmp_path / "journal.sqlite", grants=GRANTS, authority=CapabilityAuthority(KEY, GRANTS))


def test_the_worker_reports_a_refused_tunnel_with_the_declared_hosts(tmp_path, proxy):
    address = proxy(403)

    def fetch(q: str) -> str:
        fetch_through(address)
        return q

    request = ExecRequest(sandbox=BOX, tool="fetch", args={"q": "x"}, agent="reader")
    request.capability_token = CapabilityAuthority(KEY, GRANTS).issue(request.model_dump())
    reply = json.loads(make_worker(tmp_path, fetch).handle(request.model_dump_json()))
    assert reply["error"] == "egress_denied" and reply["egress_denied"] == ["pg.invalid:443"]
    assert "Tunnel" not in json.dumps(reply)


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text

    def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str:
        return self.text


@pytest.mark.parametrize("claimed,passed", [
    (["pg.invalid:443"], ["pg.invalid:443"]),
    (["secret=value", "pg.invalid:443"], ["pg.invalid:443"]),
    (["attacker text"], ["undeclared"]),
    ([], []),
])
def test_the_host_passes_on_only_declared_hosts_from_the_sandbox(monkeypatch, claimed, passed):
    runner = NemoClawRunner(NemoClawCli(dialect="modern"), {BOX: {}}, transport=_Reply(
        json.dumps({"ok": False, "error": "egress_denied", "egress_denied": claimed})))
    monkeypatch.setattr(runner, "status", lambda sandbox: {"ok": True, "policy_revision": "r"})
    result = runner.exec(ExecRequest(sandbox=BOX, tool="fetch", egress=["pg.invalid:443"]))
    assert result.egress_denied == passed
