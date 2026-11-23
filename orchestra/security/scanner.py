"""SecurityScanner 계약 + findings 스키마 (R3 / SECURITY_ROADMAP S1).

판정 3단계(confirmed / needs_validation / rejected)는 security-audit-skill 의 출력 모델을 참고했다.
`[VERIFY]` 실제 findings.json 필드명은 해당 저장소 report-schema.json 으로 대조해 `from_audit_json` 매핑을 확정할 것.
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
    severity: Severity
    verdict: Verdict
    file: Optional[str] = None
    line: Optional[int] = None
    description: str = ""
    remediation: str = ""
    category: str = ""                     # 예: injection, authz, supply_chain, egress

    @model_validator(mode="after")
    def _confirmed_needs_location(self) -> "Finding":
        if self.verdict == Verdict.CONFIRMED and not self.file:
            raise ValueError(f"{self.id}: confirmed finding must name a file")
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
         block_unvalidated_at: Optional[Severity] = Severity.CRITICAL) -> list[Finding]:
    """머지/등록을 막아야 할 findings. confirmed 는 fail_on 이상, needs_validation 은 더 엄격한 기준에서만 막는다.
    rejected 는 절대 막지 않는다(오탐으로 판정된 것)."""
    blocking = []
    for f in report.findings:
        if f.verdict == Verdict.CONFIRMED and f.severity.rank >= fail_on.rank:
            blocking.append(f)
        elif (f.verdict == Verdict.NEEDS_VALIDATION and block_unvalidated_at is not None
              and f.severity.rank >= block_unvalidated_at.rank):
            blocking.append(f)
    return blocking


def from_audit_json(data: dict[str, Any] | str | Path, *, scanner: str = "audit-skill",
                    target: str = ".") -> FindingsReport:
    """findings.json(dict/문자열/경로) → FindingsReport. 알 수 없는 verdict 는 needs_validation 으로 격하(fail-safe 쪽).
    `[VERIFY]` 키 이름(`findings`, `verdict`, `severity`, `file`, `line`)을 실제 스키마와 대조."""
    if isinstance(data, Path):
        data = json.loads(data.read_text(encoding="utf-8"))
    elif isinstance(data, str):
        data = json.loads(data)
    items: Iterable[dict] = data.get("findings", []) if isinstance(data, dict) else data
    out = []
    for i, raw in enumerate(items):
        verdict = str(raw.get("verdict", "needs_validation")).lower()
        if verdict not in {v.value for v in Verdict}:
            verdict = Verdict.NEEDS_VALIDATION.value
        sev = str(raw.get("severity", "medium")).lower()
        if sev not in {s.value for s in Severity}:
            sev = Severity.MEDIUM.value
        file = raw.get("file") or raw.get("path")
        if verdict == Verdict.CONFIRMED.value and not file:
            verdict = Verdict.NEEDS_VALIDATION.value      # 위치 없는 confirmed 는 인정하지 않는다
        out.append(Finding(id=str(raw.get("id", f"F{i+1:03d}")), title=str(raw.get("title", "")),
                           severity=sev, verdict=verdict, file=file, line=raw.get("line"),
                           description=str(raw.get("description", "")),
                           remediation=str(raw.get("remediation", "")),
                           category=str(raw.get("category", ""))))
    return FindingsReport(scanner=scanner, target=target, findings=out,
                          coverage=list(data.get("coverage", [])) if isinstance(data, dict) else [])
