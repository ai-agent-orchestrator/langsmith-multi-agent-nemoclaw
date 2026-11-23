import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestra.security.scanner import (FakeScanner, Finding, FindingsReport, Severity, Verdict,
                                        from_audit_json, gate)


def C(id, sev, file="a.py"):
    return Finding(id=id, title=id, severity=sev, verdict="confirmed", file=file)


def N(id):
    return Finding(id=id, title=id, verdict="needs_validation")


def R(id):
    return Finding(id=id, title=id, verdict="rejected")


def test_confirmed_requires_file_and_severity():
    with pytest.raises(ValidationError):
        Finding(id="x", title="t", severity="high", verdict="confirmed")
    with pytest.raises(ValidationError):
        Finding(id="x", title="t", verdict="confirmed", file="a.py")


def test_only_confirmed_carries_severity():
    with pytest.raises(ValidationError):
        Finding(id="x", title="t", severity="high", verdict="needs_validation")


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError):
        FindingsReport(scanner="s", target=".", findings=[C("a", "low"), C("a", "low")])


def test_gate_blocks_confirmed_at_threshold_only():
    rep = FakeScanner({"src": [C("a", "high"), R("b"), C("c", "medium"), N("d"), C("e", "critical")]}).scan("src/x")
    assert sorted(f.id for f in gate(rep)) == ["a", "e"]
    assert sorted(f.id for f in gate(rep, fail_on=Severity.MEDIUM)) == ["a", "c", "e"]
    assert sorted(f.id for f in gate(rep, block_on_needs_validation=True)) == ["a", "d", "e"]   # 엄격 모드
    assert "b" not in [f.id for f in gate(rep, fail_on=Severity.INFO, block_on_needs_validation=True)]


def test_fake_scanner_prefix_and_calls():
    s = FakeScanner({"src": [C("a", "low")]})
    assert s.scan("docs").findings == [] and s.scan("src").findings[0].id == "a" and s.calls == ["docs", "src"]


# ---- security-audit-skill 실제 스키마(report-schema.json) 형태
CONFIRMED = {"verdict": "confirmed", "fingerprint": "web/auth:login-bypass", "title": "t", "description": "d",
             "root_cause": "r", "intended_behavior": "i",
             "trace": [{"kind": "entrypoint", "file": "app/routes.py", "line": 10, "scope": "s", "description": "d"},
                       {"kind": "sink", "file": "app/auth.py", "line": 42, "scope": "s", "description": "d"}],
             "evidence": [{"file": "app/auth.py", "line": 44, "description": "d"}],
             "conditions": [], "execution": {}, "remediation": {"strategy": "use constant-time compare"},
             "severity": {"likelihood": {"score": "high", "reason": "r"}, "impact": {"score": "high", "reason": "r"},
                          "overall_severity": "high"}, "confidence": "high"}
NEEDS = {"verdict": "needs_validation", "fingerprint": "ssrf:fetch", "title": "t", "description": "d",
         "claimed_root_cause": "c", "trace": [{"kind": "sink", "file": "x.py", "line": 1, "scope": "s", "description": "d"}],
         "evidence": [], "blockers": ["no sandbox"], "validation_plan": "run it"}
REJECTED = {"verdict": "rejected", "fingerprint": "xss:render", "title": "t", "description": "d",
            "claimed_root_cause": "c", "trace": [], "evidence": [], "reason": "escaped upstream"}


def test_from_audit_json_maps_real_schema():
    rep = from_audit_json(json.dumps([CONFIRMED, NEEDS, REJECTED]), coverage=["app/"])
    by = {f.id: f for f in rep.findings}
    c = by["web/auth:login-bypass"]
    assert (c.verdict, c.severity, c.file, c.line) == (Verdict.CONFIRMED, Severity.HIGH, "app/auth.py", 44)
    assert c.remediation == "use constant-time compare"
    assert by["ssrf:fetch"].severity is None and by["ssrf:fetch"].file == "x.py"
    assert by["xss:render"].verdict == Verdict.REJECTED
    assert rep.coverage == ["app/"]
    assert [f.id for f in gate(rep)] == ["web/auth:login-bypass"]


def test_from_audit_json_is_fail_safe():
    weird = dict(CONFIRMED, fingerprint="w1", verdict="maybe")
    no_loc = dict(CONFIRMED, fingerprint="w2", trace=[], evidence=[])
    no_sev = dict(CONFIRMED, fingerprint="w3", severity={})
    rep = from_audit_json([weird, no_loc, no_sev])
    assert all(f.verdict == Verdict.NEEDS_VALIDATION and f.severity is None for f in rep.findings)
    assert gate(rep) == []                                  # 격하되었으므로 기본 게이트는 통과 — 엄격 모드에서 잡힌다
    assert len(gate(rep, block_on_needs_validation=True)) == 3


def test_from_audit_json_reads_file_and_rejects_non_array(tmp_path):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps([CONFIRMED]), encoding="utf-8")
    assert from_audit_json(p).findings[0].severity == Severity.HIGH
    with pytest.raises(ValueError):
        from_audit_json('{"foo": 1}')


def test_core_does_not_import_a_specific_scanner_product():
    for p in Path("orchestra").rglob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for banned in ("import garak", "from garak", "import langsmith.security"):
            assert banned not in text, (p, banned)
