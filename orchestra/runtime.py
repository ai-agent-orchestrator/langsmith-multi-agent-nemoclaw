"""한 번의 그래프 빌드/실행이 공유하는 런타임 객체: 이벤트 로그, 승인 장부, 서비스 주입."""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from .hitl import ApprovalRecord
from .ledger import LedgerStore, MemoryLedgerStore, create_ledger_store, LedgerConfigurationError
from .security.control import SecurityControl


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

    def __init__(self, *, capture_trace: bool = False, store: LedgerStore | None = None, scope: str = "runtime") -> None:
        self._events: list[Event] = []
        self._seen: set[str] = set()
        self.capture_trace = capture_trace
        self._explicit_store = store is not None
        self.scope = scope
        self.store = store or MemoryLedgerStore()
        self.bind_store(self.store)

    @property
    def events(self) -> list[Event]:
        """Persistent readers see committed/current-transaction data, including other processes."""
        if self.store.persistent:
            return [Event(e["kind"], e["data"], e["seq"]) for e in self.store.read_events(self.scope)]
        return self._events

    def bind_store(self, store: LedgerStore) -> None:
        """Attach storage before execution and reload an explicitly stable event scope."""
        if self.events and store is not self.store:
            raise LedgerConfigurationError("cannot replace a populated event ledger")
        self.store = store
        self._events = [Event(e["kind"], e["data"], e["seq"]) for e in store.read_events(self.scope)]

    def add(self, kind: str, *, dedupe_key: Optional[str] = None, **data: Any) -> None:
        if dedupe_key is not None and not self.store.persistent:
            if dedupe_key in self._seen:
                return
            self._seen.add(dedupe_key)
        saved = self.store.append_event(self.scope, kind, data, dedupe_key)
        if saved is not None and not self.store.persistent:
            self._events.append(Event(kind, data, seq=saved["seq"]))

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

    def __init__(self, *, store: LedgerStore | None = None) -> None:
        self._granted: set[str] = set()
        self.history: list[dict[str, Any]] = []
        self._records: dict[str, ApprovalRecord] = {}
        self._consumed: set[str] = set()
        self._lock = threading.RLock()
        self._explicit_store = store is not None
        self.store = store or MemoryLedgerStore()

        if self.store.persistent:
            self.history = [e["data"] for e in self.store.read_events("approval-history")]

    def bind_store(self, store: LedgerStore) -> None:
        """Use the same transaction boundary as the local payment provider."""
        if self._records and store is not self.store:
            raise LedgerConfigurationError("cannot replace a populated approval ledger")
        self.store = store
        if store.persistent:
            self.history = [e["data"] for e in store.read_events("approval-history")]

    def is_granted(self, key: str, *, binding: ApprovalRecord | None = None, now: float | None = None) -> bool:
        record = binding or self._records.get(key)
        return bool(record and record.key == key and self.store.authorized(record, time.time() if now is None else now))

    def grant(self, record: ApprovalRecord, *, now: float | None = None) -> bool:
        with self._lock, self.store.transaction():
            key = record.key
            if key in self._consumed or not self.store.grant(record, time.time() if now is None else now):
                return False
            self._records[key] = record.model_copy(update={"decided": True})
            self._granted.add(key)
            self.history.append({"key": key, "decision": True, "by": record.user_id})
            self.store.append_event("approval-history", "approval", {
                "key": self.store.token("approval-history", key), "decision": True,
                "by": self.store.token("user", record.user_id)})
            return True

    def claim(self, record: ApprovalRecord, *, now: float | None = None) -> bool:
        """Atomically consume an authorization immediately before its single execution."""
        with self._lock:
            if not self.store.claim(record, time.time() if now is None else now):
                return False
            self._granted.discard(record.key)
            self._consumed.add(record.key)
            return True

    def deny(self, key: str, by: str = "user", *, binding: ApprovalRecord | None = None) -> None:
        record = binding or self._records.get(key)
        if record is not None:
            self.store.revoke(record)
        self._granted.discard(key)
        self._consumed.add(key)
        self.history.append({"key": key, "decision": False, "by": by})
        self.store.append_event("approval-history", "approval", {
            "key": self.store.token("approval-history", key), "decision": False, "by": self.store.token("user", by)})

    def consume(self, key: str) -> None:
        self._granted.discard(key)
        if key in self._records:
            self._consumed.add(key)
            self.store.revoke(self._records[key])


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
    graph: Any = field(default=None, repr=False)
    quarantined_threads: set[str] = field(default_factory=set, repr=False)
    ledger_store: LedgerStore | None = field(default=None, repr=False)
    # 운영자는 security_control.reset(team) 으로만 열린 팀 차단기를 닫는다.
    security_control: SecurityControl = field(default_factory=SecurityControl, repr=False)

    def __post_init__(self) -> None:
        """Share one explicitly selected store across runtime services; default remains memory."""
        store = self.ledger_store or self.services.get("ledger_store")
        if store is None:
            if self.approvals._explicit_store:
                store = self.approvals.store
            elif self.events._explicit_store:
                store = self.events.store
            else:
                store = create_ledger_store()
        self.ledger_store = store
        self.approvals.bind_store(store)
        self.events.bind_store(store)
        self.services["ledger_store"] = store
