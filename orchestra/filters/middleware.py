"""V1 model middleware; tool filtering remains exclusively in tool_wrapper."""
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from .chain import FilterChain
from ..backends.base import model_messages


class FilterMiddleware(AgentMiddleware):
    """Apply pre_model to the invocation copy, without rewriting conversation history."""

    def __init__(self, filters: FilterChain, agent: str) -> None:
        self.filters, self.agent = filters, agent

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        """Validate/modify messages before invoking the model exactly once."""
        messages = model_messages(self.filters, request.messages, self.agent)
        return handler(request.override(messages=messages))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Use the same filtering path for asynchronous callers."""
        messages = model_messages(self.filters, request.messages, self.agent)
        return await handler(request.override(messages=messages))
