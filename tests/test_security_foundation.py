"""Tampering, scan binding, kill switches and offline gates fail closed."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from orchestra.security.audit import AuditLog
from orchestra.security.control import SecurityControl
from orchestra.security.invariants import check_invariants
from orchestra.security.scanner import FakeScanner, Finding, StaticScanner, files_digest, from_audit_json, require_scan
from orchestra.observability.security import report_egress_denied
from orchestra.models.providers import ProviderBuilder
from scripts.security_gate import garak_failures


def test_audit_tamper_wrong_key_and_tail_checkpoint(tmp_path):
    path = tmp_path / "audit.db"
    log = AuditLog(path, b"a" * 32)
    log.append({"kind": "approval", "args": {"email": "private@example.com"}})
    head = log.append({"kind": "blocked", "email": "private@example.com"})
    assert log.verify(expected_head=head)
    assert not AuditLog(path, b"b" * 32).verify()
    with sqlite3.connect(path) as db:
        assert "private@example.com" not in str(db.execute("SELECT data FROM audit").fetchall())
        db.execute("DELETE FROM audit WHERE seq=2")
    assert not log.verify(expected_head=head)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE audit SET data='{}'")
    assert not log.verify()


def test_scan_snapshot_and_high_findings_gate():
    files = {"bad.py": b"eval(user_input)"}
    report = StaticScanner().scan(files)
    assert not report.passed
    with pytest.raises(ValueError):
        require_scan(files, StaticScanner())
    with pytest.raises(ValueError):
        require_scan({"safe.py": b"pass"}, FakeScanner())
    assert from_audit_json(report.model_dump_json(), files) == report
    with pytest.raises(ValueError):
        from_audit_json(report.model_dump_json(), {"bad.py": b"pass"})


def test_team_latch_and_live_kill_switch(monkeypatch):
    control = SecurityControl(2, {"one": "team", "two": "team"})
    control.record("one", blocked=True)
    assert control.guard("two") is None
    control.record("two", blocked=True)
    assert control.guard("one") == "team_circuit_open"
    control.record("one", blocked=False)
    assert control.guard("two") == "team_circuit_open"
    control.reset("team")
    monkeypatch.setenv("ORCHESTRA_KILL_SWITCH", "true")
    assert control.guard("other") == "global_kill_switch"


def test_closed_feedback_has_no_sdk_calls_and_failure_is_isolated():
    client = Mock()
    report_egress_denied(client, "run", mode="closed", queue_id="queue")
    assert not client.mock_calls
    client.create_feedback.side_effect = RuntimeError("secret")
    report_egress_denied(client, "run", mode="masked", queue_id="queue")
    client.add_runs_to_annotation_queue.assert_called_once()


def test_private_and_sandbox_provider_routes_are_enforced_before_construction():
    constructor = Mock()
    builder = ProviderBuilder({"ORCHESTRA_LOCAL_BASE_URL": "http://localhost:1234/v1"},
                              constructors={"openai_compatible": constructor})
    builder("configured-model", "local")
    assert constructor.call_args.kwargs["metadata"] == {"model_route": "local"}
    for env in ({"ORCHESTRA_LOCAL_BASE_URL": "https://external.example/v1"},
                {"ORCHESTRA_SANDBOX": "nemoclaw", "NEMOCLAW_INFERENCE_BASE_URL": "https://external.example/v1"},
                {"ORCHESTRA_SANDBOX": "nemoclaw", "NEMOCLAW_INFERENCE_BASE_URL": "http://inference.local/v1", "OPENAI_API_KEY": "secret"}):
        with pytest.raises(ValueError):
            ProviderBuilder(env, constructors={"openai_compatible": constructor})("configured-model", "local")


def test_garak_requires_coverage_and_valid_nonzero_counts(tmp_path):
    path = tmp_path / "garak.jsonl"
    rows = [{"entry_type": "eval", "probe": family + ".Test", "passed": 1, "total": 1}
            for family in ("promptinject", "encoding", "xss")]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    assert garak_failures(path) == []
    rows[0]["passed"] = 0
    path.write_text("\n".join(json.dumps(row) for row in rows))
    assert "garak_failure_threshold" in garak_failures(path)
    path.write_text("")
    assert "garak_probe_coverage_missing" in garak_failures(path)


def test_unapproved_payment_and_denied_execution_fail_invariants():
    events = [{"kind": "sandbox", "data": {"tool": "capture_payment", "ok": True}},
              {"kind": "tool", "data": {"tool": "capture_payment", "result": {}}}]
    assert not check_invariants(events)["payment_approval"]
    assert not check_invariants(events)["tool_provenance"]
