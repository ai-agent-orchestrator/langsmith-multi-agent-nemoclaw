"""도메인 공용 임시 점유(hold) 저장소. travel/concert 가 만들고 payment 가 읽는다 (서로 import 하지 않기 위해)."""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Optional

HOLD_TTL_SEC = 15 * 60


@dataclass
class Hold:
    hold_id: str
    kind: str            # room | flight | car | seat
    ref: str             # 대상 id (hotel_id 등)
    label: str
    amount: int
    currency: str = "KRW"
    expires_at: float = 0.0
    status: str = "held"  # held | consumed | released


class HoldStore:
    def __init__(self, clock: Callable[[], float], ttl: float = HOLD_TTL_SEC):
        self.clock, self.ttl = clock, ttl
        self._holds: dict[str, Hold] = {}
        self._seq = itertools.count(1)

    def create(self, kind: str, ref: str, label: str, amount: int, currency: str = "KRW") -> Hold:
        h = Hold(f"hold_{next(self._seq):04d}", kind, ref, label, amount, currency, self.clock() + self.ttl)
        self._holds[h.hold_id] = h
        return h

    def get(self, hold_id: str) -> Optional[Hold]:
        return self._holds.get(hold_id)

    def is_active(self, hold_id: str) -> bool:
        h = self._holds.get(hold_id)
        return bool(h and h.status == "held" and self.clock() < h.expires_at)

    def consume(self, hold_id: str) -> None:
        if hold_id in self._holds:
            self._holds[hold_id].status = "consumed"

    def release(self, hold_id: str) -> None:
        if hold_id in self._holds and self._holds[hold_id].status == "held":
            self._holds[hold_id].status = "released"


def get_store(services: dict) -> HoldStore:
    """서비스 딕셔너리에서 공용 HoldStore 를 꺼내거나 만든다 (clock 도 공유)."""
    import time

    services.setdefault("clock", time.time)
    if "holds" not in services:
        services["holds"] = HoldStore(services["clock"])
    return services["holds"]
