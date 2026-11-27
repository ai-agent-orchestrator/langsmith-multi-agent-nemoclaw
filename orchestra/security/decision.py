"""런타임 중립 결정 계약: allow · ask · deny + reason_code.

- 어느 런타임이든(LangGraph, 외부 플러그인 훅 등) 이 계약으로 결정 서비스를 호출한다.
- 결정 서비스에 닿지 못하거나 응답이 계약에 맞지 않으면 **deny** (fail closed).
- reason_code 하나가 서비스 응답·이벤트·감사 로그·트레이스에 같은 값으로 쓰인다.
- LLM 판단은 `signals` 로만 들어올 수 있고, 결정은 이 모듈 밖의 결정적 규칙이 내린다.

이 모듈은 런타임·필터를 import 하지 않는다. `EventLog` 가 여기에 의존하기 때문이다.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError


class DecisionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"      # 사람 승인 필요
    DENY = "deny"


class ReasonCode(str, Enum):
    """결정 사유 레지스트리. 새 코드는 여기에만 추가한다 (문자열 오타가 관측에서 사라지지 않게)."""

    ALLOWED = "ALLOWED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_BINDING_INVALID = "APPROVAL_BINDING_INVALID"
    COMMIT_WITHOUT_APPROVAL = "COMMIT_WITHOUT_APPROVAL"
    BLOCKED_PROMPT_INJECTION = "BLOCKED_PROMPT_INJECTION"
    BLOCKED_TOOL_INSTRUCTION = "BLOCKED_TOOL_INSTRUCTION"
    BLOCKED_PII = "BLOCKED_PII"
    BLOCKED_CODE_GUARD = "BLOCKED_CODE_GUARD"
    BLOCKED_SIDE_EFFECT_POLICY = "BLOCKED_SIDE_EFFECT_POLICY"
    BLOCKED_PAYMENT_POLICY = "BLOCKED_PAYMENT_POLICY"
    BLOCKED_MESSAGE_CONTRACT = "BLOCKED_MESSAGE_CONTRACT"
    BLOCKED_MODEL_CALL = "BLOCKED_MODEL_CALL"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RECURSION_LIMIT = "RECURSION_LIMIT"
    KILL_SWITCH = "KILL_SWITCH"
    TEAM_CIRCUIT_OPEN = "TEAM_CIRCUIT_OPEN"
    FILTER_ERROR = "FILTER_ERROR"
    BLOCKED_BY_FILTER = "BLOCKED_BY_FILTER"
    EGRESS_DENIED = "EGRESS_DENIED"
    EGRESS_DENIED_ISOLATED = "EGRESS_DENIED_ISOLATED"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    SANDBOX_POLICY_UNVERIFIED = "SANDBOX_POLICY_UNVERIFIED"
    SANDBOX_CAPABILITY_DENIED = "SANDBOX_CAPABILITY_DENIED"
    BLOCKED_BY_SANDBOX_FILTER = "BLOCKED_BY_SANDBOX_FILTER"
    SANDBOX_FAILED = "SANDBOX_FAILED"
    INTAKE_UNPINNED = "INTAKE_UNPINNED"
    INTAKE_HASH_MISMATCH = "INTAKE_HASH_MISMATCH"
    INTAKE_SCAN_BLOCKED = "INTAKE_SCAN_BLOCKED"
    INTAKE_NOT_ADMITTED = "INTAKE_NOT_ADMITTED"
    DECISION_SERVICE_UNAVAILABLE = "DECISION_SERVICE_UNAVAILABLE"
    DECISION_INVALID = "DECISION_INVALID"


class Decision(BaseModel):
    action: DecisionAction
    reason_code: ReasonCode
    detail: str = ""                    # 사람용 설명. 원문 PII 를 넣지 않는다
    by: str | None = None               # 결정을 내린 필터·구성요소
    approval: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        """`allow` 만 참이다. `ask` 는 아직 허용이 아니다."""
        return self.action == DecisionAction.ALLOW

    def trace_metadata(self) -> dict[str, str]:
        """트레이스 metadata·메트릭 라벨용. 짧은 식별자만 담아 그대로 내보내도 안전하다."""
        out = {"security.decision": self.action.value, "security.reason_code": self.reason_code.value}
        if self.by:
            out["security.by"] = self.by
        return out


def allow(by: str | None = None) -> Decision:
    """허용 결정."""
    return Decision(action=DecisionAction.ALLOW, reason_code=ReasonCode.ALLOWED, by=by)


def deny(code: ReasonCode, detail: str = "", by: str | None = None) -> Decision:
    """거부 결정."""
    return Decision(action=DecisionAction.DENY, reason_code=code, detail=detail, by=by)


# ------------------------------------------------------------------ 차단 주체·사유 → reason_code
# 사유 문자열이 정해진 코드인 경우: 차단 주체가 달라도 같은 reason_code 가 되도록 먼저 본다.
_BY_REASON: dict[str, ReasonCode] = {
    "global_kill_switch": ReasonCode.KILL_SWITCH,
    "team_circuit_open": ReasonCode.TEAM_CIRCUIT_OPEN,
    "recursion_limit": ReasonCode.RECURSION_LIMIT,
    "commit_without_approval": ReasonCode.COMMIT_WITHOUT_APPROVAL,
    "tool_instruction_blocked": ReasonCode.BLOCKED_TOOL_INSTRUCTION,
    "message_contract_blocked": ReasonCode.BLOCKED_MESSAGE_CONTRACT,
    "sandbox_isolated": ReasonCode.EGRESS_DENIED_ISOLATED,
}
# 필터·구성요소 이름. 모르는 이름은 BLOCKED_BY_FILTER 로 (deny 는 유지).
_BY_SOURCE: dict[str, ReasonCode] = {
    "request_gate": ReasonCode.BLOCKED_PROMPT_INJECTION,
    "pii_mask": ReasonCode.BLOCKED_PII,
    "code_guard": ReasonCode.BLOCKED_CODE_GUARD,
    "side_effect_policy": ReasonCode.BLOCKED_SIDE_EFFECT_POLICY,
    "budget": ReasonCode.BUDGET_EXCEEDED,
    "amount_limit": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "state_transition": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "hold_expiry": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "idempotency": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "approval_binding": ReasonCode.APPROVAL_BINDING_INVALID,
    "tool_wrapper": ReasonCode.COMMIT_WITHOUT_APPROVAL,
    "tool_taint": ReasonCode.BLOCKED_TOOL_INSTRUCTION,
    "message_contract": ReasonCode.BLOCKED_MESSAGE_CONTRACT,
    "recursion_limit": ReasonCode.RECURSION_LIMIT,
    "pre_model": ReasonCode.BLOCKED_MODEL_CALL,
    "egress_isolation": ReasonCode.EGRESS_DENIED_ISOLATED,
}


def code_for_block(blocked_by: object, reason: object = "") -> ReasonCode:
    """차단 주체·사유를 reason_code 로. 필터 예외(fail-closed)는 FILTER_ERROR."""
    text = reason if isinstance(reason, str) else ""
    if text.startswith("filter_error:"):      # FilterChain 이 필터 예외를 BLOCK 으로 바꿀 때 쓰는 접두사
        return ReasonCode.FILTER_ERROR
    if text in _BY_REASON:
        return _BY_REASON[text]
    if isinstance(blocked_by, str) and blocked_by in _BY_SOURCE:
        return _BY_SOURCE[blocked_by]
    return ReasonCode.BLOCKED_BY_FILTER


# ------------------------------------------------------------------ 샌드박스 오류 코드 → reason_code
_SANDBOX_CODES: dict[str, ReasonCode] = {
    "sandbox_isolated": ReasonCode.EGRESS_DENIED_ISOLATED,
    "egress_denied": ReasonCode.EGRESS_DENIED,
    "sandbox_unavailable": ReasonCode.SANDBOX_UNAVAILABLE,
    "sandbox_not_declared": ReasonCode.SANDBOX_UNAVAILABLE,
    "policy_unavailable": ReasonCode.SANDBOX_POLICY_UNVERIFIED,
    "policy_drift": ReasonCode.SANDBOX_POLICY_UNVERIFIED,
    "policy_unrecognized": ReasonCode.SANDBOX_POLICY_UNVERIFIED,
    "worker_tool_forbidden": ReasonCode.SANDBOX_CAPABILITY_DENIED,
    "worker_capability_denied": ReasonCode.SANDBOX_CAPABILITY_DENIED,
    "worker_capability_invalid": ReasonCode.SANDBOX_CAPABILITY_DENIED,
    "worker_sandbox_mismatch": ReasonCode.SANDBOX_CAPABILITY_DENIED,
    "worker_commit_authorization_unavailable": ReasonCode.COMMIT_WITHOUT_APPROVAL,
    "worker_pre_tool_blocked": ReasonCode.BLOCKED_BY_SANDBOX_FILTER,
    "worker_post_tool_blocked": ReasonCode.BLOCKED_BY_SANDBOX_FILTER,
    "worker_taint_blocked": ReasonCode.BLOCKED_TOOL_INSTRUCTION,
}


def code_for_sandbox_error(error: object, egress_denied: object = None) -> ReasonCode:
    """실패한 샌드박스 실행을 reason_code 로. 모르는 오류는 SANDBOX_FAILED."""
    if error == "sandbox_isolated":
        return ReasonCode.EGRESS_DENIED_ISOLATED
    if egress_denied:
        return ReasonCode.EGRESS_DENIED
    return _SANDBOX_CODES.get(error, ReasonCode.SANDBOX_FAILED) if isinstance(error, str) else ReasonCode.SANDBOX_FAILED


def event_reason_code(kind: str, data: Mapping[str, Any]) -> str | None:
    """이벤트 로그에 붙일 reason_code. 차단과 샌드박스 실패가 아니면 None."""
    if kind == "blocked":
        return code_for_block(data.get("by"), data.get("reason")).value
    if kind == "sandbox" and data.get("ok") is False:
        return code_for_sandbox_error(data.get("error"), data.get("egress_denied")).value
    return None


def from_chain_result(result: Any) -> Decision:
    """`FilterChain.run()` 결과를 계약으로 바꾼다. MODIFY/ROUTE_SANDBOX 는 allow 로 접힌다."""
    action = getattr(result, "action", None)
    name = getattr(action, "value", action)
    reason = str(getattr(result, "reason", "") or "")
    if name == "block":
        by = getattr(result, "blocked_by", None)
        return deny(code_for_block(by, reason), reason, by)
    if name == "require_approval":
        return Decision(action=DecisionAction.ASK, reason_code=ReasonCode.APPROVAL_REQUIRED,
                        detail=reason, approval=getattr(result, "approval", None))
    if name in ("allow", "modify", "route_sandbox"):
        return allow()
    return deny(ReasonCode.DECISION_INVALID, "unknown chain action")


def from_tool_output(output: Any) -> Decision:
    """tool_wrapper 가 돌려주는 오류 dict 를 계약으로 해석한다. 도메인 오류(not_found 등)는 allow."""
    if not isinstance(output, Mapping):
        return allow()
    # 지시문이 섞인 툴 결과는 wrapper 가 {"source": "tool", "data": {...}} 로 감싸 돌려준다.
    data = output.get("data")
    if output.get("source") == "tool" and isinstance(data, Mapping) and data.get("error") == "tool_instruction_blocked":
        return deny(ReasonCode.BLOCKED_TOOL_INSTRUCTION, by="tool_taint")
    error = output.get("error")
    if error == "blocked":
        by, reason = output.get("by"), str(output.get("reason", ""))
        return deny(code_for_block(by, reason), reason, by if isinstance(by, str) else None)
    if error == "approval_denied":
        return deny(ReasonCode.APPROVAL_DENIED, by="user")
    if error == "tool_instruction_blocked":
        return deny(ReasonCode.BLOCKED_TOOL_INSTRUCTION, by="tool_taint")
    if error == "sandbox_unavailable":
        return deny(ReasonCode.SANDBOX_UNAVAILABLE, by="sandbox")
    if error == "sandbox_failed":
        reason, hosts = str(output.get("reason", "")), output.get("egress_denied")
        code = code_for_sandbox_error(reason, hosts)
        by = "egress_isolation" if code == ReasonCode.EGRESS_DENIED_ISOLATED else "sandbox"
        return deny(code, ",".join(map(str, hosts)) if hosts else reason, by=by)
    return allow()


# ------------------------------------------------------------------ 외부 결정 서비스 (fail closed)
class DecisionRequest(BaseModel):
    """결정 서비스 입력. 하위 에이전트·툴 출력은 항상 비신뢰 데이터다."""

    kind: str = "tool_call"                   # tool_call | handoff | response
    agent: str | None = None
    target: str | None = None                 # 툴 이름 또는 handoff 대상
    args_sha256: str | None = None            # 원문 대신 해시
    capabilities: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)   # 분류기 점수 등 참고값. 결정이 아니다
    trace_id: str | None = None


class DecisionClient(Protocol):
    def decide(self, req: DecisionRequest) -> Any: ...


class FailClosedClient:
    """어떤 결정 클라이언트든 감싼다: 예외·타임아웃·계약 위반 응답은 deny.

    `on_decision` 으로 모든 결정을 기록할 수 있다. 기록 실패는 결정을 바꾸지 않는다.
    """

    def __init__(self, inner: DecisionClient,
                 on_decision: Callable[[DecisionRequest, Decision], None] | None = None) -> None:
        self.inner = inner
        self.on_decision = on_decision

    def decide(self, req: DecisionRequest) -> Decision:
        """항상 `Decision` 을 돌려준다. 예외를 밖으로 내지 않는다."""
        try:
            raw = self.inner.decide(req)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 닫는다
            decision = deny(ReasonCode.DECISION_SERVICE_UNAVAILABLE, type(exc).__name__, by="decision_client")
        else:
            decision = self._coerce(raw)
        if self.on_decision is not None:
            try:
                self.on_decision(req, decision)
            except Exception:  # noqa: BLE001, S110 - 관측 실패가 결정을 바꾸지 않게
                pass
        return decision

    @staticmethod
    def _coerce(raw: Any) -> Decision:
        if isinstance(raw, Decision):
            data: dict[str, Any] = raw.model_dump()     # model_construct 로 만든 객체도 다시 검증한다
        elif isinstance(raw, Mapping):
            data = dict(raw)
            if "reasonCode" in data:
                data.setdefault("reason_code", data.pop("reasonCode"))
        else:
            return deny(ReasonCode.DECISION_INVALID, "non-mapping response", by="decision_client")
        try:
            decision = Decision.model_validate(data)
        except (ValidationError, TypeError):
            return deny(ReasonCode.DECISION_INVALID, "response violates decision contract", by="decision_client")
        if (decision.action == DecisionAction.ALLOW) != (decision.reason_code == ReasonCode.ALLOWED):
            # 허용에 차단 사유가 붙거나 차단에 ALLOWED 가 붙은 모순 응답은 신뢰하지 않는다
            return deny(ReasonCode.DECISION_INVALID, "action contradicts reason code", by="decision_client")
        return decision


class ChainDecisionClient:
    """로컬 FilterChain 을 결정 서비스로 쓰는 기본 구현."""

    def __init__(self, chain: Any, ctx_factory: Callable[[DecisionRequest], Any]) -> None:
        self.chain = chain
        self.ctx_factory = ctx_factory

    def decide(self, req: DecisionRequest) -> Decision:
        """요청을 FilterContext 로 바꿔 체인을 실행한다."""
        return from_chain_result(self.chain.run(self.ctx_factory(req)))
