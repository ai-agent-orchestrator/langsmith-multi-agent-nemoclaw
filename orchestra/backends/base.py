"""Agent construction contract; safety remains in the shared FilterChain/tool wrapper."""
from typing import Any, Protocol

from ..filters.base import Action, FilterContext, Stage
from ..filters.chain import FilterChain
from ..specs import AgentSpec, TeamSpec
from langgraph.config import get_config


class ModelBlocked(RuntimeError):
    """A pre-model filter denied invocation; its payload is never included in the error."""


def model_messages(filters: FilterChain, messages: list, agent: str) -> Any:
    """Apply pre_model once, before any model side effect, with fail-closed semantics."""
    try:
        config = get_config()
    except RuntimeError:
        config = {}
    metadata = config.get("metadata", {})
    result = filters.run(FilterContext(
        Stage.PRE_MODEL, messages, agent=agent, org=metadata.get("orchestra_org", ""),
        security_mode=metadata.get("security_mode", "masked"), sandbox_mode=metadata.get("sandbox_mode", "off"),
        state={"thread_id": config.get("configurable", {}).get("thread_id")},
    ))
    if result.action != Action.ALLOW:
        raise ModelBlocked("pre_model policy blocked model invocation")
    return result.payload


class AgentBackend(Protocol):
    """Return compiled workers and uncompiled team graphs for shared routing/checkpoint setup."""

    def agent(self, spec: AgentSpec, model: Any, tools: list, filters: FilterChain) -> Any: ...

    def supervisor(self, spec: TeamSpec, model: Any, members: list, filters: FilterChain,
                   extra_tools: list | None = None) -> Any: ...
