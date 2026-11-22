"""결제 상태기계 (순수 함수). intent 상태: created → captured → confirmed, captured/confirmed → refunded."""
from __future__ import annotations

from typing import Optional

STATUSES = ("created", "captured", "confirmed", "refunded", "failed")

# 툴 -> 허용되는 '현재' 상태 (None = intent 가 아직 없음)
ALLOWED_FROM: dict[str, set[Optional[str]]] = {
    "create_payment_intent": {None},
    "capture_payment": {"created"},
    "confirm_booking": {"captured"},
    "refund_payment": {"captured", "confirmed"},
    "get_receipt": {"created", "captured", "confirmed", "refunded"},
}

NEXT_STATUS = {"create_payment_intent": "created", "capture_payment": "captured",
               "confirm_booking": "confirmed", "refund_payment": "refunded"}


def can_apply(tool: str, status: Optional[str]) -> tuple[bool, str]:
    allowed = ALLOWED_FROM.get(tool)
    if allowed is None:
        return True, ""
    if status in allowed:
        return True, ""
    return False, f"{tool} not allowed from status {status!r} (allowed: {sorted(s for s in allowed if s)})"
