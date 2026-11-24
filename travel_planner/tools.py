"""공통 mock 툴과 전문 에이전트 (날씨 / 맛집 / 숙소)."""
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent


@tool
def get_weather(city: str, date: str) -> dict:
    """날짜별 날씨/강수확률 조회 (mock)."""
    return {"city": city, "date": date, "rain_prob": 70, "summary": "비"}


@tool
def search_restaurants(city: str, indoor_only: bool = False) -> list:
    """맛집 검색. indoor_only=True면 실내 위주 (mock)."""
    return [{"id": "r1", "name": f"{city} 실내 맛집", "area": "해운대", "indoor": True}]


@tool
def search_hotels(city: str, near: str, checkin: str, nights: int) -> list:
    """특정 지역 근처 숙소 후보 검색 (mock)."""
    return [{"id": "h1", "name": f"{near} 호텔", "price": 180000, "checkin": checkin, "nights": nights}]


@tool
def book_hotel(hotel_id: str, guests: int, checkin: str, nights: int) -> dict:
    """숙소 예약 실행 (mock). 실제 서비스에서는 결제 직전 interrupt()로 사용자 확인을 받을 것."""
    return {"status": "confirmed", "hotel_id": hotel_id, "guests": guests, "checkin": checkin, "nights": nights}


def build_agents(model, weather_extra=(), restaurant_extra=(), hotel_extra=(), hotel_can_book=True):
    """에이전트 3종 생성. *_extra 로 핸드오프 툴 등을 추가한다."""
    hotel_tools = [search_hotels, *( [book_hotel] if hotel_can_book else [] ), *hotel_extra]
    return (
        create_react_agent(model, [get_weather, *weather_extra], name="weather"),
        create_react_agent(model, [search_restaurants, *restaurant_extra], name="restaurant"),
        create_react_agent(model, hotel_tools, name="hotel"),
    )
