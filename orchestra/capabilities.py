"""Declarative capability attenuation; sandbox ownership comes from resolved packs."""
from __future__ import annotations

from typing import Any


def capability_errors(org: Any, *, tools: dict | None = None, agent_domains: dict | None = None,
                      tool_domains: dict | None = None) -> list[str]:
    """Validate ancestor upper bounds and prohibit foreign-domain sandbox access."""
    dimensions = ("tools", "domains", "sandboxes")
    errors: list[str] = []
    memo: dict[str, dict[str, set[str]]] = {}
    active: set[str] = set()
    owners: dict[str, set[str]] = {}
    if tools is not None:
        for name, (_, spec) in tools.items():
            if spec.sandbox_name:
                owners.setdefault(spec.sandbox_name, set()).add((tool_domains or {}).get(name, ""))
        if any(len(domains) != 1 for domains in owners.values()):
            errors.append("capability: sandbox has ambiguous domain ownership")

    def empty() -> dict[str, set[str]]:
        return {dimension: set() for dimension in dimensions}

    def visit(name: str) -> dict[str, set[str]]:
        if name in memo:
            return memo[name]
        if name in active:
            return empty()  # Structural cycle validation remains in org.validate_org.
        if name in org.agents:
            agent = org.agents[name]
            result = empty()
            result["tools"] = set(agent.tools)
            if agent_domains is not None:
                domain = agent_domains.get(name)
                result["domains"] = {domain} if domain else set()
                for tool in agent.tools:
                    spec = (tools or {}).get(tool)
                    if spec and spec[1].sandbox_name:
                        sandbox = spec[1].sandbox_name
                        result["sandboxes"].add(sandbox)
                        if owners.get(sandbox) != {domain}:
                            errors.append("capability: agent requests a foreign domain sandbox")
            return result
        if name not in org.teams:
            return empty()  # Domain agents are resolved later; unknown members still fail structural checks.
        active.add(name)
        team = org.teams[name]
        result = empty()
        for child in team.members:
            child_caps = visit(child)
            for dimension in dimensions:
                result[dimension].update(child_caps[dimension])
        for dimension in dimensions:
            declared = getattr(team.capabilities, dimension)
            if declared is not None:
                bound = set(declared)
                if not result[dimension] <= bound:
                    errors.append(f"capability: {dimension} delegation exceeds parent authority")
                result[dimension] = bound
        if tools is not None:
            if not result["tools"] <= tools.keys():
                errors.append("capability: unknown tool grant")
            if not result["domains"] <= set(org.domains):
                errors.append("capability: unknown domain grant")
            for sandbox in result["sandboxes"]:
                if sandbox not in owners or not owners[sandbox] <= result["domains"]:
                    errors.append("capability: team requests a foreign or unknown sandbox")
        active.remove(name)
        memo[name] = result
        return result

    for name in org.teams:
        visit(name)
    return errors
