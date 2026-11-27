"""concert_demo mock 툴. 예약은 hold(RESERVE) 까지만 하며 결제는 하지 않는다."""
from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from domains.common.holds import HoldStore

TICKET_PRICE = 50_000
MAX_TICKETS = 4


def make_tools(holds: HoldStore) -> dict[str, BaseTool]:
    """공용 HoldStore 를 공유하는 콘서트 툴을 만든다."""

    @tool
    def search_concerts(city: str) -> list:
        """도시별 공연 검색 (mock)."""
        return [{"id": "concert-1", "city": city, "title": f"{city} 라이브", "price": TICKET_PRICE}]

    @tool
    def seat_map(concert_id: str) -> dict:
        """구역별 잔여 좌석 (mock)."""
        return {"concert_id": concert_id, "sections": [{"section": "A", "seats_left": 8}]}

    @tool
    def quote_tickets(concert_id: str, count: int) -> dict:
        """티켓 견적 (mock). 한 번에 1~4장."""
        if not 1 <= count <= MAX_TICKETS:
            return {"error": f"count must be 1..{MAX_TICKETS}"}
        return {"concert_id": concert_id, "count": count, "total": TICKET_PRICE * count, "currency": "KRW"}

    @tool
    def hold_seats(concert_id: str, count: int) -> dict:
        """좌석 임시 점유(hold, 15분). 결제 전까지 확정되지 않는다."""
        if not 1 <= count <= MAX_TICKETS:
            return {"error": f"count must be 1..{MAX_TICKETS}"}
        h = holds.create("seat", concert_id, f"{concert_id} 좌석 {count}장", TICKET_PRICE * count)
        return {"status": "held", "hold_id": h.hold_id, "amount": h.amount, "currency": h.currency}

    local: dict[str, Any] = locals()
    return {n: t for n, t in local.items() if isinstance(t, BaseTool)}
