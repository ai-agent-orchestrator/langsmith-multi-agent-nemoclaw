"""egress 거부 → 샌드박스 자동 격리 + 사람 리뷰.

`IsolatingSandboxRunner` 는 어떤 SandboxRunner 든 감싸는 데코레이터다:
- 같은 샌드박스에서 `window_s` 안에 egress 거부가 `threshold` 회 이상이면 그 샌드박스를 격리한다.
- 격리된 샌드박스의 실행 요청은 내부 러너를 **호출하지 않고** `sandbox_isolated` 로 실패한다.
- 해제는 `release()` 로만 하며 사람(by)과 사유가 필요하다. 에이전트 툴로 노출하지 않는다.
- 요청 인자는 기록하지 않는다. host:port 는 정책 식별자라 그대로 남긴다.

샌드박스 단위로 막는 이유: egress 정책이 샌드박스 단위라, 툴 하나만 막으면 같은 샌드박스의 다른
툴로 같은 경로를 쓸 수 있다.

격리 상태는 프로세스 메모리에만 있다. 프로세스를 다시 띄우면 해제된다.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..sandbox.base import ExecRequest, ExecResult, SandboxRunner
from .decision import Decision, ReasonCode, allow, deny

ISOLATED_ERROR = "sandbox_isolated"
_Event = tuple[str, dict[str, Any]]


@dataclass
class ReviewItem:
    sandbox: str
    opened_at: float                 # guard.clock 의 값
    denied_hosts: list[str]
    count: int
    status: str = "open"             # open | released
    released_by: str | None = None
    release_reason: str = ""


@dataclass
class EgressIsolationGuard:
    """거부 기록·임계치·격리 상태. 시계는 주입할 수 있다.

    `on_event(kind, data)` 는 이벤트 로그에 그대로 연결한다:
    `on_event=lambda kind, data: runtime.events.add(kind, **data)`.
    """

    threshold: int = 3
    window_s: float = 300.0
    clock: Callable[[], float] = time.monotonic      # 벽시계를 앞당겨 창을 비우지 못하게
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    _denials: dict[str, deque[tuple[float, tuple[str, ...]]]] = field(default_factory=dict, repr=False)
    isolated: dict[str, ReviewItem] = field(default_factory=dict)
    review_queue: list[ReviewItem] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if not self.window_s > 0:
            raise ValueError("window_s must be > 0")

    def _emit(self, events: list[_Event]) -> None:
        # 잠금 밖에서 부른다: 기록 대상이 자체 잠금·트랜잭션을 가질 수 있다.
        if self.on_event is None:
            return
        for kind, data in events:
            try:
                self.on_event(kind, data)
            except Exception:  # noqa: BLE001, S110 - 기록 실패가 격리 판단을 바꾸지 않게
                pass

    def is_isolated(self, sandbox: str) -> bool:
        """지금 격리 중인지."""
        with self._lock:
            return sandbox in self.isolated

    def check(self, sandbox: str) -> Decision:
        """결정 계약 형태의 격리 여부."""
        if self.is_isolated(sandbox):
            return deny(ReasonCode.EGRESS_DENIED_ISOLATED, sandbox, by="egress_isolation")
        return allow("egress_isolation")

    def record_denial(self, sandbox: str, hosts: list[str]) -> bool:
        """거부 1건을 기록한다. 이번 기록으로 격리되었으면 True."""
        isolated_now = False
        with self._lock:
            now = self.clock()
            queue = self._denials.setdefault(sandbox, deque())
            queue.append((now, tuple(hosts)))
            while queue and now - queue[0][0] > self.window_s:
                queue.popleft()
            events: list[_Event] = [("egress_isolation", {
                "action": "denial", "sandbox": sandbox, "egress_denied": list(hosts), "recent_denials": len(queue)})]
            if sandbox not in self.isolated and len(queue) >= self.threshold:
                seen = list(dict.fromkeys(host for _, denied in queue for host in denied))
                item = ReviewItem(sandbox=sandbox, opened_at=now, denied_hosts=seen, count=len(queue))
                self.isolated[sandbox] = item
                self.review_queue.append(item)
                events.append(("blocked", {"by": "egress_isolation", "reason": ISOLATED_ERROR,
                                           "sandbox": sandbox, "count": len(queue)}))
                isolated_now = True
        self._emit(events)
        return isolated_now

    def release(self, sandbox: str, *, by: str, reason: str) -> bool:
        """사람이 검토한 뒤 해제한다. 누가 왜 풀었는지 없는 해제는 거부한다."""
        if not by.strip() or not reason.strip():
            raise ValueError("release requires a human 'by' and a 'reason'")
        with self._lock:
            item = self.isolated.pop(sandbox, None)
            if item is None:
                return False
            item.status, item.released_by, item.release_reason = "released", by, reason
            self._denials.pop(sandbox, None)          # 해제 후에는 처음부터 다시 센다
        self._emit([("egress_isolation", {"action": "release", "sandbox": sandbox, "by": by})])
        return True

    def open_reviews(self) -> list[ReviewItem]:
        """아직 해제되지 않은 리뷰 항목."""
        with self._lock:
            return [item for item in self.review_queue if item.status == "open"]


class IsolatingSandboxRunner:
    """SandboxRunner 데코레이터: `runtime.sandbox = IsolatingSandboxRunner(runner, guard)`."""

    def __init__(self, inner: SandboxRunner, guard: EgressIsolationGuard | None = None) -> None:
        self.inner = inner
        self.guard = guard or EgressIsolationGuard()

    def exec(self, req: ExecRequest) -> ExecResult:
        """격리 중이면 내부 러너를 부르지 않는다."""
        if self.guard.is_isolated(req.sandbox):
            return ExecResult(ok=False, error=ISOLATED_ERROR)
        result = self.inner.exec(req)
        if result.egress_denied:
            self.guard.record_denial(req.sandbox, list(result.egress_denied))
        return result

    def status(self, sandbox: str) -> dict[str, Any]:
        """내부 러너 상태에 격리 여부를 더한다."""
        return {**self.inner.status(sandbox), "isolated": self.guard.is_isolated(sandbox)}
