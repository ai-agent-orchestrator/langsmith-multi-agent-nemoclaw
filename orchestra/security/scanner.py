"""SecurityScanner 계약 + findings 스키마 (R3 / SECURITY_ROADMAP S1).

판정 3단계(confirmed / needs_validation / rejected)와 `from_audit_json` 매핑은 security-audit-skill 의
`skills/security-audit/report-schema.json` 으로 확정했다(R3): findings.json 은 **배열**, 식별자는 `fingerprint`,
심각도는 **confirmed 에만** `severity.overall_severity`(informational~critical)로 붙고 needs_validation/rejected 에는 없다.
어떤 제품(Claude Security, audit skill 등)이든 `SecurityScanner` 를 구현하면 코어가 그대로 쓴다.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from pydantic import BaseModel, Field, model_validator


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


class Verdict(str, Enum):
    CONFIRMED = "confirmed"
    NEEDS_VALIDATION = "needs_validation"
    REJECTED = "rejected"


class Finding(BaseModel):
    id: str
    title: str
    severity: Optional[Severity] = None    # confirmed 에만 존재 (needs_validation/rejected 는 심각도 없음)
    verdict: Verdict
    file: Optional[str] = None
    line: Optional[int] = None
    description: str = ""
    remediation: str = ""
    category: str = ""                     # 예: injection, authz, supply_chain, egress

    @model_validator(mode="after")
    def _verdict_rules(self) -> "Finding":
        if self.verdict == Verdict.CONFIRMED and not self.file:
            raise ValueError(f"{self.id}: confirmed finding must name a file")
        if self.verdict == Verdict.CONFIRMED and self.severity is None:
            raise ValueError(f"{self.id}: confirmed finding must have a severity")
        if self.verdict != Verdict.CONFIRMED and self.severity is not None:
            raise ValueError(f"{self.id}: only confirmed findings carry a severity")
        return self


class FindingsReport(BaseModel):
    scanner: str
    target: str
    findings: list[Finding] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)   # 훑은 경로/영역 (미커버 영역 추적용)

    @model_validator(mode="after")
    def _unique_ids(self) -> "FindingsReport":
        ids = [f.id for f in self.findings]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate finding ids: {sorted(dup)}")
        return self

    def by_verdict(self, v: Verdict) -> list[Finding]:
        return [f for f in self.findings if f.verdict == v]


class SecurityScanner(Protocol):
    name: str

    def scan(self, target: str) -> FindingsReport: ...


class FakeScanner:
    """테스트용: 경로 접두사별로 미리 정한 findings 를 돌려준다."""
    name = "fake"

    def __init__(self, by_prefix: Optional[dict[str, list[Finding]]] = None, coverage: Optional[list[str]] = None):
        self.by_prefix = by_prefix or {}
        self.coverage = coverage or []
        self.calls: list[str] = []

    def scan(self, target: str) -> FindingsReport:
        self.calls.append(target)
        found: list[Finding] = []
        for prefix, items in self.by_prefix.items():
            if target.startswith(prefix):
                found.extend(items)
        return FindingsReport(scanner=self.name, target=target, findings=found, coverage=self.coverage)


def gate(report: FindingsReport, *, fail_on: Severity = Severity.HIGH,
         block_on_needs_validation: bool = False) -> list[Finding]:
    """머지/등록을 막아야 할 findings. confirmed 는 fail_on 이상. needs_validation 은 심각도가 없으므로
    기본은 막지 않고(보고만), `block_on_needs_validation=True` 면 전부 막는다(공급망 인입 같은 엄격 모드). rejected 는 절대 안 막는다."""
    out = []
    for f in report.findings:
        if f.verdict == Verdict.CONFIRMED and f.severity is not None and f.severity.rank >= fail_on.rank:
            out.append(f)
        elif f.verdict == Verdict.NEEDS_VALIDATION and block_on_needs_validation:
            out.append(f)
    return out


_SEV = {"informational": Severity.INFO, "info": Severity.INFO, "low": Severity.LOW, "medium": Severity.MEDIUM,
        "high": Severity.HIGH, "critical": Severity.CRITICAL}


def _location(raw: dict) -> tuple[Optional[str], Optional[int]]:
    """증거 → 싱크(trace 의 kind=sink) → 첫 trace 순으로 위치를 고른다."""
    for item in list(raw.get("evidence") or []):
        if item.get("file"):
            return item["file"], item.get("line")
    trace = list(raw.get("trace") or [])
    for item in [i for i in trace if i.get("kind") == "sink"] + trace:
        if item.get("file"):
            return item["file"], item.get("line")
    return None, None


def from_audit_json(data: Any, *, scanner: str = "security-audit-skill", target: str = ".",
                    coverage: Optional[list[str]] = None) -> FindingsReport:
    """security-audit-skill 의 findings.json(배열; 문자열/경로/리스트 모두 허용) → FindingsReport.
    알 수 없는 verdict 는 needs_validation 으로 격하하고, 위치가 없는 confirmed 도 격하한다(fail-safe)."""
    if isinstance(data, Path):
        data = json.loads(data.read_text(encoding="utf-8"))
    elif isinstance(data, str):
        data = json.loads(data)
    items = data["findings"] if isinstance(data, dict) and "findings" in data else data
    if not isinstance(items, list):
        raise ValueError("findings.json must be an array")
    out: list[Finding] = []
    for i, raw in enumerate(items):
        verdict = str(raw.get("verdict", "needs_validation")).lower()
        if verdict not in {v.value for v in Verdict}:
            verdict = Verdict.NEEDS_VALIDATION.value
        file, line = _location(raw)
        sev: Optional[Severity] = None
        if verdict == Verdict.CONFIRMED.value:
            overall = ((raw.get("severity") or {}).get("overall_severity") or "").lower()
            sev = _SEV.get(overall)
            if not file or sev is None:                    # 위치/심각도 없는 confirmed 는 인정하지 않는다
                verdict = Verdict.NEEDS_VALIDATION.value
                sev = None
        remediation = raw.get("remediation")
        out.append(Finding(
            id=str(raw.get("fingerprint") or raw.get("id") or f"F{i + 1:03d}"),
            title=str(raw.get("title", "")), severity=sev, verdict=verdict, file=file, line=line,
            description=str(raw.get("description", "")),
            remediation=str(remediation.get("strategy", "")) if isinstance(remediation, dict) else str(remediation or ""),
            category=str(raw.get("category", ""))))
    return FindingsReport(scanner=scanner, target=target, findings=out, coverage=list(coverage or []))
