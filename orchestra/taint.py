"""Conservative quarantine of recognized tool-origin instructions, without storing raw text."""
import json
from typing import Any

from .filters.builtin.request_gate import check_text


class ToolTaintBlocked(RuntimeError):
    """Reject tool-origin instruction propagation with a payload-free exception."""


def instruction_in(value: Any) -> bool:
    """Recognize the shared gate patterns in structured tool data, including JSON escapes."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("error") == "tool_instruction_blocked":
                return True
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if check_text(item):
                return True
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, (dict, list)) or isinstance(parsed, str) and parsed != item:
                pending.append(parsed)
    return False


def reject_labelled_instructions(value: Any) -> None:
    """Prevent an explicit tool data envelope being laundered through handoff or COMMIT args."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("source") == "tool" and instruction_in(item):
                raise ToolTaintBlocked("tool_instruction_blocked")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def current_thread() -> str:
    """Read request scope from LangGraph config, never Runtime.thread_id."""
    from langgraph.config import get_config

    try:
        return str(get_config().get("configurable", {}).get("thread_id", ""))
    except RuntimeError:
        return ""  # Standalone tools share a conservative unscoped quarantine.
