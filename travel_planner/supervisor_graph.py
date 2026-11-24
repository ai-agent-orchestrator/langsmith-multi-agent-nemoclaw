"""A안: 슈퍼바이저 패턴 - 일정표를 한 번에 생성 (예약 불가)."""
from langgraph_supervisor import create_supervisor

from .tools import build_agents

PROMPT = (
    "너는 여행 총괄이다. 순서: ①날씨 ②날씨 반영 맛집(비 오면 실내) "
    "③맛집 동선 근처 숙소 ④셋을 합쳐 N박M일 일정표 작성. "
    "숙소 '예약'은 하지 말고 후보만 제시하라."
)


def build_supervisor_app(model):
    weather, restaurant, hotel = build_agents(model, hotel_can_book=False)
    return create_supervisor(
        [weather, restaurant, hotel], model=model, prompt=PROMPT, output_mode="last_message"
    ).compile()
