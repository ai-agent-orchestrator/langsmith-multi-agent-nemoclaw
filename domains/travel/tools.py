"""travel 도메인 mock 툴 (17개). 예약은 hold(RESERVE) 까지만 — 확정은 payment 팀의 결제 이후.

기존 travel_planner/hierarchical_tools.py 와 달리 book_* 대신 hold_* 를 쓴다 (DESIGN §11.3).
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from domains.common.holds import HoldStore

HOTEL_PRICE = {"standard": 180_000, "suite": 2_400_000}
FLIGHT_PRICE = 90_000
CAR_PRICE = 70_000


def make_tools(holds: HoldStore) -> dict[str, BaseTool]:
    @tool
    def get_weather(city: str, date: str) -> dict:
        """일자별 날씨/강수확률 (mock)."""
        return {"city": city, "date": date, "rain_prob": 70, "summary": "비"}

    @tool
    def get_hourly_forecast(city: str, date: str) -> list:
        """시간대별 예보 (mock)."""
        return [{"hour": h, "rain_prob": p} for h, p in ((9, 40), (12, 70), (15, 80), (18, 60))]

    @tool
    def search_restaurants(city: str, indoor_only: bool = False) -> list:
        """맛집 검색 (mock). indoor_only=True면 실내 매장만."""
        data = [
            {"id": "r1", "name": f"{city} 고기국수 맛집", "indoor": True},
            {"id": "r2", "name": f"{city} 흑돼지 구이집", "indoor": True},
            {"id": "r3", "name": f"{city} 해안 야외 횟집", "indoor": False},
        ]
        return [r for r in data if r["indoor"] or not indoor_only]

    @tool
    def get_menu(restaurant_id: str) -> list:
        """메뉴 조회 (mock)."""
        return [{"item": "대표 메뉴", "price": 15000}]

    @tool
    def check_wait_time(restaurant_id: str) -> dict:
        """대기시간 조회 (mock)."""
        return {"restaurant_id": restaurant_id, "wait_min": 20}

    @tool
    def search_attractions(city: str, indoor_only: bool = False) -> list:
        """관광지 검색 (mock)."""
        return [{"id": "a1", "name": f"{city} 박물관", "indoor": True}]

    @tool
    def get_opening_hours(attraction_id: str) -> dict:
        """운영시간 조회 (mock)."""
        return {"attraction_id": attraction_id, "open": "09:00", "close": "18:00"}

    @tool
    def search_flights(origin: str, dest: str, date: str) -> list:
        """항공편 검색 (mock)."""
        return [{"id": "f1", "origin": origin, "dest": dest, "date": date, "price": FLIGHT_PRICE}]

    @tool
    def check_seat(flight_id: str) -> dict:
        """잔여 좌석 (mock)."""
        return {"flight_id": flight_id, "seats_left": 5}

    @tool
    def hold_flight(flight_id: str, passengers: int) -> dict:
        """항공 좌석 임시 점유(hold, 15분). 결제 전까지 확정되지 않는다."""
        h = holds.create("flight", flight_id, f"항공 {flight_id} x{passengers}", FLIGHT_PRICE * passengers)
        return {"status": "held", "hold_id": h.hold_id, "amount": h.amount, "currency": h.currency}

    @tool
    def search_hotels(city: str, near: str, checkin: str, nights: int, room_type: str = "standard") -> list:
        """숙소 검색 (mock). room_type: standard | suite."""
        price = HOTEL_PRICE.get(room_type, HOTEL_PRICE["standard"])
        return [{"id": "h1", "name": f"{near} 호텔", "room_type": room_type, "price_per_night": price,
                 "checkin": checkin, "nights": nights}]

    @tool
    def check_room(hotel_id: str, checkin: str, nights: int) -> dict:
        """객실 재고 (mock)."""
        return {"hotel_id": hotel_id, "available": True}

    @tool
    def hold_room(hotel_id: str, guests: int, checkin: str, nights: int, room_type: str = "standard") -> dict:
        """객실 임시 점유(hold, 15분). 결제 전까지 확정되지 않는다."""
        price = HOTEL_PRICE.get(room_type, HOTEL_PRICE["standard"])
        h = holds.create("room", hotel_id, f"{hotel_id} {room_type} {checkin} {nights}박 {guests}명", price * nights)
        return {"status": "held", "hold_id": h.hold_id, "amount": h.amount, "currency": h.currency,
                "checkin": checkin, "nights": nights, "guests": guests}

    @tool
    def search_cars(city: str, date: str, days: int) -> list:
        """렌터카 검색 (mock)."""
        return [{"id": "c1", "name": "소형 SUV", "price": CAR_PRICE}]

    @tool
    def hold_car(car_id: str, date: str, days: int) -> dict:
        """렌터카 임시 점유(hold, 15분)."""
        h = holds.create("car", car_id, f"렌터카 {car_id} {date} {days}일", CAR_PRICE * days)
        return {"status": "held", "hold_id": h.hold_id, "amount": h.amount, "currency": h.currency}

    @tool
    def translate(text: str, target_lang: str) -> str:
        """번역 (mock)."""
        return f"[{target_lang}] {text}"

    @tool
    def get_emergency_contacts(country: str) -> dict:
        """긴급 연락처 (mock)."""
        return {"country": country, "police": "112", "ambulance": "119"}

    local: dict[str, Any] = locals()
    return {n: t for n, t in local.items() if isinstance(t, BaseTool)}
