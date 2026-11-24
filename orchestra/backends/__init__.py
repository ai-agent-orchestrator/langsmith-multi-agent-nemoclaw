"""Backend selection is explicit or environment-driven; imports are lazy."""
import os

from .base import AgentBackend


def get_backend(backend: str | AgentBackend | None = None) -> AgentBackend:
    """Select prebuilt/v1, reporting missing optional v1 dependencies clearly."""
    if backend is not None and not isinstance(backend, str):
        return backend
    selected = backend if backend is not None else os.environ.get("ORCHESTRA_AGENT_BACKEND", "prebuilt")
    if selected == "prebuilt":
        from .prebuilt import PrebuiltBackend

        return PrebuiltBackend()
    if selected == "v1":
        try:
            from .v1 import V1Backend
        except ImportError:
            raise RuntimeError("v1 backend requires the optional [agent-v1] dependency") from None
        return V1Backend()
    raise ValueError("unsupported agent backend; select prebuilt or v1")


__all__ = ["AgentBackend", "get_backend"]
