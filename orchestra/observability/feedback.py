"""Feedback operations always use the masked SDK facade."""
from typing import Any

from .capabilities import sdk_call


def feedback(client: Any, run_id: str, key: str, score: float, comment: str = "") -> Any:
    """Attach a deterministic or user-provided score to its exact run."""
    return sdk_call(client, "create_feedback", run_id, key, score=score, comment=comment)


def user_feedback_token(client: Any, run_id: str) -> Any:
    """Create a presigned user_score token when supported."""
    return sdk_call(client, "create_presigned_feedback_token", run_id, "user_score")
