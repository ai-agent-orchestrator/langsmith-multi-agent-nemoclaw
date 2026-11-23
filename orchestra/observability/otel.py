"""OTel configuration inspection; no exporter or network is started implicitly."""
import os
import importlib.util
from typing import Any
from .client import SafeClient, make_client, security_mode


def configuration(mode: str) -> dict:
    """Report requested OTel settings, enforcing disabled export in closed mode."""
    return {"enabled": False,
            "requested": mode != "closed" and os.environ.get("LANGSMITH_OTEL_ENABLED", "").lower() in {"true", "1"},
            "sdk_installed": importlib.util.find_spec("opentelemetry.sdk") is not None,
            "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "") if mode != "closed" else "",
            "guide": "docs/langsmith-ui-setup.md#opentelemetry",
            "status": "configuration-only; installed exporter and sandbox integration unverified"}


def make_otel_client(mode: str | None = None, **kwargs: Any) -> SafeClient | None:
    """Explicitly construct an OTel-mode client only when its SDK dependency exists."""
    selected = security_mode(mode)
    if selected == "closed":
        return make_client(selected)
    if importlib.util.find_spec("opentelemetry.sdk") is None:
        return None
    return make_client(selected, tracing_mode="otel", **kwargs)
