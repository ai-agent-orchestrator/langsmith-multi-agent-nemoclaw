"""Human-in-the-loop: 승인 요청(interrupt) / 승인 키.

주의(LangGraph 규칙): interrupt() 가 재개되면 해당 노드가 처음부터 다시 실행된다.
따라서 interrupt 호출 앞쪽 코드는 부작용이 없어야 한다 (필터 체인·키 계산만 둔다).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApprovalBindingBlocked(RuntimeError):
    """Abort an invalid/expired authorization without exposing bound arguments."""


def approval_key(tool: str, args: dict[str, Any], *, user_id: str = "anonymous", amount: int | None = None,
                 expires_at: float | None = None, request_id: str = "") -> str:
    """같은 툴 + 같은 인자 = 같은 승인 키 (인자 순서 무관)."""
    blob = json.dumps({"tool": tool, "args": args, "user_id": user_id, "amount": amount, "expires_at": expires_at,
                       "request_id": request_id},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class ApprovalRecord(BaseModel):
    """Immutable binding. The ledger store signs a record when it is approved; callers never sign."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool: str
    args_json: str = Field(repr=False)
    user_id: str
    request_id: str = ""
    amount: int | None = Field(default=None, ge=0)
    expires_at: float = Field(allow_inf_nan=False)
    decided: bool = False
    signature: str | None = None
    signature_algorithm: str | None = None
    key_id: str | None = None

    @model_validator(mode="after")
    def valid_binding(self) -> "ApprovalRecord":
        """Verify the complete canonical tuple, never a truncated or client-selected key."""
        args = json.loads(self.args_json)
        if not isinstance(args, dict) or self.args_json != json.dumps(args, sort_keys=True, ensure_ascii=False,
                                                                     separators=(",", ":"), allow_nan=False):
            raise ValueError("invalid canonical arguments")
        if self.key != approval_key(self.tool, args, user_id=self.user_id, amount=self.amount,
                                   expires_at=self.expires_at, request_id=self.request_id):
            raise ValueError("invalid approval binding")
        return self


def make_approval_record(tool: str, args: dict, *, user_id: str = "anonymous", amount: int | None = None,
                         expires_at: float, request_id: str = "") -> ApprovalRecord:
    """Pure construction: no ledger writes before interrupt."""
    expires_at = float(expires_at)
    return ApprovalRecord(key=approval_key(tool, args, user_id=user_id, amount=amount, expires_at=expires_at, request_id=request_id),
                          tool=tool, args_json=json.dumps(args, sort_keys=True, ensure_ascii=False,
                                                         separators=(",", ":"), allow_nan=False),
                          user_id=user_id, amount=amount, expires_at=expires_at, request_id=request_id)


def request(payload: dict[str, Any]) -> bool:
    """사용자 승인을 요청한다. 재개 값이 `True` 또는 `{"approved": True, ...}` 일 때만 승인이다.

    "yes"·1 같은 참 같은 값은 거절로 처리한다 (fail closed).
    """
    from langgraph.types import interrupt

    answer = interrupt(payload)
    if isinstance(answer, dict):
        return answer.get("approved") is True
    return answer is True
