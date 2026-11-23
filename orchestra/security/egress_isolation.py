"""egress 거부 → 자동 격리 + 사람 리뷰 (SECURITY_ROADMAP ① NC-5 / NS5).

`IsolatingSandboxRunner` 는 어떤 SandboxRunner 든 감싸는 데코레이터다 (tool_wrapper 수정 없음):
- 같은 샌드박스에서 `window_s` 안에 egress 거부가 `threshold` 회 이상 → 그 샌드박스를 격리한다.
- 격리된 샌드박스로의 실행 요청은 내부 러너를 **호출하지 않고** `sandbox_isolated` 로 실패한다 (fail closed).
- 격리 해제는 `release()` 로만, 사람(by)과 사유가 필요하다. 에이전트용 툴로 노출하지 않는다.
- 격리/해제는 리뷰 큐와 (선택) 감사 로그·이벤트 로그에 남는다. 호스트 목록은 원문 대신 그대로 두되
  (host:port 는 정책 식별자라 PII 가 아니다) 요청 인자는 기록하지 않는다.

왜 샌드박스 단위인가: egress 정책이 샌드박스 단위로 적용되고, 같은 샌드박스의 다른 툴도 같은 경로로
데이터를 내보낼 수 있으므로 툴 하나만 막는 것은 우회 가능하다.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..sandbox.base import ExecRequest, ExecResult, SandboxRunner
from .decision import Decision, ReasonCode, allow, deny

ISOLATED_ERROR = "sandbox_isolated"


@dataclass
class ReviewItem:
    sandbox: str
    opened_at: float
    denied_hosts: list[str]
    count: int
    status: str = "open"             # open | released
    released_by: str | None = None
    release_reason: str = ""


@dataclass
class EgressIsolationGuard:
    """순수 로직: 거부 기록·임계치·격리 상태. 시계는 주입 가능 (테스트 결정성)."""

    threshold: int = 3
    window_s: float = 300.0
    clock: Callable[[], float] = time.time
    on_event: Callable[[str, dict[str, Any]], None] | None = None   # (kind, data) → EventLog/AuditLog
    _denials: dict[str, deque] = field(default_factory=dict, repr=False)
    isolated: dict[str, ReviewItem] = field(default_factory=dict)
    review_queue: list[ReviewItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.window_s <= 0:
            raise ValueError("window_s must be > 0")

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001, S110 — 기록 실패가 격리 판단을 바꾸지 않게
            pass

    def is_isolated(self, sandbox: str) -> bool:
        return sandbox in self.isolated

    def check(self, sandbox: str) -> Decision:
        if self.is_isolated(sandbox):
            return deny(ReasonCode.EGRESS_DENIED_ISOLATED, sandbox, by="egress_isolation")
        return allow("egress_isolation")

    def record_denial(self, sandbox: str, hosts: list[str]) -> bool:
        """거부 1건 기록. 이번 기록으로 격리되었으면 True."""
        now = self.clock()
        q = self._denials.setdefault(sandbox, deque())
        q.append((now, tuple(hosts)))
        while q and now - q[0][0] > self.window_s:
            q.popleft()
        self._emit("sandbox", {"sandbox": sandbox, "egress_denied": list(hosts), "recent_denials": len(q)})
        if self.is_isolated(sandbox) or len(q) < self.threshold:
            return False
        seen: list[str] = []
        for _, hs in q:
            seen.extend(h for h in hs if h not in seen)
        item = ReviewItem(sandbox=sandbox, opened_at=now, denied_hosts=seen, count=len(q))
        self.isolated[sandbox] = item
        self.review_queue.append(item)
        self._emit("blocked", {"by": "egress_isolation", "reason_code": ReasonCode.EGRESS_DENIED_ISOLATED.value,
                               "sandbox": sandbox, "count": len(q)})
        return True

    def release(self, sandbox: str, *, by: str, reason: str) -> bool:
        """사람이 검토 후 해제. by/reason 없으면 거부 (누가 왜 풀었는지 남지 않는 해제 금지)."""
        if not by.strip() or not reason.strip():
            raise ValueError("release requires a human 'by' and a 'reason'")
        item = self.isolated.pop(sandbox, None)
        if item is None:
            return False
        item.status, item.released_by, item.release_reason = "released", by, reason
        self._denials.pop(sandbox, None)          # 해제 후 카운트는 새로 시작
        self._emit("approval", {"kind": "egress_isolation_release", "sandbox": sandbox, "by": by})
        return True

    def open_reviews(self) -> list[ReviewItem]:
        return [i for i in self.review_queue if i.status == "open"]


class IsolatingSandboxRunner:
    """SandboxRunner 데코레이터. `rt.sandbox = IsolatingSandboxRunner(runner, guard)` 로 끼운다."""

    def __init__(self, inner: SandboxRunner, guard: EgressIsolationGuard | None = None):
        self.inner = inner
        self.guard = guard or EgressIsolationGuard()

    def exec(self, req: ExecRequest) -> ExecResult:
        if self.guard.is_isolated(req.sandbox):
            return ExecResult(ok=False, error=ISOLATED_ERROR)
        res = self.inner.exec(req)
        if res.egress_denied:
            self.guard.record_denial(req.sandbox, list(res.egress_denied))
        return res

    def status(self, sandbox: str) -> dict[str, Any]:
        out = dict(self.inner.status(sandbox))
        out["isolated"] = self.guard.is_isolated(sandbox)
        return out
