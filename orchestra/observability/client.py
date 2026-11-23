"""Masked SDK facade; closed mode never constructs an SDK client."""
from __future__ import annotations

import os
from typing import Any

from langsmith import Client

from .masking import mask_any
from .safe import safe_call


def security_mode(mode: str | None = None) -> str:
    """Resolve the Orchestra mode with legacy env compatibility."""
    selected = (mode or os.environ.get("ORCHESTRA_SECURITY_MODE") or os.environ.get("TRAVEL_SECURITY_MODE", "masked")).lower()
    if selected not in {"off", "masked", "closed", "self_hosted"}:
        raise ValueError("unsupported observation security mode")
    return selected


class SafeClient:
    """Guard public SDK calls, including iterator consumption and non-run payloads."""

    def __init__(self, client: Any, mode: str) -> None:
        self.raw = client
        self.mode = security_mode(mode)

    def __getattr__(self, name: str) -> Any:
        attribute = safe_call(getattr, self.raw, name, None) if self.raw is not None and self.mode != "closed" else None
        if attribute is not None and not callable(attribute):
            return attribute

        def invoke(*args: Any, **kwargs: Any) -> Any:
            if self.mode == "closed" or self.raw is None or attribute is None:
                return [] if name.startswith("list_") else None
            if name == "share_run" and self.mode != "off":
                return None
            masked_args = mask_any(args) if self.mode != "off" else args
            masked_kwargs = mask_any(kwargs) if self.mode != "off" else kwargs

            def call() -> Any:
                result = attribute(*masked_args, **masked_kwargs)
                return list(result) if name.startswith("list_") else result

            return safe_call(call, default=[] if name.startswith("list_") else None)

        return invoke


def make_client(mode: str | None = None, **kwargs: Any) -> SafeClient:
    """Create an explicit masked transport; credentials and endpoint come from env/kwargs."""
    selected = security_mode(mode)
    if selected == "closed":
        return SafeClient(None, selected)
    if selected != "off":
        kwargs["anonymizer"] = mask_any
    return SafeClient(safe_call(Client, **kwargs), selected)
