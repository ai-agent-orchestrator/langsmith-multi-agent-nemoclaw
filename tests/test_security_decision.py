"""결정 계약과 기존 차단 경로의 reason_code 회귀 테스트 (LLM·네트워크·NemoClaw 불필요)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestra.filters.base import FilterContext, FilterDecision, Stage
from orchestra.filters.chain import FilterChain
from orchestra.runtime import EventLog
from orchestra.security.audit_events import audit_record
from orchestra.security.control import SecurityControl
from orchestra.security.decision import (
    ChainDecisionClient,
    Decision,
    DecisionAction,
    DecisionRequest,
    FailClosedClient,
    ReasonCode,
    code_for_sandbox_error,
    from_chain_result,
    from_tool_output,
)
from orchestra.simulation import run_scenario
from orchestra.simulation_trace import load_trace_export, trace_records
from orchestra.specs import FilterSpec

ROOT = Path(__file__).resolve().parents[1]


# =============================================================== 결정 계약
class _Named:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn

    def __call__(self, ctx):
        return self.fn(ctx)


def _chain(name, fn, control=None):
    return FilterChain([(FilterSpec(name=name, stages=["pre_tool"]), _Named(name, fn))],
                       security_control=control)


def _ctx():
    return FilterContext(stage=Stage.PRE_TOOL, payload={"x": 1}, tool="t", agent="a")


@pytest.mark.parametrize("name,code", [
    ("request_gate", ReasonCode.BLOCKED_PROMPT_INJECTION),
    ("budget", ReasonCode.BUDGET_EXCEEDED),
    ("amount_limit", ReasonCode.BLOCKED_PAYMENT_POLICY),
    ("side_effect_policy", ReasonCode.BLOCKED_SIDE_EFFECT_POLICY),
    ("mystery", ReasonCode.BLOCKED_BY_FILTER),
])
def test_chain_block_maps_to_filter_reason_code(name, code):
    decision = from_chain_result(_chain(name, lambda c: FilterDecision.block("no")).run(_ctx()))
    assert decision.action == DecisionAction.DENY and decision.reason_code == code and decision.by == name


def test_filter_exception_is_deny_filter_error():
    def boom(_):
        raise RuntimeError("x")

    decision = from_chain_result(_chain("budget", boom).run(_ctx()))
    assert decision.action == DecisionAction.DENY and decision.reason_code == ReasonCode.FILTER_ERROR


def test_kill_switch_and_circuit_have_their_own_codes(monkeypatch):
    control = SecurityControl(threshold=1)
    chain = _chain("budget", lambda c: FilterDecision.block("no"), control)
    chain.run(_ctx())
    assert from_chain_result(chain.run(_ctx())).reason_code == ReasonCode.TEAM_CIRCUIT_OPEN
    monkeypatch.setenv("ORCHESTRA_KILL_SWITCH", "1")
    assert from_chain_result(chain.run(_ctx())).reason_code == ReasonCode.KILL_SWITCH


def test_require_approval_maps_to_ask_and_modify_to_allow():
    ask = from_chain_result(_chain("p", lambda c: FilterDecision.require_approval("pay", {"a": 1})).run(_ctx()))
    assert ask.action == DecisionAction.ASK and ask.approval == {"a": 1} and not ask.allowed
    ok = from_chain_result(_chain("m", lambda c: FilterDecision.modify({"y": 2})).run(_ctx()))
    assert ok.allowed and ok.reason_code == ReasonCode.ALLOWED
    assert from_chain_result(object()).reason_code == ReasonCode.DECISION_INVALID


@pytest.mark.parametrize("output,code", [
    ({"error": "blocked", "by": "budget", "reason": "tool call budget exceeded (40)"}, ReasonCode.BUDGET_EXCEEDED),
    ({"error": "blocked", "by": "tool_wrapper", "reason": "commit_without_approval"},
     ReasonCode.COMMIT_WITHOUT_APPROVAL),
    ({"error": "blocked", "by": "approval_binding", "reason": "approval_arguments_changed"},
     ReasonCode.APPROVAL_BINDING_INVALID),
    ({"error": "blocked", "reason": "message_contract_blocked"}, ReasonCode.BLOCKED_MESSAGE_CONTRACT),
    ({"error": "approval_denied", "tool": "capture_payment"}, ReasonCode.APPROVAL_DENIED),
    ({"source": "tool", "data": {"error": "tool_instruction_blocked"}}, ReasonCode.BLOCKED_TOOL_INSTRUCTION),
    ({"error": "sandbox_unavailable"}, ReasonCode.SANDBOX_UNAVAILABLE),
    ({"error": "sandbox_failed", "reason": "egress_denied", "egress_denied": ["pg:443"]}, ReasonCode.EGRESS_DENIED),
    ({"error": "sandbox_failed", "reason": "sandbox_isolated", "egress_denied": []},
     ReasonCode.EGRESS_DENIED_ISOLATED),
    ({"error": "sandbox_failed", "reason": "policy_drift"}, ReasonCode.SANDBOX_POLICY_UNVERIFIED),
    ({"error": "sandbox_failed", "reason": "worker_capability_invalid"}, ReasonCode.SANDBOX_CAPABILITY_DENIED),
    ({"error": "sandbox_failed", "reason": "timeout"}, ReasonCode.SANDBOX_FAILED),
])
def test_tool_output_reason_codes(output, code):
    decision = from_tool_output(output)
    assert decision.action == DecisionAction.DENY and decision.reason_code == code


def test_tool_output_domain_error_is_not_a_security_decision():
    assert from_tool_output({"error": "not_found"}).allowed
    assert from_tool_output({"source": "tool", "data": {"error": "not_found"}}).allowed
    assert from_tool_output({"ok": True}).allowed and from_tool_output("text").allowed


def test_every_worker_and_runner_error_code_is_classified():
    """새 오류 코드를 만들고 계약에 넣지 않으면 여기서 드러난다."""
    sources = (ROOT / "orchestra/sandbox/nemoclaw" / name for name in ("worker.py", "runner.py"))
    codes = {code for path in sources
             for code in re.findall(r'(?:error=|"error": )"([a-z_]+)"', path.read_text(encoding="utf-8"))}
    operational = {"worker_request_failed", "worker_invalid_timeout", "worker_idempotency_required",
                   "worker_idempotency_conflict", "worker_execution_indeterminate", "worker_output_too_large",
                   "worker_timeout", "worker_execution_failed", "worker_unavailable", "sandbox_transport_failed"}
    assert len(codes) > 15
    for code in codes - operational:
        assert code_for_sandbox_error(code) != ReasonCode.SANDBOX_FAILED, code
    for code in operational:
        assert code_for_sandbox_error(code) == ReasonCode.SANDBOX_FAILED, code


class _Raises:
    def decide(self, req):
        raise TimeoutError


class _Returns:
    def __init__(self, value):
        self.value = value

    def decide(self, req):
        return self.value


@pytest.mark.parametrize("inner,code", [
    (_Raises(), ReasonCode.DECISION_SERVICE_UNAVAILABLE),
    (_Returns(None), ReasonCode.DECISION_INVALID),
    (_Returns({"action": "maybe", "reason_code": "ALLOWED"}), ReasonCode.DECISION_INVALID),
    (_Returns({"action": "allow", "reasonCode": "NOT_A_CODE"}), ReasonCode.DECISION_INVALID),
    (_Returns({"action": "allow", "reason_code": "BLOCKED_PII"}), ReasonCode.DECISION_INVALID),
    (_Returns({"action": "deny", "reason_code": "ALLOWED"}), ReasonCode.DECISION_INVALID),
    (_Returns(Decision.model_construct(action="allow", reason_code="BLOCKED_PII")), ReasonCode.DECISION_INVALID),
])
def test_fail_closed_client_denies_on_failure_or_bad_contract(inner, code):
    decision = FailClosedClient(inner).decide(DecisionRequest(target="t"))
    assert decision.action == DecisionAction.DENY and decision.reason_code == code


def test_fail_closed_client_accepts_valid_camelcase_and_survives_observer_failure():
    seen = []
    client = FailClosedClient(_Returns({"action": "ask", "reasonCode": "APPROVAL_REQUIRED"}),
                              on_decision=lambda req, d: seen.append(d.reason_code))
    assert client.decide(DecisionRequest()).action == DecisionAction.ASK
    assert seen == [ReasonCode.APPROVAL_REQUIRED]

    def bad(req, decision):
        raise RuntimeError

    ok = FailClosedClient(_Returns({"action": "allow", "reason_code": "ALLOWED"}), on_decision=bad)
    assert ok.decide(DecisionRequest()).allowed


def test_chain_decision_client_and_trace_metadata():
    chain = _chain("pii_mask", lambda c: FilterDecision.block("pii"))
    decision = FailClosedClient(ChainDecisionClient(chain, lambda req: _ctx())).decide(DecisionRequest(target="t"))
    assert decision.trace_metadata() == {"security.decision": "deny", "security.reason_code": "BLOCKED_PII",
                                         "security.by": "pii_mask"}


def test_reason_codes_are_unique_and_upper():
    values = [code.value for code in ReasonCode]
    assert len(values) == len(set(values)) and all(value == value.upper() for value in values)


# =============================================================== 기존 차단 경로가 같은 코드를 남긴다
def test_event_log_attaches_reason_code_to_blocks_and_sandbox_failures():
    events = EventLog()
    events.add("blocked", by="budget", reason="tool call budget exceeded (1)")
    events.add("sandbox", tool="t", ok=False, error="policy_drift")
    events.add("sandbox", tool="t", ok=True, error=None)
    events.add("tool", tool="t")
    assert [e.data.get("reason_code") for e in events.events] == [
        "BUDGET_EXCEEDED", "SANDBOX_POLICY_UNVERIFIED", None, None]
    records = trace_records(events)
    assert records[0]["reason_code"] == "BUDGET_EXCEEDED"
    assert load_trace_export({"trace": records})["trace"][1]["reason_code"] == "SANDBOX_POLICY_UNVERIFIED"
    assert audit_record("blocked", 1, events.events[0].data)["reason_code"] == "BUDGET_EXCEEDED"


@pytest.mark.parametrize("scenario", ["pii_bulk_blocked", "jeju_trip_payment"])
def test_scenario_block_events_and_final_answer_agree(scenario):
    result = run_scenario(scenario)
    blocks = result.events.of("blocked")
    assert all(e.data["reason_code"] in ReasonCode.__members__ for e in blocks)
    if result.final.get("blocked"):
        assert result.final["reason_code"] == blocks[-1].data["reason_code"]
    else:
        assert "reason_code" not in result.final


def test_prompt_injection_gate_answer_carries_reason_code():
    result = run_scenario("pii_bulk_blocked")
    assert result.final["blocked"] and result.final["reason_code"] == "BLOCKED_PROMPT_INJECTION"
