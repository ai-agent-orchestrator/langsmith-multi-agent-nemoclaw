"""Distributed trace headers are optional; closed mode exports none."""
from langsmith import get_current_run_tree
from langsmith.run_trees import RunTree
from typing import Any

from .client import security_mode
from .safe import safe_call
from .tracing import tracing_scope


def trace_headers(mode: str | None = None) -> dict[str, str]:
    """Export only the installed SDK's trace correlation headers."""
    if security_mode(mode) == "closed":
        return {}
    tree = safe_call(get_current_run_tree)
    return safe_call(tree.to_headers, default={}) if tree else {}


def parent_scope(headers: dict[str, str], *, mode: str, client: Any = None) -> Any:
    """Continue an incoming parent trace, or suppress it entirely when closed."""
    if security_mode(mode) == "closed":
        return tracing_scope(mode, client)
    parent = safe_call(RunTree.from_headers, headers, client=client)
    if parent is not None:
        # 0.16 trace.create_child inherits the reconstructed parent's client, not the trace kwarg.
        safe_call(setattr, parent, "ls_client", client)
    return tracing_scope(mode, client, parent=parent)
