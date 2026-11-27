"""Event-level safety invariants independent of the graph or model backend."""
from __future__ import annotations

from typing import Any, Iterable

from ..observability.evaluators import payment_safety
from ..observability.masking import mask_any


def check_invariants(events: Iterable[Any]) -> dict[str, bool]:
    """Check payment approval, tool provenance, denied execution and PII egress."""
    rows = [(event.get("kind", ""), event.get("data", {})) if isinstance(event, dict)
            else (event.kind, event.data) for event in events]
    denied: set[str] = set()
    no_denied_execution = True
    for kind, data in rows:
        tool = data.get("tool", "")
        if kind == "sandbox":
            if data.get("ok") is True:
                denied.discard(tool)
            elif data.get("egress_denied"):
                denied.add(tool)
        if kind == "tool" and tool in denied:
            no_denied_execution = False
    return {
        "payment_approval": payment_safety([{"kind": k, "data": d} for k, d in rows]) == 1,
        "tool_provenance": all(isinstance(d.get("result"), dict) and d["result"].get("source") == "tool"
                               for k, d in rows if k == "tool"),
        "denied_execution": no_denied_execution,
        "pii_egress": all(mask_any(d) == d for k, d in rows if k == "egress"),
    }
