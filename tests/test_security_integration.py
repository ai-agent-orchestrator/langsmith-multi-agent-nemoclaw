"""Security integration paths remain offline and do not bypass existing ledgers."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from orchestra.hitl import make_approval_record
from orchestra.runtime import ApprovalLedger, Runtime
from orchestra.security.audit import AuditLog
from orchestra.observability.evals import sdk_evaluate
from orchestra.observability.client import SafeClient
from orchestra.patterns.hierarchical_graph import build_hierarchical_app
from orchestra.models.factory import ScriptedModelFactory
from orchestra.service import OrchestraService
from orchestra.simulation import load_scenario, run_scenario


def test_approval_signature_tampering_and_cross_store_replay():
    ledger = ApprovalLedger()
    record = make_approval_record("commit", {"value": "one"}, expires_at=20, user_id="user", amount=5)
    assert ledger.grant(record, now=10)
    signed = ledger._records[record.key]
    assert signed.decided and signed.signature_algorithm == "hmac-sha256"
    assert signed.signature and signed.signature != record.key
    assert not ledger.is_granted(record.key, binding=signed.model_copy(update={"signature": "0" * 64}), now=10)
    assert not ApprovalLedger().is_granted(record.key, binding=signed, now=10)
    assert ledger.claim(signed, now=10)
    assert not ledger.claim(signed, now=10)


def test_runtime_audit_hook_records_nested_arguments_only_as_digest(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite", b"a" * 32)
    runtime = Runtime(services={"audit_log": log})
    runtime.events.add("tool", args={"email": "private@example.com"}, tool="read")
    assert log.verify()
    with sqlite3.connect(log.path) as db:
        rows = str(db.execute("SELECT data FROM audit").fetchall())
    assert "private@example.com" not in rows and "hmac_sha256" in rows


def test_offline_evaluation_never_enters_sdk_or_constructs_client(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_SECURITY_MODE", "closed")
    with patch("langsmith.evaluate", side_effect=AssertionError("SDK must not run")):
        rows = sdk_evaluate(lambda inputs: inputs, [{"inputs": {"v": 1}, "outputs": {"v": 1}}],
                            [lambda output, reference: output == reference])
        assert rows[0]["scores"] == [True]
        with pytest.raises(RuntimeError):
            sdk_evaluate(None, [], [], offline=False, client=SafeClient(None, "closed"))


def test_global_kill_stops_graph_before_model(monkeypatch):
    scenario = load_scenario("concert_swap")
    runtime = Runtime(security_mode="closed")
    app = build_hierarchical_app(scenario["org"], ScriptedModelFactory(scenario["script"], runtime.events), runtime=runtime)
    monkeypatch.setenv("ORCHESTRA_KILL_SWITCH", "1")
    result = OrchestraService(app).ask(scenario["request"])
    assert result["blocked"] and not runtime.events.of("llm", "tool")


def test_six_reviewer_fanout_is_normalized_and_aggregates_three_issues():
    result = run_scenario("pr_review_parallel")
    assert len([name for name in result.app.org.teams if name.startswith("review_lead_sub")]) == 2
    state = result.app.graph.get_state(result.app.config("pr_review_parallel"))
    documents = []
    for message in state.values.get("messages", []):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.startswith('{"reports":'):
            documents.append(json.loads(content))
    assert any(len(document["reports"]) == 6 and len(document["issues"]) == 3 for document in documents)


def test_recursive_graph_failure_is_returned_as_a_block():
    from langgraph.errors import GraphRecursionError

    scenario = load_scenario("concert_swap")
    app = build_hierarchical_app(scenario["org"], ScriptedModelFactory({}))
    with patch.object(app.graph, "invoke", side_effect=GraphRecursionError()):
        result = OrchestraService(app).ask("test")
    assert result["blocked"] and result["reason"] == "recursion_limit"
