"""Stable local trace schema for CLI and playground; no LangSmith/network side effects."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from .observability.masking import mask_any, mask_text
from .runtime import EventLog

if TYPE_CHECKING:
    from .simulation import SimResult

_FIELDS = {
    "filter": ("stage", "filter", "action", "reason", "tool", "agent"),
    "approval_request": ("tool", "ask"),
    "approval": ("tool", "approved"),
    "sandbox_route": ("tool", "agent", "sandbox", "route", "mode"),
    "sandbox": ("tool", "sandbox", "ok", "error", "egress_denied", "policy_revision", "reason_code"),
    "blocked": ("stage", "by", "tool", "agent", "reason", "reason_code"),
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


def export_simulation(result: SimResult, *, trace: bool = False, scenario_label: str | None = None) -> dict:
    """Share the C13 JSON contract between the CLI and the read-only playground viewer."""
    from .simulation import check_expect

    payload = {
        "scenario": scenario_label if scenario_label is not None else result.scenario["id"],
        "final": result.final, "interrupts": result.interrupts, "decisions": result.decisions,
        "errors": check_expect(result),
        "events": [{"seq": event.seq, "kind": event.kind, **event.data} for event in result.events.events],
    }
    if trace:
        payload["trace"] = trace_records(result.events)
        payload = mask_any(payload)
    return payload


def load_trace_export(value: Any) -> dict:
    """Validate, minimize, and mask imported C13 diagnostics without retaining raw event payloads."""
    if not isinstance(value, dict) or not isinstance(value.get("trace"), list) or len(value["trace"]) > 10000:
        raise ValueError("invalid_trace_export")
    scenario = value.get("scenario", "imported")
    if not isinstance(scenario, str) or len(scenario) > 512:
        raise ValueError("invalid_trace_export")
    records = []
    previous = -1
    for record in value["trace"]:
        if not isinstance(record, dict):
            raise ValueError("invalid_trace_export")
        seq, kind = record.get("seq"), record.get("kind")
        if not isinstance(seq, int) or isinstance(seq, bool) or not previous < seq <= 2**31 - 1 or not isinstance(kind, str) or kind not in _FIELDS:
            raise ValueError("invalid_trace_export")
        if kind == "approval" and not isinstance(record.get("approved"), bool):
            raise ValueError("invalid_trace_export")
        row = {"seq": seq, "kind": kind, **{key: record.get(key) for key in _FIELDS[kind]}}
        for key, item in row.items():
            if key == "seq" or item is None:
                continue
            if key in {"approved", "ok"}:
                valid = isinstance(item, bool)
            elif key == "egress_denied":
                valid = isinstance(item, list) and all(isinstance(text, str) for text in item)
            elif key == "policy_revision":
                valid = isinstance(item, str) or type(item) is int and 0 <= item <= 2**31 - 1
            else:
                valid = isinstance(item, str)
            if not valid:
                raise ValueError("invalid_trace_export")
        records.append(mask_any(row))
        previous = seq
    return {"scenario": mask_text(scenario), "trace": records}
