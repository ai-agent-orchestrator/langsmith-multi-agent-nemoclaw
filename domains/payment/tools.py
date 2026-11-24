"""payment 도메인 툴 (mock 제공자 위임)."""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from .ledger import MockPaymentProvider


def make_tools(p: MockPaymentProvider) -> dict[str, BaseTool]:
    @tool
    def create_payment_intent(hold_ids: list[str], idempotency_key: str = "") -> dict:
        """hold 목록으로 결제 의도(intent)를 만든다. 금액은 hold 합계에서 서버가 계산한다 (인자로 받지 않는다)."""
        return p.create_intent(hold_ids, idempotency_key)

    @tool
    def capture_payment(intent_id: str, idempotency_key: str = "") -> dict:
        """결제 실행(capture). 금전 이동 — 사용자 승인 필요."""
        return p.capture(intent_id, idempotency_key)

    @tool
    def confirm_booking(intent_id: str, idempotency_key: str = "") -> dict:
        """결제 완료된 intent 의 hold 들을 예약 확정으로 전환한다."""
        return p.confirm(intent_id, idempotency_key)

    @tool
    def refund_payment(intent_id: str, amount: int, reason: str, idempotency_key: str = "") -> dict:
        """환불 실행. 금전 이동 — 사용자 승인 필요."""
        return p.refund(intent_id, amount, reason, idempotency_key)

    @tool
    def get_receipt(intent_id: str) -> dict:
        """영수증/상태 조회."""
        return p.receipt(intent_id)

    return {t.name: t for t in (create_payment_intent, capture_payment, confirm_booking, refund_payment, get_receipt)}
