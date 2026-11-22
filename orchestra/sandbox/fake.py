"""테스트/시뮬레이션용 샌드박스: preset 의 허용 호스트로 egress 를 흉내 낸다 (NemoClaw 없이 동작)."""
from __future__ import annotations

import time
from typing import Any, Optional

from .base import ExecRequest, ExecResult


class FakeSandboxRunner:
    def __init__(self, allowed: Optional[dict[str, set[str]]] = None, policy_revision: int = 1,
                 fail_tools: Optional[dict[str, str]] = None):
        self.allowed = allowed or {}                  # sandbox -> {"host:port"}
        self.policy_revision = policy_revision
        self.fail_tools = fail_tools or {}            # tool -> error 메시지 (장애 주입)
        self.calls: list[ExecRequest] = []

    def allow(self, sandbox: str, *hostports: str) -> None:
        self.allowed.setdefault(sandbox, set()).update(hostports)
        self.policy_revision += 1                     # hot-reload 흉내

    def exec(self, req: ExecRequest) -> ExecResult:
        self.calls.append(req)
        rev = str(self.policy_revision)
        denied = [e for e in req.egress if e not in self.allowed.get(req.sandbox, set())]
        if denied:
            return ExecResult(ok=False, error="egress_denied", egress_denied=denied, policy_revision=rev)
        if req.tool in self.fail_tools:
            return ExecResult(ok=False, error=self.fail_tools[req.tool], policy_revision=rev)
        if req.local_fn is None:
            return ExecResult(ok=False, error="no_local_fn", policy_revision=rev)
        t0 = time.perf_counter()
        out = req.local_fn(**req.args)
        return ExecResult(ok=True, output=out, policy_revision=rev,
                          duration_ms=int((time.perf_counter() - t0) * 1000))

    def status(self, sandbox: str) -> dict[str, Any]:
        return {"sandbox": sandbox, "runner": "fake", "policy_revision": self.policy_revision,
                "allowed": sorted(self.allowed.get(sandbox, set()))}
