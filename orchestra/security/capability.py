"""Short-lived argument-bound execution capabilities; tokens contain no raw arguments."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from typing import Any, Callable
from uuid import uuid4


def _digest(request: dict[str, Any]) -> str:
    selected = {key: value for key, value in request.items() if key != "capability_token"}
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class CapabilityAuthority:
    """A bootstrap key must be provisioned by operators and rotated outside agent control."""

    def __init__(self, key: bytes, grants: dict[str, set[str]], *, clock: Callable[[], float] = time.time,
                 ttl_s: float = 60.0) -> None:
        if len(key) < 32 or not math.isfinite(ttl_s) or not 0 < ttl_s <= 300:
            raise ValueError("invalid_capability_configuration")
        self._key, self.grants, self.clock, self.ttl_s = key, grants, clock, ttl_s

    def issue(self, request: dict[str, Any]) -> str:
        """Attenuate a caller's grant to one exact tool invocation and a short expiry."""
        agent = request.get("agent")
        if not isinstance(agent, str) or request.get("tool") not in self.grants.get(agent, set()):
            raise ValueError("capability_grant_denied")
        claims = json.dumps({"digest": _digest(request), "expires": self.clock() + self.ttl_s,
                             "nonce": uuid4().hex}, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(claims).decode()
        signature = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def verify(self, request: dict[str, Any], token: str | None) -> bool:
        """Reject forged, expired, broadened and argument-modified capabilities."""
        try:
            if not token or len(token) > 8192:
                return False
            encoded, signature = token.split(".")
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            claims = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
            expiry, now = claims["expires"], self.clock()
            agent = request.get("agent")
            return (type(expiry) in {int, float} and math.isfinite(expiry) and now < expiry <= now + 300
                    and claims["digest"] == _digest(request)
                    and isinstance(agent, str) and request.get("tool") in self.grants.get(agent, set()))
        except (ValueError, KeyError, TypeError):
            return False
