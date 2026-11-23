"""Declarative delegation may only reduce tools/domains/sandbox authority."""
import pytest

from orchestra.org import load_org


@pytest.mark.parametrize("dimension", ["tools", "domains", "sandboxes"])
def test_privilege_escalating_yaml_is_rejected(tmp_path, dimension):
    import yaml

    data = {"name": "caps", "root": "parent", "domains": [], "teams": {
        "parent": {"members": ["child"], "capabilities": {dimension: ["allowed"]}},
        "child": {"members": ["example/worker"], "capabilities": {dimension: ["forbidden"]}},
    }, "agents": {"example/worker": {"id": "example/worker", "name": "worker", "tools": []}}}
    path = tmp_path / "org.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="capability"):
        load_org(path)


def test_agent_tool_cannot_exceed_parent_allowlist():
    with pytest.raises(ValueError, match="capability"):
        load_org({"name": "caps", "root": "lead", "domains": [], "teams": {
            "lead": {"members": ["example/worker"], "capabilities": {"tools": ["read"]}},
        }, "agents": {"example/worker": {"id": "example/worker", "name": "worker", "tools": ["write"]}}})


def test_cross_domain_sandbox_tool_is_rejected_after_pack_resolution():
    from orchestra.registry import build_packs, resolve

    org = load_org("orgs/travel_payment.yaml")
    packs = build_packs(org.domains)
    travel = next(pack for pack in packs if pack.name == "travel")
    original = travel.agents

    def agents():
        result = original()
        result["travel/hotel_agent"].tools = ["capture_payment"]
        return result

    travel.agents = agents
    with pytest.raises(ValueError, match="capability"):
        resolve(org, packs)


def test_existing_orgs_and_generated_fanout_preserve_capability_bounds():
    from orchestra.org import normalize_fanout, assert_valid
    from orchestra.registry import resolve, build_packs

    org = resolve(load_org("orgs/travel_payment.yaml"), build_packs(["travel", "payment"])).org
    org.teams[org.root].max_fanout = 2
    org.teams[org.root].capabilities.tools = sorted({tool for agent in org.agents.values() for tool in agent.tools})
    normalized = normalize_fanout(org)
    assert_valid(normalized)
    assert len(normalized.teams) > len(org.teams)
