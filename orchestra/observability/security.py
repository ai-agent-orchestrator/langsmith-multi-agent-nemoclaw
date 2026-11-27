"""Failure-isolated security feedback with no raw denied hosts or tool arguments."""
from __future__ import annotations

from typing import Any

from .annotation import enqueue_low_scores
from .client import SafeClient
from .feedback import feedback
from .safe import safe_call


def report_egress_denied(client: Any, run_id: str, *, mode: str, policy_revision: str | None = None,
                         queue_id: str | None = None) -> None:
    """Send a fixed feedback code and optional review queue entry; closed sends nothing."""
    if mode == "closed":
        return
    facade = client if isinstance(client, SafeClient) else SafeClient(client, mode)
    safe_call(feedback, facade, run_id, "nemoclaw_egress_denied", 0.0,
              comment="Sandbox egress denied; policy revision: " + (policy_revision or "unknown"))
    if queue_id:
        safe_call(enqueue_low_scores, facade, queue_id, {run_id: 0.0})
