from pathlib import Path

from orchestra import registry
from orchestra.sandbox.fake import FakeSandboxRunner
from orchestra.sandbox.base import ExecRequest
from orchestra.sandbox.nemoclaw import policy
from orchestra.specs import EgressRule, SideEffect, ToolSpec


def specs():
    packs = registry.build_packs(["travel", "payment"], {})
    return [s for p in packs for _, s in p.tools().values()]


def test_presets_per_sandbox_and_security_mode():
    s = specs()
    masked = policy.build_presets(s, security_mode="masked")
    assert set(masked) == {"orchestra-payments-egress", "orchestra-langsmith"}
    closed = policy.build_presets(s, security_mode="closed")
    assert set(closed) == {"orchestra-payments-egress"}                # closed: LangSmith 경로를 물리적으로 열지 않는다
    m = policy.applied_presets(["orchestra-payments"], masked)
    assert m == {"orchestra-payments": ["orchestra-payments-egress", "orchestra-langsmith"]}


def test_validate_flags_placeholder_and_forbidden():
    s = specs()
    pre = policy.build_presets(s)
    assert any("placeholder" in e for e in policy.validate_presets(pre, tool_specs=s))
    assert policy.validate_presets(pre, tool_specs=s, allow_placeholder=True) == []
    bad = ToolSpec(name="x", side_effect=SideEffect.COMMIT, sandbox="required", sandbox_name="sb",
                   egress=[EgressRule(host="inference.local", rules=[{"method": "POST", "path": "/v1/**"}])])
    errs = policy.validate_presets(policy.build_presets([bad], security_mode="closed"), tool_specs=[bad])
    assert any("inference/credential host" in e for e in errs)


def test_validate_structural_rules():
    doc = {"preset": {"name": "p"}, "network_policies": {"k": {"name": "k", "endpoints": [
        {"host": "*.evil.com", "port": 443, "enforcement": "enforce", "rules": []}], "binaries": []}}}
    errs = policy.validate_preset(doc)
    joined = " | ".join(errs)
    assert "binaries missing" in joined and "wildcard host" in joined and "no L7 rules" in joined
    doc["network_policies"]["k"]["access"] = "full"
    assert any("access: full" in e for e in policy.validate_preset(doc))
    doc["network_policies"]["k"]["endpoints"][0].update(host="a.example.com", rules=[{"allow": {"method": "GET", "path": "/"}}],
                                                         allowed_ips=["10.0.0.0/8"])
    assert any("allowed_ips" in e for e in policy.validate_preset(doc))


def test_cross_sandbox_same_host_rejected():
    mk = lambda sb: ToolSpec(name=f"t_{sb}", sandbox="required", sandbox_name=sb,
                             egress=[EgressRule(host="api.pg.example.com", rules=[{"method": "POST", "path": "/x"}])])
    a, b = mk("orchestra-payments"), mk("orchestra-code")
    errs = policy.validate_presets(policy.build_presets([a, b], security_mode="closed"), tool_specs=[a, b])
    assert any("split trust boundaries" in e for e in errs)


def test_check_applied_fail_closed():
    exp = {"sb": ["sb-egress"]}                                          # closed 모드 기대
    assert policy.check_applied(exp, {"sb": ["sb-egress"]}) == []
    assert any("missing" in e for e in policy.check_applied(exp, {"sb": []}))
    assert any("forbids trace egress" in e for e in policy.check_applied(exp, {"sb": ["sb-egress", policy.LANGSMITH_PRESET]}))


def test_drift_detection(tmp_path: Path):
    s = specs()
    pre = policy.build_presets(s)
    assert len(policy.diff_dir(pre, tmp_path)) == 2                     # missing
    policy.write_all(pre, tmp_path)
    assert policy.diff_dir(pre, tmp_path) == []
    (tmp_path / "orchestra-langsmith.yaml").write_text("x: 1\n")
    assert policy.diff_dir(pre, tmp_path) == [f"drift: {tmp_path / 'orchestra-langsmith.yaml'}"]
    (tmp_path / "old.yaml").write_text("x: 1\n")
    assert any(p.startswith("stale") for p in policy.diff_dir(pre, tmp_path))


def test_fake_runner_egress():
    r = FakeSandboxRunner({"sb": {"a.com:443"}})
    ok = r.exec(ExecRequest(sandbox="sb", tool="t", egress=["a.com:443"], local_fn=lambda: 7))
    assert ok.ok and ok.output == 7
    denied = r.exec(ExecRequest(sandbox="sb", tool="t", egress=["b.com:443"], local_fn=lambda: 7))
    assert not denied.ok and denied.egress_denied == ["b.com:443"]
    r.allow("sb", "b.com:443")                                           # hot-reload 흉내 → revision 증가
    assert r.exec(ExecRequest(sandbox="sb", tool="t", egress=["b.com:443"], local_fn=lambda: 7)).ok
    assert r.policy_revision == 2
