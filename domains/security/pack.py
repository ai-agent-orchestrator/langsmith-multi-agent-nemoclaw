"""Security agents inspect submitted code and propose, never apply, policy changes."""
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from orchestra import registry
from orchestra.security.scanner import StaticScanner
from orchestra.specs import AgentSpec, ToolSpec


class SecurityPack:
    """All security-agent tools are READ/proposal operations with no policy mutation."""

    name = "security"

    def __init__(self, services: dict[str, Any]) -> None:
        scanner = services.get("security_scanner", StaticScanner())

        @tool
        def scan_code(code: str) -> dict:
            """Scan bounded submitted Python without executing it."""
            if len(code.encode()) > 200_000:
                raise ValueError("code_too_large")
            return scanner.scan({"submitted.py": code.encode()}).model_dump()

        @tool
        def propose_policy_change(sandbox: str, reason: str) -> dict:
            """Produce an inert proposal requiring separate operator review."""
            return {"status": "proposal_only", "sandbox": sandbox, "reason": reason,
                    "requires_operator_approval": True, "applied": False}

        @tool
        def probe_output(payload: str) -> str:
            """Echo bounded red-team data to exercise the post-tool trust boundary."""
            if len(payload) > 20_000:
                raise ValueError("probe_too_large")
            return payload

        @tool
        def bounded_probe_loop(seed: str) -> dict:
            """Exercise a deterministic three-iteration red-team loop with a hard cap."""
            from orchestra.modules.loop_adapter import loop_node

            return loop_node(lambda previous, iteration: previous, lambda output: False,
                             max_iterations=3)({"seed": seed})

        self._tools = {t.name: (t, ToolSpec(name=t.name)) for t in (scan_code, propose_policy_change, probe_output, bounded_probe_loop)}

    def tools(self) -> dict:
        """No mutation-capable policy tool is exposed."""
        return self._tools

    def agents(self) -> dict:
        """Give each specialist one least-privilege inspection/proposal tool."""
        return {"security/scan": AgentSpec(id="security/scan", name="scan_agent", tools=["scan_code"]),
                "security/policy": AgentSpec(id="security/policy", name="policy_agent", tools=["propose_policy_change"]),
                "security/redteam": AgentSpec(id="security/redteam", name="redteam_agent", tools=["probe_output", "bounded_probe_loop"])}

    def teams(self) -> dict:
        """Security review team is declared by the organization."""
        return {}

    def filters(self) -> list:
        """Retain organization-level filters for all security agents."""
        return []

    def filter_impls(self) -> dict:
        """No vendor-specific filter implementation is required."""
        return {}


registry.register("security", SecurityPack)
