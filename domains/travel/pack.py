"""travel DomainPack: 날씨/맛집/관광지/항공/숙소/렌터카/번역/긴급연락 (에이전트 8, 툴 17)."""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from langchain_core.tools import BaseTool

from domains.common.holds import get_store
from orchestra import registry
from orchestra.filters.base import Filter, FilterContext, FilterDecision
from orchestra.specs import AgentSpec, FilterSpec, SideEffect, TeamSpec, ToolSpec

from .tools import make_tools

_RESERVE = {"hold_flight", "hold_room", "hold_car"}


def _date_sanity() -> Filter:
    """pre_tool: 날짜 형식(YYYY-MM-DD) 검사 + 과거 날짜 차단 ('today' 는 services['clock'] 이 아니라 ctx.state)."""

    class _F:
        name = "date_sanity"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            args = ctx.payload if isinstance(ctx.payload, dict) else {}
            today = ctx.state.get("today")
            for k in ("date", "checkin"):
                v = args.get(k)
                if v is None:
                    continue
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                    return FilterDecision.block(f"{k} must be YYYY-MM-DD, got {v!r}")
                if today and date.fromisoformat(str(v)) < date.fromisoformat(str(today)):
                    return FilterDecision.block(f"{k} {v} is in the past (today={today})")
            return FilterDecision.allow()

    return _F()


class TravelPack:
    name = "travel"

    def __init__(self, services: dict[str, Any]):
        self.holds = get_store(services)
        self._tools = make_tools(self.holds)

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        out = {}
        for n, t in self._tools.items():
            se = SideEffect.RESERVE if n in _RESERVE else SideEffect.READ
            out[n] = (t, ToolSpec(name=n, side_effect=se, idempotent=n not in _RESERVE))
        return out

    def agents(self) -> dict[str, AgentSpec]:
        def a(name: str, tools: list[str], prompt: str, **kw: Any) -> tuple[str, AgentSpec]:
            return f"travel/{name}", AgentSpec(id=f"travel/{name}", name=name, tools=tools, prompt=prompt, **kw)

        return dict([
            a("weather_agent", ["get_weather", "get_hourly_forecast"], "날씨/시간대별 예보 담당",
              handoff_to=["travel/restaurant_agent"]),
            a("restaurant_agent", ["search_restaurants", "get_menu", "check_wait_time"], "맛집 담당"),
            a("attraction_agent", ["search_attractions", "get_opening_hours"], "관광지 담당"),
            a("flight_agent", ["search_flights", "check_seat", "hold_flight"], "항공 담당. hold 까지만 한다."),
            a("hotel_agent", ["search_hotels", "check_room", "hold_room"], "숙소 담당. hold 까지만 한다."),
            a("car_agent", ["search_cars", "hold_car"], "렌터카 담당. hold 까지만 한다."),
            a("translation_agent", ["translate"], "번역 담당"),
            a("emergency_agent", ["get_emergency_contacts"], "긴급 연락 담당"),
        ])

    def teams(self) -> dict[str, TeamSpec]:
        return {}

    def filters(self) -> list[FilterSpec]:
        return [FilterSpec(name="date_sanity", stages=["pre_tool"], order=15,
                           applies_to=["search_*", "hold_*", "get_weather", "get_hourly_forecast"])]

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        return {"date_sanity": _date_sanity}


registry.register("travel", TravelPack)
