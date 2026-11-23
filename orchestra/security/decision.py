"""런타임 중립 결정 계약 (SECURITY_ROADMAP ④ AR-1/AR-7): allow · ask · deny + reasonCode.

- 어떤 런타임(LangGraph, OpenClaw 플러그인 훅 등)이든 이 계약으로 결정 서비스를 호출한다.
- 결정 서비스에 닿지 못하거나 응답이 계약에 맞지 않으면 **deny** (fail closed, P5).
- reasonCode 하나가 API 응답·감사 로그·LangSmith metadata·대시보드 라벨에 같은 값으로 쓰인다 (P6).
- LLM 판단은 `signals` 로만 들어올 수 있고, 결정은 이 모듈 밖의 결정적 규칙이 내린다 (P1).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from ..filters.base import Action


class DecisionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"      # 사람 승인 필요
    DENY = "deny"


class ReasonCode(str, Enum):
    """결정 사유 레지스트리. 새 코드는 여기에만 추가한다 (문자열 오타가 관측성에서 사라지지 않게)."""

    ALLOWED = "ALLOWED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    COMMIT_WITHOUT_APPROVAL = "COMMIT_WITHOUT_APPROVAL"
    BLOCKED_PROMPT_INJECTION = "BLOCKED_PROMPT_INJECTION"
    BLOCKED_PII = "BLOCKED_PII"
    BLOCKED_CODE_GUARD = "BLOCKED_CODE_GUARD"
    BLOCKED_SIDE_EFFECT_POLICY = "BLOCKED_SIDE_EFFECT_POLICY"
    BLOCKED_PAYMENT_POLICY = "BLOCKED_PAYMENT_POLICY"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RECURSION_LIMIT = "RECURSION_LIMIT"
    FILTER_ERROR = "FILTER_ERROR"
    BLOCKED_BY_FILTER = "BLOCKED_BY_FILTER"
    EGRESS_DENIED = "EGRESS_DENIED"
    EGRESS_DENIED_ISOLATED = "EGRESS_DENIED_ISOLATED"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
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
    detail: str = ""                    # 사람용 설명. 원문 PII 를 넣지 않는다 (호출자가 마스킹 경로를 거친 값만)
    by: str | None = None            # 결정을 내린 필터/구성요소
    approval: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.action == DecisionAction.ALLOW

    def trace_metadata(self) -> dict[str, str]:
        """LangSmith run metadata / 메트릭 라벨용. 값은 짧은 열거형뿐이라 그대로 내보내도 안전하다."""
        out = {"security.decision": self.action.value, "security.reason_code": self.reason_code.value}
        if self.by:
            out["security.by"] = self.by
        return out


def allow(by: str | None = None) -> Decision:
    return Decision(action=DecisionAction.ALLOW, reason_code=ReasonCode.ALLOWED, by=by)


def deny(code: ReasonCode, detail: str = "", by: str | None = None) -> Decision:
    return Decision(action=DecisionAction.DENY, reason_code=code, detail=detail, by=by)


# ------------------------------------------------------------------ FilterChain → Decision
# blocked_by(필터 이름) 우선, 그다음 사유 문자열의 키워드. 모르는 필터는 BLOCKED_BY_FILTER 로 (deny 는 유지).
_BY_FILTER: dict[str, ReasonCode] = {
    "request_gate": ReasonCode.BLOCKED_PROMPT_INJECTION,
    "pii_mask": ReasonCode.BLOCKED_PII,
    "code_guard": ReasonCode.BLOCKED_CODE_GUARD,
    "side_effect_policy": ReasonCode.BLOCKED_SIDE_EFFECT_POLICY,
    "budget": ReasonCode.BUDGET_EXCEEDED,
    "amount_limit": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "state_transition": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "hold_expiry": ReasonCode.BLOCKED_PAYMENT_POLICY,
    "tool_wrapper": ReasonCode.COMMIT_WITHOUT_APPROVAL,
}


def code_for_block(blocked_by: str | None, reason: str = "") -> ReasonCode:
    """차단 주체·사유를 reasonCode 로. 필터 예외(fail-closed)는 FILTER_ERROR."""
    r = (reason or "").lower()
    if r.startswith("filter_error:"):          # FilterChain 이 필터 예외를 BLOCK 으로 바꿀 때 쓰는 접두사
        return ReasonCode.FILTER_ERROR
    if blocked_by in _BY_FILTER:
        return _BY_FILTER[blocked_by]
    if "commit_without_approval" in r:
        return ReasonCode.COMMIT_WITHOUT_APPROVAL
    if "budget" in r:
        return ReasonCode.BUDGET_EXCEEDED
    return ReasonCode.BLOCKED_BY_FILTER


def from_chain_result(result: Any) -> Decision:
    """`FilterChain.run()` 결과(ChainResult)를 계약으로 변환한다. ROUTE_SANDBOX/MODIFY 는 allow 로 접힌다."""
    action = getattr(result, "action", None)
    if action == Action.BLOCK:
        by = getattr(result, "blocked_by", None)
        return deny(code_for_block(by, getattr(result, "reason", "")), getattr(result, "reason", ""), by)
    if action == Action.REQUIRE_APPROVAL:
        return Decision(action=DecisionAction.ASK, reason_code=ReasonCode.APPROVAL_REQUIRED,
                        detail=getattr(result, "reason", ""), approval=getattr(result, "approval", None))
    if action in (Action.ALLOW, Action.MODIFY, Action.ROUTE_SANDBOX):
        return allow()
    return deny(ReasonCode.DECISION_INVALID, f"unknown chain action {action!r}")


def from_tool_output(output: Any) -> Decision:
    """tool_wrapper 가 돌려주는 오류 dict(blocked/approval_denied/sandbox_*)를 계약으로 해석한다."""
    if not isinstance(output, Mapping) or "error" not in output:
        return allow()
    err = output.get("error")
    if err == "blocked":
        by = output.get("by")
        return deny(code_for_block(by, str(output.get("reason", ""))), str(output.get("reason", "")), by)
    if err == "approval_denied":
        return deny(ReasonCode.APPROVAL_DENIED, by="user")
    if err == "sandbox_unavailable":
        return deny(ReasonCode.SANDBOX_UNAVAILABLE, by="sandbox")
    if err == "sandbox_failed":
        if output.get("reason") == "sandbox_isolated":
            return deny(ReasonCode.EGRESS_DENIED_ISOLATED, by="egress_isolation")
        if output.get("egress_denied"):
            return deny(ReasonCode.EGRESS_DENIED, ",".join(output.get("egress_denied") or []), by="sandbox")
        return deny(ReasonCode.SANDBOX_FAILED, str(output.get("reason", "")), by="sandbox")
    return allow()   # 도메인 오류(not_found 등)는 보안 결정이 아니다


# ------------------------------------------------------------------ 외부 결정 서비스 (fail closed)
class DecisionRequest(BaseModel):
    """결정 서비스 입력. `data` 는 하위 에이전트/툴 출력 = 항상 비신뢰 데이터 (P2)."""

    kind: str = "tool_call"                   # tool_call | handoff | response
    agent: str | None = None
    target: str | None = None              # 툴 이름 또는 handoff 대상
    args_sha256: str | None = None         # 원문 대신 해시 (P4 바인딩과 같은 값)
    capabilities: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)   # LLM 분류기 점수 등: 참고값일 뿐 결정이 아니다
    trace_id: str | None = None


class DecisionClient(Protocol):
    def decide(self, req: DecisionRequest) -> Any: ...


class FailClosedClient:
    """어떤 결정 클라이언트든 감싸서: 예외·타임아웃·계약 위반 응답 = deny.

    `on_decision` 콜백으로 모든 결정을 감사 로그/LangSmith 에 남길 수 있다 (콜백 실패는 결정에 영향 없음).
    """

    def __init__(self, inner: DecisionClient, on_decision: Callable[[DecisionRequest, Decision], None] | None = None):
        self.inner = inner
        self.on_decision = on_decision

    def decide(self, req: DecisionRequest) -> Decision:
        try:
            raw = self.inner.decide(req)
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 닫는다
            d = deny(ReasonCode.DECISION_SERVICE_UNAVAILABLE, type(exc).__name__, by="decision_client")
        else:
            d = self._coerce(raw)
        if self.on_decision is not None:
            try:
                self.on_decision(req, d)
            except Exception:  # noqa: BLE001, S110 — 관측 실패가 결정을 바꾸지 않게
                pass
        return d

    @staticmethod
    def _coerce(raw: Any) -> Decision:
        if isinstance(raw, Decision):
            return raw
        if not isinstance(raw, Mapping):
            return deny(ReasonCode.DECISION_INVALID, f"non-mapping response {type(raw).__name__}", by="decision_client")
        data = dict(raw)
        data.setdefault("reason_code", data.pop("reasonCode", None))
        try:
            d = Decision.model_validate(data)
        except ValidationError:
            return deny(ReasonCode.DECISION_INVALID, "response violates decision contract", by="decision_client")
        if d.action == DecisionAction.ALLOW and d.reason_code != ReasonCode.ALLOWED:
            # allow 에 차단 사유가 붙은 모순 응답은 신뢰하지 않는다
            return deny(ReasonCode.DECISION_INVALID, "allow with non-ALLOWED reason", by="decision_client")
        return d


class ChainDecisionClient:
    """로컬 FilterChain 을 결정 서비스로 쓰는 기본 구현 (외부 서비스가 없을 때/shadow 비교용)."""

    def __init__(self, chain: Any, ctx_factory: Callable[[DecisionRequest], Any]):
        self.chain = chain
        self.ctx_factory = ctx_factory

    def decide(self, req: DecisionRequest) -> Decision:
        return from_chain_result(self.chain.run(self.ctx_factory(req)))
