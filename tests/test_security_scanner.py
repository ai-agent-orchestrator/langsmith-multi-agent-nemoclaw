import json

import pytest
from pydantic import ValidationError

from orchestra.security.scanner import (FakeScanner, Finding, FindingsReport, Severity, Verdict,
                                        from_audit_json, gate)


def F(id, sev, verdict, file="a.py"):
    return Finding(id=id, title=id, severity=sev, verdict=verdict, file=file)


def test_confirmed_requires_file():
    with pytest.raises(ValidationError):
        Finding(id="x", title="t", severity="high", verdict="confirmed")


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError):
        FindingsReport(scanner="s", target=".", findings=[F("a", "low", "confirmed"), F("a", "low", "confirmed")])


def test_gate_blocks_confirmed_high_not_rejected():
    rep = FakeScanner({"src": [F("a", "high", "confirmed"), F("b", "critical", "rejected"),
                               F("c", "medium", "confirmed"), F("d", "critical", "needs_validation"),
                               F("e", "high", "needs_validation")]}).scan("src/x")
    assert sorted(f.id for f in gate(rep)) == ["a", "d"]
    assert [f.id for f in gate(rep, block_unvalidated_at=None)] == ["a"]
    assert sorted(f.id for f in gate(rep, fail_on=Severity.MEDIUM)) == ["a", "c", "d"]


def test_fake_scanner_prefix_and_calls():
    s = FakeScanner({"src": [F("a", "low", "confirmed")]})
    assert s.scan("docs").findings == [] and s.scan("src").findings[0].id == "a" and s.calls == ["docs", "src"]


def test_from_audit_json_is_fail_safe():
    raw = {"findings": [
        {"id": "F1", "title": "t", "severity": "HIGH", "verdict": "confirmed", "file": "x.py", "line": 3},
        {"id": "F2", "title": "t", "severity": "weird", "verdict": "maybe", "file": "y.py"},
        {"id": "F3", "title": "t", "severity": "critical", "verdict": "confirmed"},   # 위치 없음 → 격하
    ], "coverage": ["src/"]}
    rep = from_audit_json(json.dumps(raw))
    by = {f.id: f for f in rep.findings}
    assert by["F1"].verdict == Verdict.CONFIRMED and by["F1"].severity == Severity.HIGH
    assert by["F2"].verdict == Verdict.NEEDS_VALIDATION and by["F2"].severity == Severity.MEDIUM
    assert by["F3"].verdict == Verdict.NEEDS_VALIDATION
    assert rep.coverage == ["src/"]
    assert [f.id for f in gate(rep)] == ["F1", "F3"]       # F3 은 critical + needs_validation → 막음


def test_core_does_not_import_a_specific_scanner_product():
    from pathlib import Path
    for p in Path("orchestra").rglob("*.py"):
        t = p.read_text(encoding="utf-8").lower()
        assert "garak" not in t.replace("garak 를", "") or p.name in {"scanner.py"}, p
