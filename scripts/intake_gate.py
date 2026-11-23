#!/usr/bin/env python3
"""플러그인 인입 게이트 CLI (SECURITY_ROADMAP ③ SA-1).

  python scripts/intake_gate.py --lock third_party/LOCK.json --base third_party --findings-dir reports/intake
각 단위(플러그인)마다 pin → sha256 → findings 를 확인한다. findings 는 security-audit-skill 의
`findings.json` 을 `<findings-dir>/<source>__<unit>.findings.json` 으로 둔다 (`/` 대신 `__`).
findings 파일이 없으면 엄격 모드에서 불합격이다 (스캔 안 한 플러그인 = 통과 아님).
종료 코드: 0 통과 · 1 하나 이상 불합격 · 2 입력 오류.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.security.intake import (
    IntakeGate,
    manifests_from_lock,
    unit_root,
)
from orchestra.security.scanner import (
    Finding,
    FindingsReport,
    Severity,
    Verdict,
    from_audit_json,
)


class FindingsDirScanner:
    """스캔 결과 파일을 읽는 스캐너 어댑터. 파일이 없으면 needs_validation 1건 (엄격 모드에서 차단)."""

    name = "findings-dir"

    def __init__(self, directory: Path, unit: str):
        self.path = directory / (unit.replace("/", "__") + ".findings.json")

    def scan(self, target: str) -> FindingsReport:
        if not self.path.is_file():
            return FindingsReport(scanner=self.name, target=target, findings=[Finding(
                id="NOT_SCANNED", title="no findings.json for this unit", verdict=Verdict.NEEDS_VALIDATION)])
        return from_audit_json(self.path, target=target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--base", required=True, help="LOCK.json 의 source 디렉터리들이 있는 상위 경로")
    ap.add_argument("--findings-dir", required=True)
    ap.add_argument("--unit-dir", default="plugins")
    ap.add_argument("--fail-on", default="high", choices=[s.value for s in Severity])
    ap.add_argument("--lenient-unvalidated", action="store_true", help="needs_validation 을 통과시킨다 (비권장)")
    a = ap.parse_args(argv)
    try:
        manifests = manifests_from_lock(Path(a.lock), unit_dir=a.unit_dir)
    except (OSError, ValueError) as exc:
        print(f"input error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not manifests:
        print("input error: LOCK.json has no files", file=sys.stderr)
        return 2
    failed = False
    for m in manifests:
        root, exclude = unit_root(Path(a.base), m, unit_dir=a.unit_dir)
        g = IntakeGate(FindingsDirScanner(Path(a.findings_dir), m.name), fail_on=Severity(a.fail_on),
                       strict_unvalidated=not a.lenient_unvalidated)
        v = g.evaluate(m, root, exclude)
        mark = "PASS" if v.decision.allowed else "FAIL"
        print(f"{mark} {m.name} {v.decision.reason_code.value}" + (f" — {v.problems[0]}" if v.problems else ""))
        failed |= not v.decision.allowed
    print("intake gate: FAIL" if failed else "intake gate: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
