"""Mock 결제 제공자 + 장부. 멱등성(idempotency_key)을 장부가 직접 보장한다.

실제 PG 어댑터는 같은 메서드를 가진 클래스로 교체한다 (PAYMENT_PROVIDER 설정).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from domains.common.holds import HoldStore


@dataclass
class Intent:
    intent_id: str
    amount: int
    currency: str
    hold_ids: list[str]
    status: str = "created"
    receipt_id: Optional[str] = None
    refunded: int = 0


@dataclass
class MockPaymentProvider:
    holds: HoldStore
    intents: dict[str, Intent] = field(default_factory=dict)
    _by_key: dict[str, Any] = field(default_factory=dict)
    _seq: Any = field(default_factory=lambda: itertools.count(1))

    def status_of(self, intent_id: Optional[str]) -> Optional[str]:
        i = self.intents.get(intent_id or "")
        return i.status if i else None

    def amount_for_holds(self, hold_ids: list[str]) -> int:
        return sum(h.amount for hid in hold_ids if (h := self.holds.get(hid)))

    def _idem(self, key: str, fn):  # 같은 키는 같은 결과를 돌려준다
        if key and key in self._by_key:
            return {**self._by_key[key], "idempotent_replay": True}
        out = fn()
        if key:
            self._by_key[key] = out
        return out

    def create_intent(self, hold_ids: list[str], key: str = "") -> dict:
        def go() -> dict:
            amount = self.amount_for_holds(hold_ids)
            cur = (self.holds.get(hold_ids[0]).currency if hold_ids and self.holds.get(hold_ids[0]) else "KRW")
            iid = f"pi_{next(self._seq):04d}"
            self.intents[iid] = Intent(iid, amount, cur, list(hold_ids))
            return {"intent_id": iid, "amount": amount, "currency": cur, "status": "created", "hold_ids": hold_ids}
        return self._idem(key, go)

    def capture(self, intent_id: str, key: str = "") -> dict:
        def go() -> dict:
            i = self.intents[intent_id]
            i.status, i.receipt_id = "captured", f"rcpt_{intent_id[3:]}"
            return {"intent_id": intent_id, "status": "captured", "receipt_id": i.receipt_id,
                    "amount": i.amount, "currency": i.currency}
        return self._idem(key, go)

    def confirm(self, intent_id: str, key: str = "") -> dict:
        def go() -> dict:
            i = self.intents[intent_id]
            for hid in i.hold_ids:
                self.holds.consume(hid)
            i.status = "confirmed"
            return {"intent_id": intent_id, "status": "confirmed", "receipt_id": i.receipt_id,
                    "bookings": [self.holds.get(h).label for h in i.hold_ids if self.holds.get(h)]}
        return self._idem(key, go)

    def refund(self, intent_id: str, amount: int, reason: str, key: str = "") -> dict:
        def go() -> dict:
            i = self.intents[intent_id]
            i.status, i.refunded = "refunded", amount
            return {"intent_id": intent_id, "status": "refunded", "refunded": amount, "reason": reason}
        return self._idem(key, go)

    def receipt(self, intent_id: str) -> dict:
        i = self.intents.get(intent_id)
        if not i:
            return {"error": "not_found", "intent_id": intent_id}
        return {"intent_id": i.intent_id, "status": i.status, "amount": i.amount, "currency": i.currency,
                "receipt_id": i.receipt_id, "refunded": i.refunded}
