"""계층형 구조용 mock 툴 (LLM/그래프 의존성 없음 - langchain-core 만 필요)."""
from langchain_core.tools import tool


# ---------- 일정 기획 팀 ----------
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


# ---------- 예약/결제 팀 ----------
@tool
def search_flights(origin: str, dest: str, date: str) -> list:
    """항공편 검색 (mock)."""
    return [{"id": "f1", "origin": origin, "dest": dest, "date": date, "price": 90000}]


@tool
def check_seat(flight_id: str) -> dict:
    """잔여 좌석 (mock)."""
    return {"flight_id": flight_id, "seats_left": 5}


@tool
def book_flight(flight_id: str, passengers: int) -> dict:
    """항공 예약 (mock). 실서비스에서는 결제 직전 interrupt()로 확인."""
    return {"status": "confirmed", "flight_id": flight_id, "passengers": passengers}


@tool
def search_hotels(city: str, near: str, checkin: str, nights: int) -> list:
    """숙소 검색 (mock)."""
    return [{"id": "h1", "name": f"{near} 호텔", "price_per_night": 180000, "checkin": checkin, "nights": nights}]


@tool
def check_room(hotel_id: str, checkin: str, nights: int) -> dict:
    """객실 재고 (mock)."""
    return {"hotel_id": hotel_id, "available": True}


@tool
def book_hotel(hotel_id: str, guests: int, checkin: str, nights: int) -> dict:
    """숙소 예약 (mock)."""
    return {"status": "confirmed", "hotel_id": hotel_id, "guests": guests, "checkin": checkin, "nights": nights}


@tool
def search_cars(city: str, date: str, days: int) -> list:
    """렌터카 검색 (mock)."""
    return [{"id": "c1", "name": "소형 SUV", "price": 70000}]


@tool
def book_car(car_id: str, date: str, days: int) -> dict:
    """렌터카 예약 (mock)."""
    return {"status": "confirmed", "car_id": car_id}


# ---------- 현지 지원 팀 ----------
@tool
def translate(text: str, target_lang: str) -> str:
    """번역 (mock)."""
    return f"[{target_lang}] {text}"


@tool
def get_emergency_contacts(country: str) -> dict:
    """긴급 연락처 (mock)."""
    return {"country": country, "police": "112", "ambulance": "119"}
