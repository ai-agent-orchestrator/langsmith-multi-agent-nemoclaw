"""Network changes bind approvals to exact reviewed bytes and never replace policies."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from orchestra.hitl import make_approval_record
from orchestra.runtime import ApprovalLedger
from orchestra.sandbox.nemoclaw.cli import CommandResult, NemoClawCli
from orchestra.sandbox.nemoclaw.runner import NemoClawRunner
from orchestra.security.policy_change import proposal_binding, apply_approved_policy


def document(host="service.example.com"):
    return {"preset": {"name": "reviewed-egress", "description": "reviewed"}, "network_policies": {
        "service": {"name": "service", "endpoints": [{"host": host, "port": 443, "protocol": "rest",
        "enforcement": "enforce", "rules": [{"allow": {"method": "GET", "path": "/v1/**"}}]}],
        "binaries": [{"path": "/opt/orchestra/venv/bin/python"}]}}}


def rig(tmp_path, doc):
    ledger = ApprovalLedger()
    record = make_approval_record("apply_network_policy", proposal_binding("test-box", doc), expires_at=20)
    runner = NemoClawRunner(NemoClawCli(preset_root=tmp_path, dialect="modern"), {"test-box": {}})
    runner.status = Mock(return_value={"ok": True})
    runner.cli.run = Mock(return_value=CommandResult((), True))
    return ledger, record, runner


def test_policy_requires_signed_approval_and_audit(tmp_path):
    doc = document()
    ledger, record, runner = rig(tmp_path, doc)
    assert apply_approved_policy(runner, ledger, record, "test-box", doc, now=10)["error"] == "policy_approval_invalid"
    assert ledger.grant(record, now=10)
    assert apply_approved_policy(runner, ledger, record, "test-box", doc, now=10)["error"] == "policy_audit_required"
    runner.cli.run.assert_not_called()


def test_policy_changed_args_rejected_and_valid_approval_consumed_once(tmp_path):
    doc = document()
    ledger, record, runner = rig(tmp_path, doc)
    assert ledger.grant(record, now=10)
    audit = Mock()
    changed = deepcopy(doc)
    changed["network_policies"]["service"]["endpoints"][0]["host"] = "other.example.com"
    assert apply_approved_policy(runner, ledger, record, "test-box", changed, now=10, audit=audit)["error"] == "policy_approval_invalid"
    assert apply_approved_policy(runner, ledger, record, "test-box", doc, now=10, audit=audit)["ok"]
    assert not apply_approved_policy(runner, ledger, record, "test-box", doc, now=10, audit=audit)["ok"]
    runner.cli.run.assert_called_once()
    assert runner.cli.run.call_args.args == ("policy_add_file",)
    assert not list(tmp_path.glob("*.yaml"))


def test_policy_cross_sandbox_host_is_denied(tmp_path):
    doc = document()
    ledger, record, runner = rig(tmp_path, doc)
    runner.expected["other-box"] = {"other": document()}
    assert ledger.grant(record, now=10)
    result = apply_approved_policy(runner, ledger, record, "test-box", doc, now=10, audit=Mock())
    assert result["error"] == "policy_cross_sandbox_validation_failed"
    runner.cli.run.assert_not_called()


def test_custom_file_command_is_rooted_and_additive(tmp_path):
    path = tmp_path / "reviewed.yaml"
    path.write_text("reviewed file")
    cli = NemoClawCli(preset_root=tmp_path, dialect="modern", dry_run=True)
    result = cli.run("policy_add_file", sandbox="test-box", preset=str(path))
    assert result.argv == ("nemoclaw", "test-box", "policy", "add", "--from-file", str(path), "--yes")
    assert result.dry_run


def test_audit_failure_before_mutation_is_reported_and_blocks_cli(tmp_path):
    doc = document()
    ledger, record, runner = rig(tmp_path, doc)
    assert ledger.grant(record, now=10)
    audit = Mock()
    audit.append.side_effect = OSError("disk full")
    result = apply_approved_policy(runner, ledger, record, "test-box", doc, now=10, audit=audit)
    assert result == {"ok": False, "error": "policy_audit_failed"}
    runner.cli.run.assert_not_called()
    assert not list(tmp_path.glob("*.yaml"))
