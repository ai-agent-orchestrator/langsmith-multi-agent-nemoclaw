"""security 도메인 도구: 점검(READ), 레드팀 프로브(READ), 정책 변경 제안(RESERVE), 적용(COMMIT, 승인 필수).

보안 에이전트는 **제안자**다. 정책을 실제로 바꾸는 `apply_policy_change` 는 COMMIT 등급이라 코어가 사용자(운영자) 승인 없이는 막는다.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.tools import BaseTool, tool

from orchestra.sandbox.nemoclaw import policy
from orchestra.security import garak_bridge
from orchestra.security.scanner import SecurityScanner, Severity, gate
from orchestra.specs import SANDBOX_NAME_RE


@dataclass
class Proposal:
    proposal_id: str
    sandbox: str
    host: str
    port: int
    method: str
    path: str
    reason: str
    status: str = "proposed"          # proposed | rejected | applied
    problems: list[str] = field(default_factory=list)


class ProposalStore:
    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self.items: dict[str, Proposal] = {}
        self.applied_hooks: list[Any] = []     # 적용 시 호출(Fake 샌드박스의 hot-reload 흉내 등)

    def add(self, **kw: Any) -> Proposal:
        p = Proposal(proposal_id=f"prop_{next(self._seq):04d}", **kw)
        self.items[p.proposal_id] = p
        return p


def check_proposal(sandbox: str, host: str, port: int, method: str, path: str) -> list[str]:
    """제안이 NemoClaw 정책 규칙을 지키는지: 샌드박스명, 경로 형식, 금지 호스트, 와일드카드, 포트, 메서드."""
    problems: list[str] = []
    if not SANDBOX_NAME_RE.match(sandbox or ""):
        problems.append(f"invalid sandbox name {sandbox!r}")
    if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        problems.append(f"method {method!r} not allowed (wildcard '*' is not accepted for proposals)")
    if not path.startswith("/") or path.strip() in ("/", "/**"):
        problems.append("path must start with '/' and must not allow everything")
    doc = {"preset": {"name": "proposal"}, "network_policies": {"p": {
        "name": "p", "binaries": [{"path": policy.DEFAULT_BINARY}],
        "endpoints": [{"host": host, "port": port, "protocol": "rest", "enforcement": "enforce",
                       "rules": [{"allow": {"method": method.upper(), "path": path}}]}]}}}
    problems += [e.split(": ", 1)[-1] for e in policy.validate_preset(doc)]
    return problems


def make_tools(store: ProposalStore, scanner: SecurityScanner) -> dict[str, BaseTool]:
    @tool
    def scan_path(path: str) -> dict:
        """경로를 보안 스캐너로 점검하고 머지 차단 대상(blocking)과 검증 필요 항목을 요약한다."""
        rep = scanner.scan(path)
        return {"scanner": rep.scanner, "target": rep.target, "total": len(rep.findings),
                "blocking": [f.id for f in gate(rep, fail_on=Severity.HIGH)],
                "needs_validation": [f.id for f in rep.findings if f.verdict.value == "needs_validation"],
                "coverage": rep.coverage}

    @tool
    def run_redteam_probe(prompt: str) -> dict:
        """공격 프롬프트를 방어 계층에 흘려 거절되는지 확인한다(모델 없이 결정적 방어만)."""
        out = garak_bridge.respond(prompt)
        return {"refused": out == garak_bridge.REFUSAL}

    @tool
    def propose_policy_change(sandbox: str, host: str, port: int, method: str, path: str, reason: str) -> dict:
        """샌드박스 egress 허용 1건을 제안한다. 규칙 위반이면 rejected 로 기록되고 적용할 수 없다."""
        problems = check_proposal(sandbox, host, port, method, path)
        p = store.add(sandbox=sandbox, host=host, port=port, method=method.upper(), path=path, reason=reason,
                      status="rejected" if problems else "proposed", problems=problems)
        return {"proposal_id": p.proposal_id, "status": p.status, "problems": problems}

    @tool
    def apply_policy_change(proposal_id: str) -> dict:
        """승인된 제안을 적용한다(운영자 승인 필수). 거절된/없는 제안은 적용되지 않는다."""
        p: Optional[Proposal] = store.items.get(proposal_id)
        if p is None:
            return {"proposal_id": proposal_id, "status": "error", "problems": ["unknown proposal"]}
        if p.status == "rejected":
            return {"proposal_id": proposal_id, "status": "error", "problems": ["proposal was rejected: " + "; ".join(p.problems)]}
        if p.status == "proposed":
            for hook in store.applied_hooks:
                hook(p)
            p.status = "applied"
        return {"proposal_id": proposal_id, "status": p.status, "sandbox": p.sandbox, "endpoint": f"{p.host}:{p.port}"}

    return {t.name: t for t in (scan_path, run_redteam_probe, propose_policy_change, apply_policy_change)}
