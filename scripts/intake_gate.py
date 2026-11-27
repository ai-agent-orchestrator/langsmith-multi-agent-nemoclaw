#!/usr/bin/env python3
"""플러그인 도입 게이트 CLI: 단위(플러그인)마다 고정 → sha256 → 스캔 보고서를 확인한다.

    python scripts/intake_gate.py --static
    python scripts/intake_gate.py --print-digests
    python scripts/intake_gate.py --reports-dir reports/intake

`--reports-dir` 에는 단위마다 `<source>__<unit>.report.json`(`ScanReport` 형식)을 둔다. 보고서의
digest 는 `--print-digests` 가 보여 주는 값이어야 한다. 보고서가 없으면 불합격이다.
`--static` 은 내장 정적 검사만 돌린다. 전체 취약점 스캔을 대신하지 않는다.
테스트를 돌린 체크아웃에는 `__pycache__` 가 생겨 불합격이 된다. 깨끗한 체크아웃에서 실행한다.
`third_party/` 바로 아래의 파일은 `sync_third_party.py --check` 가 확인한다.
종료 코드: 0 통과 · 1 하나 이상 불합격 · 2 입력 오류.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestra.security.intake import (  # noqa: E402
    IntakeGate,
    check_coverage,
    check_pin,
    manifests_from_lock,
    read_snapshot,
    unit_root,
)
from orchestra.security.scanner import (  # noqa: E402
    Finding,
    ScanReport,
    SecurityScanner,
    StaticScanner,
    files_digest,
    from_audit_json,
)


class ReportFileScanner:
    """운영자가 미리 돌린 보고서를 읽는다. 파일이 없으면 '스캔 안 함' 차단 항목을 낸다."""

    def __init__(self, directory: Path, unit: str) -> None:
        self.path = directory / (unit.replace("/", "__") + ".report.json")

    def scan(self, files: Mapping[str, bytes]) -> ScanReport:
        """보고서의 digest 가 이 바이트와 다르면 예외가 난다."""
        if not self.path.is_file():
            return ScanReport(scanner="report-file", digest=files_digest(files),
                              findings=[Finding(rule="intake.not_scanned", severity="high", path=self.path.name)])
        return from_audit_json(self.path.read_text(encoding="utf-8"), files)


def main(argv: list[str] | None = None) -> int:
    """LOCK.json 의 모든 단위를 판정하고, 잠금에 없는 항목이 있으면 실패한다."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=ROOT / "third_party" / "LOCK.json")
    parser.add_argument("--base", type=Path, default=None, help="source 디렉터리들의 상위 경로 (기본: LOCK.json 위치)")
    parser.add_argument("--unit-dir", default="plugins")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reports-dir", type=Path)
    mode.add_argument("--static", action="store_true", help="내장 정적 검사만")
    mode.add_argument("--print-digests", action="store_true", help="보고서에 넣을 단위별 digest 출력")
    args = parser.parse_args(argv)
    base = args.base or args.lock.parent
    try:
        manifests = manifests_from_lock(args.lock, unit_dir=args.unit_dir)
        if not manifests:
            raise ValueError("LOCK.json has no files")
        uncovered = check_coverage(base, manifests, unit_dir=args.unit_dir)
    except (OSError, ValueError) as exc:
        print(f"input error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    failed = bool(uncovered)
    for problem in uncovered:
        print(f"FAIL {problem}")
    for manifest in manifests:
        root, exclude = unit_root(base, manifest, unit_dir=args.unit_dir)
        if args.print_digests:
            problems, files = read_snapshot(manifest, root, exclude)
            problems = check_pin(manifest) + problems
            failed |= bool(problems)
            print(f"FAIL {manifest.name} — {problems[0]}" if problems else
                  f"DIGEST {manifest.name} {files_digest(files)}")
            continue
        scanner: SecurityScanner = StaticScanner() if args.static else ReportFileScanner(args.reports_dir, manifest.name)
        verdict = IntakeGate(scanner).evaluate(manifest, root, exclude)
        mark = "PASS" if verdict.decision.allowed else "FAIL"
        detail = f" — {verdict.problems[0]}" if verdict.problems else ""
        print(f"{mark} {manifest.name} {verdict.decision.reason_code.value}{detail}")
        failed |= not verdict.decision.allowed
    print("intake gate: FAIL" if failed else "intake gate: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
