"""security DomainPack (O2): security_lead 팀 = scan_agent / policy_agent / redteam_agent. 제안만 하고 적용은 승인."""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool

from orchestra import registry
from orchestra.filters.base import Filter
from orchestra.security.scanner import FakeScanner
from orchestra.specs import AgentSpec, FilterSpec, SideEffect, TeamSpec, ToolSpec

from .tools import ProposalStore, make_tools

_SE = {"scan_path": SideEffect.READ, "run_redteam_probe": SideEffect.READ,
       "propose_policy_change": SideEffect.RESERVE, "apply_policy_change": SideEffect.COMMIT}


class SecurityPack:
    name = "security"

    def __init__(self, services: dict[str, Any]):
        self.store: ProposalStore = services.setdefault("proposals", ProposalStore())
        scanner = services.setdefault("scanner", FakeScanner())
        self._tools = make_tools(self.store, scanner)

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        return {n: (t, ToolSpec(name=n, side_effect=_SE[n], sensitivity="internal", idempotent=n != "apply_policy_change"))
                for n, t in self._tools.items()}

    def agents(self) -> dict[str, AgentSpec]:
        return {
            "security/scan_agent": AgentSpec(id="security/scan_agent", name="scan_agent", tools=["scan_path"], model="sonnet",
                prompt="코드/플러그인 경로를 스캐너로 점검하고 차단 대상과 검증 필요 항목을 요약한다. 결과를 근거 없이 부풀리지 않는다."),
            "security/redteam_agent": AgentSpec(id="security/redteam_agent", name="redteam_agent", tools=["run_redteam_probe"], model="sonnet",
                prompt="공격 프롬프트가 방어 계층에서 거절되는지 확인한다. 거절되지 않으면 그대로 보고한다."),
            "security/policy_agent": AgentSpec(id="security/policy_agent", name="policy_agent",
                tools=["propose_policy_change", "apply_policy_change"], model="sonnet",
                prompt="네트워크 정책 변경을 제안한다. 제안이 rejected 면 적용하지 말고 사유를 보고한다. 적용은 운영자 승인 후에만 가능하다."),
        }

    def teams(self) -> dict[str, TeamSpec]:
        return {}

    def filters(self) -> list[FilterSpec]:
        return []

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        return {}


registry.register("security", SecurityPack)
