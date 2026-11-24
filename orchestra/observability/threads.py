"""Thread metadata is per invocation, never a global runtime identifier."""


def thread_metadata(thread_id: str) -> dict[str, str]:
    """Use identical session/thread IDs across ask and approval resume."""
    return {"thread_id": thread_id, "session_id": thread_id}
