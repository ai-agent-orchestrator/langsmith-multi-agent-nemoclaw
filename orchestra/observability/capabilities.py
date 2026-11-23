"""Detect installed SDK methods rather than assuming cloud API availability."""
from typing import Any

from .client import SafeClient, security_mode
from .safe import safe_call

METHODS = ("create_presigned_feedback_token", "create_annotation_queue", "add_runs_to_annotation_queue",
           "push_prompt", "pull_prompt", "create_run_rule", "share_run", "create_examples")


def capabilities(client: Any) -> dict[str, bool]:
    """Report local method availability; this is not proof of server-side permission."""
    raw = client.raw if isinstance(client, SafeClient) else client
    return {name: callable(getattr(raw, name, None)) for name in METHODS}


def sdk_call(client: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Guard explicit client operations and report unsupported APIs without a request."""
    facade = client if isinstance(client, SafeClient) else SafeClient(client, security_mode())
    raw = facade.raw
    if raw is None:
        return None
    if not callable(getattr(raw, method, None)):
        return {"status": "unavailable", "guide": "docs/langsmith-ui-setup.md"}
    return safe_call(getattr(facade, method), *args, **kwargs)
