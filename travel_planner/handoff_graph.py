"""B안: 핸드오프 패턴 - 에이전트가 사용자와 직접 대화하며 바통 터치."""
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from .tools import build_agents


def make_handoff(target: str):
    @tool(f"transfer_to_{target}")
    def _transfer(
        state: Annotated[dict, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ):
        """해당 전문 에이전트에게 대화 제어권을 넘긴다."""
        ack = {"role": "tool", "content": f"{target}에게 제어권 이양", "tool_call_id": tool_call_id}
        return Command(
            goto=target,
            graph=Command.PARENT,
            update={"messages": state["messages"] + [ack], "active_agent": target},
        )

    return _transfer


def build_handoff_app(model, checkpointer=None):
    weather, restaurant, hotel = build_agents(
        model,
        weather_extra=[make_handoff("restaurant"), make_handoff("hotel")],
        restaurant_extra=[make_handoff("weather"), make_handoff("hotel")],
        hotel_extra=[make_handoff("weather"), make_handoff("restaurant")],
    )
    g = StateGraph(MessagesState)
    for name, agent in [("weather", weather), ("restaurant", restaurant), ("hotel", hotel)]:
        g.add_node(name, agent)
    g.add_edge(START, "weather")
    # 멀티턴 유지를 위해 체크포인터 필수
    return g.compile(checkpointer=checkpointer or MemorySaver())
