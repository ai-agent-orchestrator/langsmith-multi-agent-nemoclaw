"""egress 거부 → 자동 격리 회귀 테스트 (LLM·네트워크·NemoClaw 불필요)."""
from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from orchestra.filters.chain import FilterChain
from orchestra.filters.tool_wrapper import wrap_tool
from orchestra.runtime import Runtime
from orchestra.sandbox.base import ExecRequest
from orchestra.sandbox.fake import FakeSandboxRunner
from orchestra.security.audit import AuditLog
from orchestra.security.decision import ReasonCode, from_tool_output
from orchestra.security.egress_isolation import EgressIsolationGuard, IsolatingSandboxRunner
from orchestra.simulation_trace import trace_records
from orchestra.specs import SideEffect, ToolSpec


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _req(sandbox="orchestra-pay"):
    return ExecRequest(sandbox=sandbox, tool="create_payment_intent", egress=["pg.invalid:443"],
                       local_fn=lambda **kw: {"ok": True})


def test_isolates_after_threshold_and_stops_calling_inner_runner():
    inner = FakeSandboxRunner(allowed={})
    guard = EgressIsolationGuard(threshold=3, window_s=60, clock=_Clock())
    runner = IsolatingSandboxRunner(inner, guard)
    for _ in range(3):
        assert runner.exec(_req()).error == "egress_denied"
    assert guard.is_isolated("orchestra-pay") and len(guard.open_reviews()) == 1
    assert guard.open_reviews()[0].denied_hosts == ["pg.invalid:443"]
    calls = len(inner.calls)
    result = runner.exec(_req())
    assert result.ok is False and result.error == "sandbox_isolated" and len(inner.calls) == calls
    assert runner.status("orchestra-pay")["isolated"] is True
    assert guard.check("orchestra-pay").reason_code == ReasonCode.EGRESS_DENIED_ISOLATED


def test_isolation_is_per_sandbox_and_window_expires():
    clock = _Clock()
    guard = EgressIsolationGuard(threshold=2, window_s=10, clock=clock)
    runner = IsolatingSandboxRunner(FakeSandboxRunner(allowed={}), guard)
    runner.exec(_req("a"))
    clock.t = 11
    runner.exec(_req("a"))
    assert not guard.is_isolated("a")
    runner.exec(_req("a"))
    assert guard.is_isolated("a") and not guard.is_isolated("b")
    assert runner.exec(_req("b")).error == "egress_denied"


def test_allowed_egress_never_counts():
    runner = IsolatingSandboxRunner(FakeSandboxRunner(allowed={"orchestra-pay": {"pg.invalid:443"}}),
                                    EgressIsolationGuard(threshold=1))
    for _ in range(5):
        assert runner.exec(_req()).ok
    assert not runner.guard.is_isolated("orchestra-pay")


def test_release_requires_human_and_reason_and_resets_count():
    guard = EgressIsolationGuard(threshold=1)
    IsolatingSandboxRunner(FakeSandboxRunner(allowed={}), guard).exec(_req())
    with pytest.raises(ValueError):
        guard.release("orchestra-pay", by="", reason="x")
    with pytest.raises(ValueError):
        guard.release("orchestra-pay", by="ops", reason=" ")
    assert guard.is_isolated("orchestra-pay")
    assert guard.release("orchestra-pay", by="ops@example", reason="policy reviewed")
    assert not guard.is_isolated("orchestra-pay") and guard.open_reviews() == []
    assert guard.review_queue[0].status == "released"
    assert guard.release("orchestra-pay", by="ops", reason="again") is False


def test_isolation_events_flow_into_event_log_trace_and_audit(tmp_path):
    runtime = Runtime(sandbox_mode="fake")
    log = AuditLog(tmp_path / "audit.sqlite", b"a" * 32)
    runtime.events.audit = log
    guard = EgressIsolationGuard(threshold=2, on_event=lambda kind, data: runtime.events.add(kind, **data))
    runner = IsolatingSandboxRunner(FakeSandboxRunner(allowed={}), guard)
    runner.exec(_req())
    runner.exec(_req())
    guard.release("orchestra-pay", by="ops@example", reason="reviewed")
    assert [e.kind for e in runtime.events.events] == [
        "egress_isolation", "egress_isolation", "blocked", "egress_isolation"]
    blocked = runtime.events.of("blocked")[0].data
    assert blocked["reason_code"] == "EGRESS_DENIED_ISOLATED" and blocked["by"] == "egress_isolation"
    assert trace_records(runtime.events)[0]["reason"] == "sandbox_isolated"
    assert log.verify()
    assert b"ops@example" not in (tmp_path / "audit.sqlite").read_bytes()


def test_isolation_survives_event_sink_failure_and_rejects_bad_config():
    def bad(kind, data):
        raise OSError

    guard = EgressIsolationGuard(threshold=1, on_event=bad)
    IsolatingSandboxRunner(FakeSandboxRunner(allowed={}), guard).exec(_req())
    assert guard.is_isolated("orchestra-pay")
    for options in ({"threshold": 0}, {"window_s": 0}, {"window_s": float("nan")}):
        with pytest.raises(ValueError):
            EgressIsolationGuard(**options)


def test_wrapped_tool_reports_isolation_with_one_reason_code():
    runtime = Runtime(sandbox_mode="fake")
    runtime.sandbox = IsolatingSandboxRunner(FakeSandboxRunner(allowed={}), EgressIsolationGuard(threshold=1))
    tool = StructuredTool.from_function(func=lambda q: {"q": q}, name="fetch", description="fetch")
    spec = ToolSpec(name="fetch", side_effect=SideEffect.READ, sandbox="required", sandbox_name="orchestra-pay",
                    egress=[{"host": "pg.invalid"}])
    wrapped = wrap_tool(tool, spec, FilterChain([]), runtime)
    assert from_tool_output(wrapped.invoke({"q": "1"})).reason_code == ReasonCode.EGRESS_DENIED
    assert from_tool_output(wrapped.invoke({"q": "2"})).reason_code == ReasonCode.EGRESS_DENIED_ISOLATED
    assert [e.data["reason_code"] for e in runtime.events.of("sandbox")] == [
        "EGRESS_DENIED", "EGRESS_DENIED_ISOLATED"]
