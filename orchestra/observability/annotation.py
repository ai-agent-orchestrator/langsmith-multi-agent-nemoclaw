"""Annotation queue creation and low-score routing."""
from typing import Any

from .capabilities import sdk_call


def create_queue(client: Any, name: str = "orchestra-review") -> Any:
    """Create a queue with an explicit payment/helpfulness rubric."""
    return sdk_call(client, "create_annotation_queue", name=name,
                    rubric_instructions="Verify payment_safety (approval evidence) and helpfulness (task completion).")


def enqueue_low_scores(client: Any, queue_id: str, scores: dict[str, float], threshold: float = 0.5) -> Any:
    """Route only runs below the configured review threshold."""
    ids = [run_id for run_id, score in scores.items() if score < threshold]
    return sdk_call(client, "add_runs_to_annotation_queue", queue_id, run_ids=ids) if ids else None
