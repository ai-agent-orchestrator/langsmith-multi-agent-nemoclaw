import pytest
from pydantic import ValidationError

from orchestra.org import depth, normalize_fanout, recursion_limit, validate_org
from orchestra.specs import AgentSpec, OrgSpec, TeamSpec, ToolSpec


def _org(teams, agents, root="root"):
    return OrgSpec(name="t", root=root, domains=[], teams={k: TeamSpec(name=k, **v) for k, v in teams.items()},
                   agents={a.id: a for a in agents})


def _agents(n):
    return [AgentSpec(id=f"d/a{i}", name=f"a{i}") for i in range(n)]


def test_tool_cap_and_names():
    with pytest.raises(ValidationError):
        AgentSpec(id="d/x", name="x", tools=["a", "b", "c", "d"])
    with pytest.raises(ValidationError):
        AgentSpec(id="d/x", name="bad-name")
    with pytest.raises(ValidationError):
        ToolSpec(name="t", sandbox="required")  # sandbox_name 필요


def test_validate_errors():
    ags = _agents(2)
    ok = _org({"root": {"members": ["d/a0", "d/a1"]}}, ags)
    assert validate_org(ok) == []
    assert any("unknown member" in e for e in validate_org(_org({"root": {"members": ["nope"]}}, ags)))
    dup = _org({"root": {"members": ["l1", "l2"]}, "l1": {"members": ["d/a0"]}, "l2": {"members": ["d/a0"]}}, ags)
    assert any("two parents" in e for e in validate_org(dup))
    cyc = _org({"root": {"members": ["l1"]}, "l1": {"members": ["root"]}}, ags)
    assert any("cycle" in e or "root" in e for e in validate_org(cyc))
    coll = _org({"root": {"members": ["d/a0"]}}, [AgentSpec(id="d/a0", name="root")])
    assert any("collision" in e for e in validate_org(coll))


def test_handoff_must_stay_in_team():
    a0 = AgentSpec(id="d/a0", name="a0", handoff_to=["d/a1"])
    a1 = AgentSpec(id="d/a1", name="a1")
    same = _org({"root": {"members": ["d/a0", "d/a1"]}}, [a0, a1])
    assert validate_org(same) == []
    cross = _org({"root": {"members": ["l1", "l2"]}, "l1": {"members": ["d/a0"]}, "l2": {"members": ["d/a1"]}}, [a0, a1])
    assert any("crosses team boundary" in e for e in validate_org(cross))


def test_normalize_fanout_splits_and_is_pure():
    org = _org({"root": {"members": [a.id for a in _agents(6)]}}, _agents(6))
    before = org.model_dump()
    out = normalize_fanout(org)
    assert org.model_dump() == before                      # 입력 불변
    assert all(len(t.members) <= t.max_fanout for t in out.teams.values())
    assert validate_org(out) == []
    leaves = [m for t in out.teams.values() for m in t.members if m.startswith("d/")]
    assert sorted(leaves) == sorted(a.id for a in _agents(6))   # 6명 전원 보존
    assert depth(out) == 2 and len(out.teams) == 3


def test_normalize_fanout_noop_and_deep():
    org = _org({"root": {"members": [a.id for a in _agents(5)]}}, _agents(5))
    assert normalize_fanout(org).teams.keys() == org.teams.keys()
    big = _org({"root": {"members": [a.id for a in _agents(30)]}}, _agents(30))
    out = normalize_fanout(big)
    assert all(len(t.members) <= 5 for t in out.teams.values()) and validate_org(out) == []


def test_recursion_limit():
    org = _org({"root": {"members": [a.id for a in _agents(3)]}}, _agents(3))
    assert recursion_limit(org) == 10 + 8 * 1 + 2 * 3
    org.recursion_limit = 7
    assert recursion_limit(org) == 7
