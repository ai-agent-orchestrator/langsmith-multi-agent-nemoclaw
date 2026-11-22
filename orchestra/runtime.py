"""한 번의 그래프 빌드/실행이 공유하는 런타임 객체: 이벤트 로그, 승인 장부, 서비스 주입."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Event:
    kind: str                 # llm | tool | filter | approval | sandbox | blocked | feedback
    data: dict[str, Any]
    seq: int = 0


class EventLog:
    """순서 있는 이벤트 기록 (시뮬레이션 출력·골든 테스트·LangSmith feedback 의 원천).

    interrupt 재개 시 툴 노드가 처음부터 다시 실행되므로, 재실행에 의해 중복될 수 있는
    이벤트는 dedupe_key 를 줘서 한 번만 기록한다.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._seen: set[str] = set()

    def add(self, kind: str, *, dedupe_key: Optional[str] = None, **data: Any) -> None:
        if dedupe_key is not None:
            if dedupe_key in self._seen:
                return
            self._seen.add(dedupe_key)
        self.events.append(Event(kind, data, seq=len(self.events)))

    def of(self, *kinds: str) -> list[Event]:
        return [e for e in self.events if e.kind in kinds]

    def feedback(self) -> dict[str, float]:
        """filter 결정이 남긴 feedback 키의 최솟값 (하나라도 0이면 0)."""
        out: dict[str, float] = {}
        for e in self.of("feedback"):
            for k, v in e.data.items():
                out[k] = min(out.get(k, 1.0), v)
        return out


class ApprovalLedger:
    """승인 장부. 승인은 1회용: 실행 후 consume 한다 (재사용·재생 방지)."""

    def __init__(self) -> None:
        self._granted: set[str] = set()
        self.history: list[dict[str, Any]] = []

    def is_granted(self, key: str) -> bool:
        return key in self._granted

    def grant(self, key: str, by: str = "user") -> None:
        self._granted.add(key)
        self.history.append({"key": key, "decision": True, "by": by})

    def deny(self, key: str, by: str = "user") -> None:
        self._granted.discard(key)
        self.history.append({"key": key, "decision": False, "by": by})

    def consume(self, key: str) -> None:
        self._granted.discard(key)


@dataclass
class Runtime:
    org: str = ""
    security_mode: str = "masked"
    sandbox_mode: str = "off"
    events: EventLog = field(default_factory=EventLog)
    approvals: ApprovalLedger = field(default_factory=ApprovalLedger)
    sandbox: Any = None                              # SandboxRunner | None
    services: dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    thread_id: str = "default"
