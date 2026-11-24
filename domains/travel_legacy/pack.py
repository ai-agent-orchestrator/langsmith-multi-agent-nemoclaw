"""Legacy tools exposed through the domain-independent Orchestra builder."""

from typing import Any, Callable

from langchain_core.tools import BaseTool

from orchestra import registry
from orchestra.filters.base import Filter
from orchestra.specs import AgentSpec, FilterSpec, SideEffect, TeamSpec, ToolSpec

from . import tools as legacy_tools


class LegacyTravelPack:
    """Preserve mock schemas; confirmed bookings still require explicit approval."""

    name = "travel_legacy"

    def __init__(self, services: dict[str, Any]) -> None:
        self._tools = {n: t for n, t in vars(legacy_tools).items() if isinstance(t, BaseTool)}

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        """Attach COMMIT metadata to the original booking tools."""
        return {
            name: (tool, ToolSpec(name=name, side_effect=(
                SideEffect.COMMIT if name.startswith("book_") else SideEffect.READ
            )))
            for name, tool in self._tools.items()
        }

    def agents(self) -> dict[str, AgentSpec]:
        """Agent declarations live in travel_legacy.yaml."""
        return {}

    def teams(self) -> dict[str, TeamSpec]:
        """Team declarations live in travel_legacy.yaml."""
        return {}

    def filters(self) -> list[FilterSpec]:
        """Require the core approval policy for booking confirmations."""
        return [FilterSpec(name="side_effect_policy", stages=["pre_tool"], order=50)]

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        """Use built-in filter implementations."""
        return {}


registry.register("travel_legacy", LegacyTravelPack)
