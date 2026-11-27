"""concert_demo DomainPack: 공연 조회(READ) 에이전트와 좌석 점유(RESERVE) 에이전트."""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool

from domains.common.holds import get_store
from orchestra import registry
from orchestra.filters.base import Filter
from orchestra.specs import AgentSpec, FilterSpec, SideEffect, TeamSpec, ToolSpec

from .tools import make_tools

_RESERVE = {"hold_seats"}


class ConcertDemoPack:
    """콘서트 도메인의 툴과 에이전트 선언."""

    name = "concert_demo"

    def __init__(self, services: dict[str, Any]):
        self._tools = make_tools(get_store(services))

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        """툴과 부작용 등급."""
        out = {}
        for name, item in self._tools.items():
            effect = SideEffect.RESERVE if name in _RESERVE else SideEffect.READ
            out[name] = (item, ToolSpec(name=name, side_effect=effect, idempotent=name not in _RESERVE))
        return out

    def agents(self) -> dict[str, AgentSpec]:
        """에이전트 선언."""
        def agent(name: str, tools: list[str], prompt: str) -> tuple[str, AgentSpec]:
            ident = f"{self.name}/{name}"
            return ident, AgentSpec(id=ident, name=name, tools=tools, prompt=prompt)

        return dict([
            agent("concert_catalog", ["search_concerts", "seat_map", "quote_tickets"], "공연 조회와 견적 담당"),
            agent("concert_booking", ["hold_seats"], "좌석 임시 점유 담당. hold 까지만 한다."),
        ])

    def teams(self) -> dict[str, TeamSpec]:
        """팀은 조직도에서 선언한다."""
        return {}

    def filters(self) -> list[FilterSpec]:
        """도메인 전용 필터 없음."""
        return []

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        """도메인 전용 필터 없음."""
        return {}


registry.register("concert_demo", ConcertDemoPack)
