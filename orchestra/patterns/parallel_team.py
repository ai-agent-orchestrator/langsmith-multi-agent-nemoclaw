"""병렬 팀 (DESIGN §7.4, §12.3): LLM 팀장 없이 모든 멤버를 `Send` 로 동시에 실행하고 reducer 로 합친다.

동시에 실행되는 갈래에서는 승인 interrupt 와 부작용의 순서를 보장할 수 없으므로 READ 툴만 허용한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Send

from ..modules.review_adapter import child_reports, review_document, reviewer_reports
from ..registry import Resolved
from ..runtime import Runtime
from ..specs import OrgSpec, SideEffect, TeamSpec

Reports = Callable[[str, Any], list[dict[str, Any]]]


@dataclass(frozen=True)
class Reducer:
    """멤버 출력 해석 방법과 합치는 방법."""

    from_agent: Reports
    from_team: Reports
    combine: Callable[[list[dict[str, Any]]], str]


REDUCERS = {"review_aggregate": Reducer(reviewer_reports, child_reports, review_document)}


def check_parallel_teams(org: OrgSpec, resolved: Resolved) -> None:
    """병렬 팀 선언을 검증한다. 어긋나면 그래프를 만들지 않는다."""
    for team in org.teams.values():
        if not team.parallel:
            if team.reducer is not None:
                raise ValueError(f"team {team.name}: reducer requires parallel: true")
            continue
        if team.reducer not in REDUCERS:
            raise ValueError(f"team {team.name}: unknown reducer {team.reducer!r}")
        for member in team.members:
            if member in org.teams:
                child = org.teams[member]
                if not child.parallel or child.reducer != team.reducer:
                    raise ValueError(f"team {team.name}: sub-team {member} must be parallel with the same reducer")
                continue
            agent = org.agents[member]
            if agent.handoff_to:
                raise ValueError(f"team {team.name}: parallel member {agent.name} cannot hand off")
            if any(resolved.tools[t][1].side_effect != SideEffect.READ for t in agent.tools):
                raise ValueError(f"team {team.name}: parallel member {agent.name} may only use READ tools")


def _collect(left: Optional[list], right: Optional[list]) -> list:
    # None 은 초기화 신호: 같은 thread 의 다음 요청에 이전 보고서가 섞이지 않게 한다.
    return [] if right is None else (left or []) + right


class _State(MessagesState):
    reports: Annotated[list[dict[str, Any]], _collect]


def build_parallel_team(team: TeamSpec, members: list[tuple[Runnable, bool]], runtime: Runtime) -> StateGraph:
    """컴파일 전 그래프를 돌려준다. members 는 (실행 객체, 하위 팀 여부) 목록이다."""
    reducer = REDUCERS[str(team.reducer)]
    dispatch, join = f"{team.name}_dispatch", f"{team.name}_join"
    names = [str(member.name) for member, _ in members]

    def worker(member: Runnable, is_team: bool) -> RunnableLambda:
        parse = reducer.from_team if is_team else reducer.from_agent

        def collect(result: dict) -> dict:
            return {"reports": parse(str(member.name), result["messages"][-1].content)}

        def invoke(state: dict, config: RunnableConfig) -> dict:
            return collect(member.invoke({"messages": state["messages"]}, config))

        async def ainvoke(state: dict, config: RunnableConfig) -> dict:
            return collect(await member.ainvoke({"messages": state["messages"]}, config))

        return RunnableLambda(invoke, afunc=ainvoke)

    def combine(state: _State) -> dict:
        document = reducer.combine(state["reports"])
        summary = json.loads(document)
        runtime.events.add(
            "report", team=team.name, status="done" if summary["complete"] else "partial",
            summary=f"{len(summary['reports'])} reviews, {len(summary['issues'])} issues kept, "
                    f"{summary['dropped']} dropped",
        )
        return {"messages": [AIMessage(content=document, name=team.name)]}

    graph = StateGraph(_State, input_schema=MessagesState, output_schema=MessagesState)
    graph.add_node(dispatch, lambda state: {"reports": None})
    graph.add_node(join, combine)
    graph.add_edge(START, dispatch)
    graph.add_conditional_edges(
        dispatch, lambda state: [Send(name, {"messages": state["messages"]}) for name in names], names,
    )
    for name, (member, is_team) in zip(names, members):
        graph.add_node(name, worker(member, is_team))
        graph.add_edge(name, join)
    graph.add_edge(join, END)
    return graph
