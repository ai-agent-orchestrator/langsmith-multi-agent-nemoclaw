"""Bindings cannot be replayed with changed identity, amount, arguments, or time."""
import pytest

from orchestra.hitl import make_approval_record
from orchestra.runtime import ApprovalLedger


def test_canonical_full_hash_and_one_shot_binding():
    record = make_approval_record("capture", {"b": 2, "a": 1}, user_id="alice", amount=100, expires_at=20)
    same = make_approval_record("capture", {"a": 1, "b": 2}, user_id="alice", amount=100, expires_at=20)
    assert record.key == same.key and len(record.key) == 64
    ledger = ApprovalLedger()
    assert ledger.grant(record, now=10)
    assert ledger.is_granted(record.key, binding=same, now=10)
    for changes in ({"user_id": "bob"}, {"amount": 101}, {"args": {"a": 1, "b": 3}}):
        values = {"tool": "capture", "args": {"a": 1, "b": 2}, "user_id": "alice", "amount": 100, "expires_at": 20, **changes}
        assert not ledger.is_granted(record.key, binding=make_approval_record(**values), now=10)
    assert not ledger.is_granted(record.key, binding=record, now=20)
    ledger.consume(record.key)
    assert not ledger.is_granted(record.key, binding=record, now=10)
    assert not ledger.grant(record, now=10)


@pytest.mark.parametrize("backend", ["prebuilt", "v1"])
@pytest.mark.parametrize("change", ["expiry", "amount", "user"])
def test_graph_resume_rejects_changed_binding(backend, change):
    from orchestra.models.factory import ScriptedModelFactory
    from orchestra.patterns.hierarchical_graph import build_hierarchical_app
    from orchestra.runtime import Runtime
    from orchestra.sandbox.fake import FakeSandboxRunner
    from orchestra.service import OrchestraService
    from orchestra.simulation import load_scenario

    clock = [0.0]
    services = {"clock": lambda: clock[0], "today": "2026-11-23"}
    runtime = Runtime(services=services, sandbox_mode="fake", clock=services["clock"])
    scenario = load_scenario("jeju_trip_payment")
    app = build_hierarchical_app(scenario["org"], ScriptedModelFactory(scenario["script"], runtime.events),
                                 runtime=runtime, services=services, backend=backend)
    runtime.sandbox = FakeSandboxRunner({"orchestra-payments": {"payments.example.invalid:443"}})
    service = OrchestraService(app)
    result = service.ask(scenario["request"], "bound", user_id="alice")
    assert result["interrupt"] and runtime.approvals.history == []
    if change == "expiry":
        clock[0] = 100000
    elif change == "amount":
        services["payments"].intents["pi_0001"].amount += 1
    assert service.resume("bound", True, user_id="bob" if change == "user" else "alice")["blocked"]
    assert not any(call.tool == "capture_payment" for call in runtime.sandbox.calls)
    assert services["payments"].intents["pi_0001"].status == "created"


def test_concurrent_claims_authorize_only_one_execution():
    from concurrent.futures import ThreadPoolExecutor

    ledger = ApprovalLedger()
    record = make_approval_record("capture", {}, expires_at=20)
    assert ledger.grant(record, now=10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ledger.claim(record, now=10), range(32)))
    assert sum(results) == 1
