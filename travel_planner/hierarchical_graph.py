"""Compatibility entry point for the original three-team travel organization."""
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from orchestra.models.factory import ModelFactory
from orchestra.org import REPO_ROOT
from orchestra.patterns import hierarchical_graph


def build_hierarchical_app(model: BaseChatModel | ModelFactory, checkpointer: Any = None) -> Any:
    """Build via Orchestra, preserving the compiled graph and optional checkpointer API."""
    built = hierarchical_graph.build_hierarchical_app(
        REPO_ROOT / "orgs/travel_legacy.yaml", model, checkpointer=checkpointer,
    )
    # Legacy callers can invoke without a thread_id when they did not request persistence.
    built.graph.checkpointer = checkpointer
    return built.graph
