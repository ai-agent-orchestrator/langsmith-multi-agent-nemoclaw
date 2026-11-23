"""Mock 결제 제공자 + 장부. 멱등성(idempotency_key)을 장부가 직접 보장한다.

실제 PG 어댑터는 같은 메서드를 가진 클래스로 교체한다 (PAYMENT_PROVIDER 설정).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from typing import Any, Optional, Callable

from domains.common.holds import HoldStore
from orchestra.ledger import LedgerStore, MemoryLedgerStore, LedgerConfigurationError


@dataclass
class Intent:
    intent_id: str
    amount: int
    currency: str
    hold_ids: list[str]
    status: str = "created"
    receipt_id: Optional[str] = None
    refunded: int = 0


class MockPaymentProvider:
    """Local mock state and idempotency participate in the approval store transaction."""

    def __init__(self, holds: HoldStore, *, store: LedgerStore | None = None):
        self.holds = holds
        self.store = store or MemoryLedgerStore()
        self._intents: dict[str, Intent] = {}
        self._by_key: dict[str, Any] = {}
        self._next = 1
        self._revision = -1
        self.captures = 0
        self._refresh()

    @property
    def intents(self) -> dict[str, Intent]:
        """Refresh cross-process changes while preserving current-process memory mutations."""
        self._refresh()
        return self._intents

    def _refresh(self) -> None:
        state = self.store.get_state("payment")
        if state and state["revision"] != self._revision:
            self._intents = {key: Intent(**value) for key, value in state["intents"].items()}
            self._by_key = state["idempotency"]
            self._next = state["next"]
            self._revision = state["revision"]
            self.captures = state["captures"]

    def _save(self) -> None:
        self._revision += 1
        self.store.put_state("payment", {"schema_version": 1, "revision": self._revision, "next": self._next,
                                         "intents": {key: asdict(value) for key, value in self._intents.items()},
                                         "idempotency": self._by_key, "captures": self.captures})

    def status_of(self, intent_id: Optional[str]) -> Optional[str]:
        i = self.intents.get(intent_id or "")
        return i.status if i else None

    def amount_for_holds(self, hold_ids: list[str]) -> int:
        return sum(h.amount for hid in hold_ids if (h := self.holds.get(hid)))

    def _idem(self, operation: str, key: str, arguments: dict, fn: Callable[[], dict]) -> dict:
        with self.store.transaction():
            self._refresh()
            index = self.store.token("payment-idempotency", operation + ":" + key)
            request = self.store.token("payment-request", json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if key and index in self._by_key:
                prior = self._by_key[index]
                if prior["request"] != request:
                    raise LedgerConfigurationError("payment idempotency arguments changed")
                return {**prior["result"], "idempotent_replay": True}
            out = fn()
            if key:
                self._by_key[index] = {"request": request, "result": out, "key_id": self.store.key_id}
            self._save()
            return out

    def create_intent(self, hold_ids: list[str], key: str = "") -> dict:
        def go() -> dict:
            amount = self.amount_for_holds(hold_ids)
            cur = (self.holds.get(hold_ids[0]).currency if hold_ids and self.holds.get(hold_ids[0]) else "KRW")
            iid = f"pi_{self._next:04d}"
            self._next += 1
            self._intents[iid] = Intent(iid, amount, cur, list(hold_ids))
            return {"intent_id": iid, "amount": amount, "currency": cur, "status": "created", "hold_ids": hold_ids}
        return self._idem("create_intent", key, {"hold_ids": hold_ids}, go)

    def capture(self, intent_id: str, key: str = "") -> dict:
        def go() -> dict:
            i = self._intents[intent_id]
            if self.store.persistent and i.status in {"captured", "confirmed"}:
                return {"intent_id": intent_id, "status": i.status, "receipt_id": i.receipt_id,
                        "amount": i.amount, "currency": i.currency, "idempotent_replay": True}
            i.status, i.receipt_id = "captured", f"rcpt_{intent_id[3:]}"
            self.captures += 1
            return {"intent_id": intent_id, "status": "captured", "receipt_id": i.receipt_id,
                    "amount": i.amount, "currency": i.currency}
        return self._idem("capture", key, {"intent_id": intent_id}, go)

    def confirm(self, intent_id: str, key: str = "") -> dict:
        def go() -> dict:
            i = self._intents[intent_id]
            for hid in i.hold_ids:
                self.holds.consume(hid)
            i.status = "confirmed"
            return {"intent_id": intent_id, "status": "confirmed", "receipt_id": i.receipt_id,
                    "bookings": [self.holds.get(h).label for h in i.hold_ids if self.holds.get(h)]}
        return self._idem("confirm", key, {"intent_id": intent_id}, go)

    def refund(self, intent_id: str, amount: int, reason: str, key: str = "") -> dict:
        def go() -> dict:
            i = self._intents[intent_id]
            i.status, i.refunded = "refunded", amount
            return {"intent_id": intent_id, "status": "refunded", "refunded": amount, "reason": reason}
        return self._idem("refund", key, {"intent_id": intent_id, "amount": amount, "reason": reason}, go)

    def receipt(self, intent_id: str) -> dict:
        i = self.intents.get(intent_id)
        if not i:
            return {"error": "not_found", "intent_id": intent_id}
        return {"intent_id": i.intent_id, "status": i.status, "amount": i.amount, "currency": i.currency,
                "receipt_id": i.receipt_id, "refunded": i.refunded}
