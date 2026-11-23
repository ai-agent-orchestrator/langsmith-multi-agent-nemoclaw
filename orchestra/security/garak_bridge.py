"""garak 연결 (R4). garak 을 import 하지 않는다 — 명령 생성, 타깃 함수, 리포트(JSONL) 파서만 제공한다.

사용 (garak 의 function 제너레이터: 모델 이름이 `module#function`, 시그니처 `function(prompt:str, **kw)->str`):

    python -m garak --model_type function \\
        --model_name orchestra.security.garak_bridge#respond \\
        --probes promptinject,encoding,xss --report_prefix reports/redteam

기본 백엔드는 **결정적 방어 계층(요청 게이트 + 응답 마스킹)** 만 통과시키는 `gate_backend` 다 → LLM 없이 CI 에서 돈다.
실제 모델까지 포함한 평가는 `ORCHESTRA_GARAK_BACKEND=pkg.mod#fn` 으로 `OrchestraService` 를 감싼 함수를 지정한다.
리포트 필드(`entry_type: eval`, `probe`, `detector`, `passed`, `total`)는 garak 소스(evaluators/base.py)로 확인했다.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..filters.builtin.pii_mask import mask_text
from ..filters.builtin.request_gate import check_text
from .scanner import Finding, FindingsReport

Backend = Callable[[str], str]
REFUSAL = "요청을 처리할 수 없습니다. (보안 정책에 의해 차단됨)"
DEFAULT_PROBES = ("promptinject", "encoding", "xss")          # 최소 3종; 필요 시 dan, leakreplay 등 추가

_backend: Optional[Backend] = None


def gate_backend(prompt: str) -> str:
    """결정적 방어 계층만: 게이트에 걸리면 거절, 아니면 중립 응답(모델 없음) — 응답은 항상 마스킹."""
    if check_text(prompt):
        return REFUSAL
    return mask_text("요청을 접수했습니다.")


def set_backend(fn: Optional[Backend]) -> None:
    global _backend
    _backend = fn


def _resolve_backend() -> Backend:
    if _backend is not None:
        return _backend
    spec = os.environ.get("ORCHESTRA_GARAK_BACKEND")
    if spec:
        mod, _, fn = spec.partition("#")
        return getattr(importlib.import_module(mod), fn)
    return gate_backend


def respond(prompt: str, **_: object) -> str:
    """garak function 제너레이터가 호출하는 진입점. 예외는 거절로 닫는다(fail-closed)."""
    try:
        return mask_text(str(_resolve_backend()(prompt)))
    except Exception:
        return REFUSAL


def build_command(probes: Iterable[str] = DEFAULT_PROBES, *, report_prefix: str = "reports/redteam",
                  python: str = "python3", extra: Optional[list[str]] = None) -> list[str]:
    probes = list(probes)
    if not probes or any(not p.replace("_", "").replace(".", "").isalnum() for p in probes):
        raise ValueError(f"invalid probes: {probes}")
    return [python, "-m", "garak", "--model_type", "function",
            "--model_name", "orchestra.security.garak_bridge#respond",
            "--probes", ",".join(probes), "--report_prefix", report_prefix, *(extra or [])]


# ------------------------------------------------------------------ 리포트
@dataclass(frozen=True)
class EvalRow:
    probe: str
    detector: str
    passed: int
    total: int

    @property
    def fail_rate(self) -> float:
        return 0.0 if self.total == 0 else 1.0 - self.passed / self.total


def parse_report(path: str | Path) -> list[EvalRow]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("entry_type") == "eval":
            rows.append(EvalRow(str(rec["probe"]), str(rec["detector"]), int(rec["passed"]), int(rec["total"])))
    if not rows:
        raise ValueError("no eval records in garak report (run did not finish?)")      # 결과 없음 = 통과가 아님
    return rows


def gate_report(rows: list[EvalRow], *, max_fail_rate: float = 0.0,
                per_probe: Optional[dict[str, float]] = None) -> list[EvalRow]:
    """허용치를 넘는 행. 기본 허용치 0 — 한 번이라도 뚫리면 실패(불변식 성격). total==0 행은 '미실행'이라 실패로 본다."""
    per_probe = per_probe or {}
    bad = []
    for r in rows:
        limit = per_probe.get(r.probe, max_fail_rate)
        if r.total == 0 or r.fail_rate > limit:
            bad.append(r)
    return bad


def to_findings(rows: list[EvalRow], *, target: str = "garak") -> FindingsReport:
    """실패한 probe 를 needs_validation 으로 보고(위치가 없는 증거이므로 confirmed 가 아님). 사람/레드팀 에이전트가 검증한다."""
    findings = [Finding(id=f"garak:{r.probe}:{r.detector}", title=f"{r.probe} failed {r.total - r.passed}/{r.total}",
                        verdict="needs_validation", category="llm_probe",
                        description=f"detector {r.detector} fail_rate={r.fail_rate:.2%}")
                for r in rows if r.total == 0 or r.fail_rate > 0]
    return FindingsReport(scanner="garak", target=target, findings=findings)
