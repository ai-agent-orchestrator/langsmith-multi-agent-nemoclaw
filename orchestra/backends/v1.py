"""LangChain v1 workers and tool-calling supervisors; no deprecated factory calls."""
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph_supervisor.handoff import create_handoff_tool, create_handoff_back_messages

from ..filters.chain import FilterChain
from ..filters.middleware import FilterMiddleware
from ..specs import AgentSpec, TeamSpec


def member_node(member: Any, spec: TeamSpec) -> RunnableLambda:
    """Preserve supervisor output_mode and return-control messages on sync/async paths."""
    def output(result: dict) -> dict:
        messages = list(result["messages"])
        if spec.output_mode == "last_message":
            messages = messages[-2:] if isinstance(messages[-1], ToolMessage) else messages[-1:]
        messages += list(create_handoff_back_messages(member.name, spec.name))
        return {"messages": messages}

    def invoke(state: dict, config: RunnableConfig) -> dict:
        return output(member.invoke(state, config))

    async def ainvoke(state: dict, config: RunnableConfig) -> dict:
        return output(await member.ainvoke(state, config))

    return RunnableLambda(invoke, afunc=ainvoke)


class V1Backend:
    """Use create_agent at every level while preserving the existing handoff protocol."""

    name = "v1"

    def agent(self, spec: AgentSpec, model: Any, tools: list, filters: FilterChain) -> Any:
        """Build a v1 worker with shared guarded tools and model middleware."""
        return create_agent(model, tools, name=spec.name, system_prompt=spec.prompt or None,
                            middleware=[FilterMiddleware(filters, spec.name)])

    def supervisor(self, spec: TeamSpec, model: Any, members: list, filters: FilterChain,
                   extra_tools: list | None = None) -> Any:
        """Expose child graphs through handoff tools in an uncompiled parent graph."""
        tools = (extra_tools or []) + [create_handoff_tool(agent_name=member.name) for member in members]
        controller = create_agent(model, tools, name=spec.name, system_prompt=spec.prompt or None,
                                  middleware=[FilterMiddleware(filters, spec.name)])
        graph = StateGraph(MessagesState)
        graph.add_node(spec.name, controller, destinations=tuple(member.name for member in members) + (END,))
        graph.add_edge(START, spec.name)
        for member in members:
            graph.add_node(member.name, member_node(member, spec))
            graph.add_edge(member.name, spec.name)
        return graph
