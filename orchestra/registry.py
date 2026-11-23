"""DomainPack 레지스트리. 코어는 도메인을 정적 import 하지 않고, 이름으로 동적 로드한다 (DESIGN §2-1)."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from langchain_core.tools import BaseTool

from .filters.base import Filter
from .filters.builtin import BUILTIN_FILTERS
from .org import assert_valid
from .specs import AgentSpec, FilterSpec, OrgSpec, TeamSpec, ToolSpec


class DomainPack(Protocol):
    name: str

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]: ...
    def agents(self) -> dict[str, AgentSpec]: ...
    def teams(self) -> dict[str, TeamSpec]: ...
    def filters(self) -> list[FilterSpec]: ...
    def filter_impls(self) -> dict[str, Callable[..., Filter]]: ...


_FACTORIES: dict[str, Callable[[dict[str, Any]], DomainPack]] = {}


def register(name: str, factory: Callable[[dict[str, Any]], DomainPack]) -> None:
    _FACTORIES[name] = factory


def build_packs(names: list[str], services: Optional[dict[str, Any]] = None) -> list[DomainPack]:
    """names 순서대로 팩 생성. 모듈 domains.<name>.pack 이 import 시 register() 를 호출해야 한다."""
    services = services if services is not None else {}
    packs = []
    for n in names:
        if n not in _FACTORIES:
            importlib.import_module(f"domains.{n}.pack")
        if n not in _FACTORIES:
            raise KeyError(f"domain {n!r} did not register itself")
        packs.append(_FACTORIES[n](services))
    return packs


@dataclass
class Resolved:
    org: OrgSpec
    tools: dict[str, tuple[BaseTool, ToolSpec]]
    filter_specs: list[FilterSpec]
    filter_impls: dict[str, Callable[..., Filter]]
    tool_domain: dict[str, str] = field(default_factory=dict)
    agent_domain: dict[str, str] = field(default_factory=dict)


def resolve(org: OrgSpec, packs: list[DomainPack]) -> Resolved:
    """org 에 도메인 팩의 에이전트/팀/툴/필터를 합치고 구조를 검증한다."""
    tools: dict[str, tuple[BaseTool, ToolSpec]] = {}
    tool_domain: dict[str, str] = {}
    agents: dict[str, AgentSpec] = {}
    agent_domain: dict[str, str] = {}
    teams = dict(org.teams)
    specs: list[FilterSpec] = []
    impls: dict[str, Callable[..., Filter]] = dict(BUILTIN_FILTERS)
    for p in packs:
        for n, pair in p.tools().items():
            if n in tools:
                raise ValueError(f"tool name {n!r} defined by both {tool_domain[n]} and {p.name}")
            tools[n] = pair
            tool_domain[n] = p.name
        for aid, a in p.agents().items():
            if aid in agents:
                raise ValueError(f"agent id {aid!r} defined twice")
            agents[aid] = a
            agent_domain[aid] = p.name
        for tn, t in p.teams().items():
            teams.setdefault(tn, t)  # org.yaml 이 우선
        specs += p.filters()
        impls.update(p.filter_impls())
    specs += org.filters  # 조직 공통 필터가 마지막 (같은 name 이면 덮어씀)
    full = org.model_copy(update={"agents": {**org.agents, **agents}, "teams": teams})
    for a in full.agents.values():
        for tool_name in a.tools:
            if tool_name not in tools:
                raise ValueError(f"agent {a.id}: unknown tool {tool_name!r}")
    assert_valid(full)
    return Resolved(full, tools, specs, impls, tool_domain, agent_domain)
