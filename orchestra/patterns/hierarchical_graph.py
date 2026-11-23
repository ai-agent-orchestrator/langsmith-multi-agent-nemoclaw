"""계층형 슈퍼바이저 빌더 (DESIGN §6.4): OrgSpec → 총괄 → 팀장 → 실무자.

하드코딩된 도메인이 없다. 조직도(orgs/*.yaml) + 도메인 팩(domains/*) + 모델 팩토리만 받는다.
기존 travel_planner.hierarchical_graph.build_hierarchical_app(model) 은 shim 에서 이 함수를 호출한다(M0, Codex).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from ..filters.chain import FilterChain, build_chain
from ..filters.tool_wrapper import wrap_tool
from ..models.factory import ConstantModelFactory, ModelFactory
from ..org import assert_valid, load_org, normalize_fanout, read_prompt, recursion_limit
from ..registry import Resolved, build_packs, resolve
from ..runtime import Runtime
from ..specs import AgentSpec, OrgSpec, TeamSpec
from .hierarchical_tools import make_escalate, make_handoff, make_report


@dataclass
class BuiltApp:
    graph: Any
    runtime: Runtime
    org: OrgSpec
    chain: FilterChain
    resolved: Resolved

    def config(self, thread_id: str = "default", **extra: Any) -> dict:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit(self.org), **extra}

    def invoke(self, payload: Any, config: Optional[dict] = None, **kw: Any) -> Any:
        return self.graph.invoke(payload, config or self.config(), **kw)

    def get_graph(self, **kw: Any) -> Any:
        return self.graph.get_graph(**kw)


def _sensitivity(agent: AgentSpec, res: Resolved) -> str:
    order = ["public", "internal", "pii", "pci"]
    levels = [res.tools[t][1].sensitivity for t in agent.tools]
    return max(levels, key=order.index) if levels else "public"


def _detach_to_supervisor(builder: Any, member: str, supervisor: str) -> None:
    """member→supervisor 고정 엣지를 조건부 엣지로 교체 (handoff 직후면 END, 아니면 supervisor)."""
    builder.edges.discard((member, supervisor))

    def route(state: dict) -> str:
        last = (state.get("messages") or [None])[-1]
        handed_off = isinstance(last, ToolMessage) and (last.name or "").startswith("transfer_to_")
        return END if handed_off else supervisor

    builder.add_conditional_edges(member, route, {supervisor: supervisor, END: END})


def build_hierarchical_app(
    org: OrgSpec | str | Path | dict,
    model_factory: ModelFactory | BaseChatModel,
    *,
    services: Optional[dict[str, Any]] = None,
    runtime: Optional[Runtime] = None,
    checkpointer: Any = None,
    prompt_base: Optional[Path] = None,
) -> BuiltApp:
    """조직도로부터 계층형 그래프를 만든다. checkpointer 는 최상위에만 붙는다(interrupt/resume 필수)."""
    from langgraph.checkpoint.memory import MemorySaver

    if not isinstance(org, OrgSpec):
        org = load_org(org)
    if isinstance(model_factory, BaseChatModel):
        model_factory = ConstantModelFactory(model_factory)
    services = services if services is not None else {}

    packs = build_packs(org.domains, services)
    res = resolve(org, packs)
    org = assert_valid(normalize_fanout(res.org))
    res.org = org

    rt = runtime or Runtime(org=org.name, services=services)
    rt.org = org.name
    rt.services = services if not rt.services else rt.services
    chain = build_chain(res.filter_specs, res.filter_impls, rt.events)
    base = prompt_base or Path(__file__).resolve().parent.parent.parent

    id_to_name = {aid: a.name for aid, a in org.agents.items()}
    built: dict[str, Runnable] = {}

    def prompt_text(declaration: str) -> Any:
        if declaration.startswith("hub:"):
            resolver = services.get("prompt_resolver")
            if not callable(resolver):
                raise ValueError("hub prompt requires an explicit prompt_resolver with local fallback")
            return resolver(declaration)
        return read_prompt(declaration, base)

    def build_agent(aid: str) -> Runnable:
        a = org.agents[aid]
        tools = [wrap_tool(res.tools[t][0], res.tools[t][1], chain, rt, agent=a.name, domain=res.tool_domain[t])
                 for t in a.tools]
        tools += [make_handoff(id_to_name[h], rt, source=a.name) for h in a.handoff_to]
        model = model_factory(role=a.name, alias=a.model, sensitivity=_sensitivity(a, res), kind="agent")
        return create_react_agent(model, tools, name=a.name, prompt=prompt_text(a.prompt) or None)

    def build_team(t: TeamSpec, is_root: bool) -> Any:
        members = [built[m] for m in t.members]
        model = model_factory(role=t.name, alias=t.model, sensitivity="public", kind="supervisor")
        sup = create_supervisor(
            cast(Any, members), model=model, prompt=prompt_text(t.prompt) or None, supervisor_name=t.name,
            output_mode=t.output_mode, tools=[make_report(t.name, rt), make_escalate(t.name, rt)],
        )
        # langgraph_supervisor 는 모든 멤버에 고정 엣지 member→supervisor 를 건다. handoff 를 쓰는 멤버는
        # Command(goto=형제) 와 이 고정 엣지가 같은 step 에 둘 다 실행돼 팀장이 중간에 끼어든다.
        # → 고정 엣지를 조건부 엣지로 바꿔, 방금 handoff 했으면 팀장으로 돌아가지 않게 한다.
        for m in t.members:
            if m in org.agents and org.agents[m].handoff_to:
                _detach_to_supervisor(sup, org.agents[m].name, t.name)
        return sup.compile(checkpointer=(checkpointer or MemorySaver()) if is_root else None, name=t.name)

    def build(name: str, is_root: bool = False) -> Runnable:
        if name in built:
            return built[name]
        if name in org.agents:
            built[name] = build_agent(name)
        else:
            t = org.teams[name]
            for m in t.members:
                build(m)
            built[name] = build_team(t, is_root)
        return built[name]

    graph = build(org.root, is_root=True)
    return BuiltApp(graph, rt, org, chain, res)
