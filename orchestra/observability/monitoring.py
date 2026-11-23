"""LangSmith 트레이스 기반 병목 진단: 노드별 토큰 / 지연(P50·P95·P99) / 에러율 → 튜닝 액션.

summarize_runs / diagnose 는 순수 함수라 LangSmith 연결 없이 테스트 가능하다.
fetch_runs 만 LangSmith(클라우드 또는 self-hosted)에 접속한다.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# 진단 임계값 (프로젝트에 맞게 조정)
SLOW_P95_SEC = 5.0
TOKEN_SHARE_WARN = 0.40
TAIL_RATIO_WARN = 3.0  # p99 / p50
ERROR_RATE_WARN = 0.10


@dataclass
class NodeStats:
    name: str
    calls: int = 0
    total_tokens: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    def pct(self, p: float) -> float:
        """nearest-rank 백분위수 (초)."""
        if not self.latencies:
            return 0.0
        xs = sorted(self.latencies)
        return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)]


def summarize_runs(runs: Iterable[Any]) -> dict[str, NodeStats]:
    """run.name 별 집계. run 은 name/start_time/end_time/total_tokens/error 속성을 가진 객체."""
    stats: dict[str, NodeStats] = defaultdict(lambda: NodeStats(""))
    for r in runs:
        s = stats[r.name]
        s.name = r.name
        s.calls += 1
        s.total_tokens += int(getattr(r, "total_tokens", 0) or 0)
        if getattr(r, "error", None):
            s.errors += 1
        if r.start_time and r.end_time:
            s.latencies.append((r.end_time - r.start_time).total_seconds())
    return dict(stats)


def diagnose(stats: dict[str, NodeStats]) -> list[tuple[str, str]]:
    """(노드, 튜닝 액션) 목록."""
    total = sum(s.total_tokens for s in stats.values()) or 1
    out: list[tuple[str, str]] = []
    for s in stats.values():
        if s.pct(95) >= SLOW_P95_SEC:
            out.append((s.name, f"P95 {s.pct(95):.1f}s: 프롬프트 길이 축소 또는 asyncio 병렬 실행으로 전환"))
        if s.total_tokens / total >= TOKEN_SHARE_WARN:
            out.append((s.name, f"전체 토큰의 {s.total_tokens / total:.0%}: State 에 전체 대화 대신 직전 요약본만 전달 / 프롬프트 압축"))
        if s.pct(50) > 0 and s.pct(99) / s.pct(50) >= TAIL_RATIO_WARN:
            out.append((s.name, f"P99/P50 = {s.pct(99) / s.pct(50):.1f}배: recursion_limit·타임아웃 설정, 무거운 추론 노드는 경량 모델로 교체"))
        if s.error_rate >= ERROR_RATE_WARN:
            out.append((s.name, f"에러율 {s.error_rate:.0%}: 툴 파라미터 스키마 설명 보완 및 예외 처리 가드레일 추가"))
    return out


def render_report(stats: dict[str, NodeStats]) -> str:
    total = sum(s.total_tokens for s in stats.values()) or 1
    lines = [
        "| 노드 | 호출 | 토큰 | 토큰 비중 | P50 | P95 | P99 | 에러율 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sorted(stats.values(), key=lambda x: x.total_tokens, reverse=True):
        lines.append(
            f"| {s.name} | {s.calls} | {s.total_tokens:,} | {s.total_tokens / total:.0%} "
            f"| {s.pct(50):.1f}s | {s.pct(95):.1f}s | {s.pct(99):.1f}s | {s.error_rate:.0%} |"
        )
    actions = diagnose(stats)
    if actions:
        lines += ["", "튜닝 포인트:"] + [f"- **{n}**: {a}" for n, a in actions]
    return "\n".join(lines)


def fetch_runs(project: str, *, hours: int = 24, pattern: Optional[str] = None, limit: int = 2000,
               client: Any = None, filter: Optional[str] = None):
    """LangSmith 에서 최근 run 조회. pattern 을 주면 `pattern:<name>` 태그로 구조별 필터링."""
    from .client import make_client
    from .safe import safe_call

    client = client or make_client()
    flt = filter or (f'has(tags, "pattern:{pattern}")' if pattern else None)
    return safe_call(lambda: list(client.list_runs(
        project_name=project,
        start_time=datetime.now(timezone.utc) - timedelta(hours=hours),
        filter=flt,
        limit=limit,
    )), default=[])
