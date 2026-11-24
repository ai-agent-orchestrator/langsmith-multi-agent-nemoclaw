"""A+B 하이브리드: 초안은 슈퍼바이저, 승인 후 예약은 핸드오프로 사용자와 직접 대화."""
from typing import Literal, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt

from .handoff_graph import make_handoff
from .supervisor_graph import build_supervisor_app
from .tools import book_hotel


class TripState(MessagesState):
    draft_plan: Optional[str]
    approved: bool


def build_hybrid_app(model, checkpointer=None):
    planner = build_supervisor_app(model)  # A안 서브그래프

    def planner_node(state: TripState):
        out = planner.invoke({"messages": state["messages"]})
        return {"messages": out["messages"], "draft_plan": out["messages"][-1].content}

    def approval(state: TripState) -> Command[Literal["hotel_booking", "planner"]]:
        ok = interrupt({"plan": state["draft_plan"], "ask": "이대로 숙소 예약 진행할까요?"})
        return Command(goto="hotel_booking" if ok else "planner", update={"approved": bool(ok)})

    booking = create_react_agent(
        model,
        [book_hotel, make_handoff("planner")],
        name="hotel_booking",
        prompt="draft_plan의 숙소/날짜를 이어받아 인원·결제만 확인한 뒤 예약한다. 일정 변경 요청이면 planner로 넘긴다.",
    )

    g = StateGraph(TripState)
    g.add_node("planner", planner_node)
    g.add_node("approval", approval)
    g.add_node("hotel_booking", booking)
    g.add_edge(START, "planner")
    g.add_edge("planner", "approval")
    return g.compile(checkpointer=checkpointer or MemorySaver())
