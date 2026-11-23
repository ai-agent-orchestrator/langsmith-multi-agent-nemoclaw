#!/usr/bin/env python3
"""보안 게이트 (O4): garak 리포트와 security-audit findings.json 을 읽어 합격/불합격(종료 코드)을 낸다.

  python scripts/security_gate.py --garak reports/redteam.report.jsonl --findings findings.json --fail-on high
종료 코드: 0 통과 · 1 게이트 불합격 · 2 입력 오류(파일 없음/형식 오류/결과 없음 = 통과로 보지 않는다).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.security import garak_bridge as g          # noqa: E402
from orchestra.security.scanner import Severity, from_audit_json, gate   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--garak", help="garak report.jsonl")
    ap.add_argument("--max-fail-rate", type=float, default=0.0)
    ap.add_argument("--findings", help="security-audit findings.json (배열)")
    ap.add_argument("--fail-on", default="high", choices=[s.value for s in Severity])
    ap.add_argument("--strict-unvalidated", action="store_true", help="needs_validation 도 불합격 처리")
    a = ap.parse_args(argv)
    if not a.garak and not a.findings:
        print("nothing to check: pass --garak and/or --findings", file=sys.stderr)
        return 2
    failed = False
    try:
        if a.garak:
            bad = g.gate_report(g.parse_report(a.garak), max_fail_rate=a.max_fail_rate)
            for r in bad:
                print(f"GARAK FAIL {r.probe} {r.detector}: {r.total - r.passed}/{r.total} ({r.fail_rate:.1%})")
            failed |= bool(bad)
        if a.findings:
            blocking = gate(from_audit_json(Path(a.findings)), fail_on=Severity(a.fail_on),
                            block_on_needs_validation=a.strict_unvalidated)
            for f in blocking:
                print(f"FINDING {f.verdict.value} {f.severity.value if f.severity else '-'} {f.id} {f.file or ''}:{f.line or ''}")
            failed |= bool(blocking)
    except (OSError, ValueError, KeyError) as exc:
        print(f"input error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("security gate: FAIL" if failed else "security gate: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
