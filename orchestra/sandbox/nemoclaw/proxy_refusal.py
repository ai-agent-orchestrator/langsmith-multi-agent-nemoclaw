"""Recognize an HTTPS request that the sandbox proxy refused.

OpenShell answers `403` to a tunnel its policy does not allow, and HTTP clients report that as an
exception. Only that case is recognized: a plain-HTTP 403 looks the same as the server's own answer,
and a tool that turns the exception into a normal return value is not seen.
"""
from __future__ import annotations

# http.client (urllib, requests, urllib3) puts the status line into this message.
_TUNNEL = "Tunnel connection failed: 403"
_MAX_CHAIN = 16


def proxy_refused(error: BaseException) -> bool:
    """Whether the exception, or one it wraps, says the proxy refused to open a tunnel."""
    pending: list[object] = [error]
    seen: set[int] = set()
    while pending and len(seen) < _MAX_CHAIN:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        try:
            text = str(item)
        except Exception:  # noqa: BLE001 - a broken __str__ must not replace the tool's failure
            text = ""
        # httpx reports a refused tunnel as its ProxyError carrying only the status line.
        if _TUNNEL in text or type(item).__name__ == "ProxyError" and text.startswith("403"):
            return True
        pending.extend((item.__cause__, item.__context__, getattr(item, "reason", None), *item.args))
    return False
