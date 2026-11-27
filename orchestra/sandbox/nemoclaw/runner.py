"""Live-policy checked runner; a missing transport never runs a host callable."""
from __future__ import annotations

import json
from typing import Any

from ..base import ExecRequest, ExecResult
from .cli import NemoClawCli, SANDBOX_NAME_RE
from .live import drift_errors, parse_policy, revision
from .transport import Transport
from ...security.capability import CapabilityAuthority


class NemoClawRunner:
    """Recheck declared network rules before each execution, including after startup."""

    def __init__(self, cli: NemoClawCli, expected: dict[str, dict[str, dict]], *,
                 transport: Transport | None = None, security_mode: str = "closed",
                 authority: CapabilityAuthority | None = None) -> None:
        self.cli, self.expected, self.transport, self.security_mode = cli, expected, transport, security_mode
        self.authority = authority
        self.baseline: dict[str, dict] = {}

    def status(self, sandbox: str) -> dict[str, Any]:
        """Read live policy and return only safe status codes and its fingerprint."""
        if SANDBOX_NAME_RE.fullmatch(sandbox) is None or sandbox not in self.expected:
            return {"ok": False, "error": "sandbox_not_declared"}
        result = self.cli.run("policy_get", sandbox=sandbox)
        if not result.ok or result.dry_run:
            return {"ok": False, "error": "policy_unavailable"}
        try:
            problems = drift_errors(result.stdout, self.expected[sandbox], closed=self.security_mode == "closed",
                                    baseline=self.baseline.get(sandbox))
            if problems:
                return {"ok": False, "error": "policy_drift", "problems": problems}
            return {"ok": True, "policy_revision": revision(parse_policy(result.stdout))}
        except (ValueError, TypeError):
            return {"ok": False, "error": "policy_unrecognized"}

    def doctor(self) -> dict[str, Any]:
        """Probe read-only help and declared policies without modifying a sandbox."""
        help_results = [self.cli.run(action) for action in ("help", "openshell_help")]
        policy_command = "policy" if self.cli.dialect == "modern" else "policy-list"
        features = {name: name in help_results[0].stdout for name in (policy_command, "exec", "status")}
        features["openshell_policy"] = "policy" in help_results[1].stdout
        sandboxes = {sandbox: self.status(sandbox) for sandbox in self.expected}
        return {"ok": bool(sandboxes) and all(features.values()) and all(r.ok and not r.dry_run for r in help_results)
                and all(s["ok"] for s in sandboxes.values()), "sandboxes": sandboxes,
                "transport_available": self.transport is not None, "features": features}

    def exec(self, req: ExecRequest) -> ExecResult:
        """Transmit serialized data only after successful live drift validation."""
        state = self.status(req.sandbox)
        if not state["ok"]:
            return ExecResult(ok=False, error=state["error"])
        if self.transport is None:
            return ExecResult(ok=False, error="sandbox_unavailable")
        try:
            request = req.model_copy(deep=False)
            if self.authority is not None:
                request.capability_token = self.authority.issue(request.model_dump())
            wire = request.model_dump_json()
            response = self.transport.exchange(req.sandbox, wire, req.timeout_s)
            if len(response.encode()) > 1_048_576:
                raise ValueError
            data = json.loads(response)
            if not isinstance(data, dict) or type(data.get("ok")) is not bool:
                raise ValueError
            result = ExecResult.model_validate(data)
            result.policy_revision = state["policy_revision"]
            return result
        except Exception:
            return ExecResult(ok=False, error="sandbox_transport_failed")
