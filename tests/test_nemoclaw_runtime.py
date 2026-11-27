"""Adversarial worker/transport/policy contracts, entirely offline."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from langchain_core.tools import tool
import pytest

from orchestra.filters.base import FilterDecision
from orchestra.filters.chain import FilterChain
from orchestra.sandbox.base import ExecRequest, ExecResult
from orchestra.sandbox.nemoclaw.cli import CommandResult, NemoClawCli
from orchestra.sandbox.nemoclaw.live import drift_errors, parse_presets_from_policy
from orchestra.sandbox.nemoclaw.runner import NemoClawRunner
from orchestra.sandbox.nemoclaw.transport import LocalTransport, StdioTransport
from orchestra.sandbox.nemoclaw.worker import Worker
from orchestra.specs import FilterSpec, SideEffect, ToolSpec
from orchestra.security.capability import CapabilityAuthority


@pytest.fixture
def rig(tmp_path):
    calls = []

    @tool
    def reserve(value: str) -> dict:
        """Record a simulated reservation."""
        calls.append(value)
        return {"value": value}

    spec = ToolSpec(name="reserve", sandbox="required", sandbox_name="test-box", side_effect=SideEffect.RESERVE)
    tools = {"reserve": (reserve, spec)}
    authority = CapabilityAuthority(b"a" * 32, {"worker": {"reserve"}})
    worker = Worker("test-box", tools, FilterChain([]), journal=tmp_path / "worker.db", grants={"worker": {"reserve"}}, authority=authority)
    request = ExecRequest(sandbox="test-box", tool="reserve", args={"value": "hello"}, agent="worker", idempotency_key="one")
    return SimpleNamespace(worker=worker, calls=calls, request=request, tools=tools, path=tmp_path / "worker.db", authority=authority)


def result(worker, req):
    if req.agent == "worker" and req.tool == "reserve":
        req.capability_token = worker.authority.issue(req.model_dump())
    return ExecResult.model_validate_json(worker.handle(req.model_dump_json()))


def test_worker_replay_across_restart_and_conflicting_args(rig):
    assert result(rig.worker, rig.request).ok
    restarted = Worker("test-box", rig.tools, FilterChain([]), journal=rig.path, grants={"worker": {"reserve"}}, authority=rig.authority)
    assert result(restarted, rig.request).output == {"value": "hello"}
    assert rig.calls == ["hello"]
    rig.request.args = {"value": "changed"}
    assert result(restarted, rig.request).error == "worker_idempotency_conflict"
    assert rig.calls == ["hello"]


@pytest.mark.parametrize("change,error", [
    ({"sandbox": "other"}, "worker_tool_forbidden"),
    ({"tool": "unknown"}, "worker_tool_forbidden"),
    ({"agent": "admin"}, "worker_capability_denied"),
    ({"idempotency_key": None}, "worker_idempotency_required"),
])
def test_host_cannot_change_worker_authority(rig, change, error):
    assert result(rig.worker, rig.request.model_copy(update=change)).error == error
    assert not rig.calls


def test_worker_rechecks_pre_post_and_commit_without_host_chain(rig):
    entry = FilterSpec(name="deny", stages=["pre_tool"])
    rig.worker.chain = FilterChain([(entry, lambda ctx: FilterDecision.block("deny"))])
    assert result(rig.worker, rig.request).error == "worker_pre_tool_blocked"
    assert not rig.calls
    entry.stages = ["post_tool"]
    assert result(rig.worker, rig.request).error == "worker_post_tool_blocked"
    assert rig.calls == ["hello"]
    rig.tools["reserve"][1].side_effect = SideEffect.COMMIT
    assert result(rig.worker, rig.request).error == "worker_commit_authorization_unavailable"


def test_wire_cannot_inject_local_callable_or_approval(rig):
    payload = rig.request.model_dump()
    payload["approval_granted"] = True
    assert json.loads(rig.worker.handle(json.dumps(payload)))["ok"] is False
    assert not rig.calls


def test_runner_drift_and_missing_transport_never_fall_back_to_host(rig):
    cli = Mock()
    cli.run.return_value = CommandResult((), True, stdout='{"network_policies":{}}')
    runner = NemoClawRunner(cli, {"test-box": {}}, authority=rig.authority)
    rig.request.local_fn = Mock(side_effect=AssertionError("must not execute"))
    assert runner.exec(rig.request).error == "sandbox_unavailable"
    runner.transport = LocalTransport(rig.worker.handle)
    assert runner.exec(rig.request).ok
    cli.run.return_value = CommandResult((), True, stdout='{"network_policies":{"extra":{}}}')
    assert runner.exec(rig.request).error == "policy_drift"
    assert rig.calls == ["hello"]
    rig.request.local_fn.assert_not_called()


def test_custom_keys_and_policy_contents_are_both_checked():
    expected = {"sample-egress": {"network_policies": {"read": {"endpoints": [{"host": "example.com"}]}}}}
    key = "nemoclaw_custom__sample-egress__read"
    output = json.dumps({"network_policies": {key: expected["sample-egress"]["network_policies"]["read"]}})
    assert parse_presets_from_policy(output) == {"sample-egress"}
    assert drift_errors(output, expected, closed=True) == []
    assert "policy_rules_differ" in drift_errors(output.replace("example.com", "evil.com"), expected, closed=True)
    with pytest.raises(ValueError):
        parse_presets_from_policy("Policy ready\nnetwork_policies: {}")


def test_transport_requires_explicit_verification_and_fixed_worker_command():
    cli = NemoClawCli(dry_run=True, dialect="modern")
    assert cli.command("policy_list", sandbox="test-box") == ("nemoclaw", "test-box", "policy", "list")
    assert cli.command("worker", sandbox="test-box")[-3:] == ("/opt/orchestra/venv/bin/python", "-m", "orchestra.sandbox.nemoclaw.worker")
    with pytest.raises(RuntimeError):
        StdioTransport(cli).exchange("test-box", "{}", 1)
