"""Stable local trace schema for CLI and playground; no LangSmith/network side effects."""
import json
from typing import Any

from .observability.masking import mask_any, mask_text
from .runtime import EventLog

_FIELDS = {
    "filter": ("stage", "filter", "action", "reason", "tool", "agent"),
    "approval_request": ("tool", "ask"),
    "approval": ("tool", "approved"),
    "sandbox_route": ("tool", "agent", "sandbox", "route", "mode"),
    "sandbox": ("tool", "sandbox", "ok", "error", "egress_denied", "policy_revision"),
    "blocked": ("stage", "by", "tool", "agent", "reason"),
}


def trace_records(events: EventLog) -> list[dict[str, Any]]:
    """Return chronological, masked records with only explicit diagnostic fields."""
    return [mask_any({"seq": event.seq, "kind": event.kind,
                      **{key: event.data.get(key) for key in _FIELDS[event.kind]}})
            for event in events.events if event.kind in _FIELDS]


def render_trace(records: list[dict[str, Any]]) -> str:
    """Render one JSON record per line; embedded newlines stay escaped."""
    return "\n".join("[TRACE] " + mask_text(json.dumps(record, ensure_ascii=False, default=str))
                     for record in records)
