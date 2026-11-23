"""Compatibility backend with the original supervisor implementation."""
from typing import Any

from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from ..filters.chain import FilterChain
from ..specs import AgentSpec, TeamSpec
from .base import model_messages


def model_hook(filters: FilterChain, agent: str) -> Any:
    """Provide prebuilt's verified pre_model_hook interface."""
    def before(state: dict) -> dict:
        return {"llm_input_messages": model_messages(filters, state["messages"], agent)}

    return before


class PrebuiltBackend:
    """Preserve the current worker/supervisor construction and output conventions."""

    name = "prebuilt"

    def agent(self, spec: AgentSpec, model: Any, tools: list, filters: FilterChain) -> Any:
        """Build a compatibility worker using already guarded tools."""
        return create_react_agent(model, tools, name=spec.name, prompt=spec.prompt or None,
                                  pre_model_hook=model_hook(filters, spec.name))

    def supervisor(self, spec: TeamSpec, model: Any, members: list, filters: FilterChain,
                   extra_tools: list | None = None) -> Any:
        """Return the existing uncompiled supervisor for shared handoff edge repair."""
        return create_supervisor(members, model=model, prompt=spec.prompt or None, supervisor_name=spec.name,
                                 output_mode=spec.output_mode, tools=extra_tools or [],
                                 pre_model_hook=model_hook(filters, spec.name))
