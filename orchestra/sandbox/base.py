"""SandboxRunner 계약 (DESIGN §10.3). 실제 NemoClaw 러너는 Codex(M8), 여기서는 계약 + Fake."""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from pydantic import BaseModel, Field


class ExecRequest(BaseModel):
    sandbox: str                                   # "orchestra-payments" ...
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    egress: list[str] = Field(default_factory=list)  # 툴이 선언한 "host:port" (Fake 의 정책 검사용)
    trace_headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 60.0
    idempotency_key: Optional[str] = None
    agent: Optional[str] = None                    # 워커가 이 이름의 grant 를 다시 검사한다
    capability_token: Optional[str] = Field(default=None, repr=False)  # 러너가 전송 직전에 발급
    # 같은 프로세스에서 실행하는 러너(Fake/Local)만 사용. 직렬화·전송 대상 아님.
    local_fn: Optional[Callable[..., Any]] = Field(default=None, exclude=True)


class ExecResult(BaseModel):
    ok: bool
    output: Any = None
    error: Optional[str] = None
    egress_denied: list[str] = Field(default_factory=list)
    policy_revision: Optional[str] = None
    duration_ms: int = 0


class SandboxRunner(Protocol):
    def exec(self, req: ExecRequest) -> ExecResult: ...
    def status(self, sandbox: str) -> dict[str, Any]: ...
