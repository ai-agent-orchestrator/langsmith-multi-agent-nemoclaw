"""Operator-only network-policy change loop, bound to signed one-use approval."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import os
from typing import Any
from urllib.parse import urlsplit

from ..hitl import ApprovalRecord
from ..runtime import ApprovalLedger
from ..sandbox.nemoclaw import policy
from ..sandbox.nemoclaw.cli import SANDBOX_NAME_RE
from ..sandbox.nemoclaw.runner import NemoClawRunner
from ..sandbox.nemoclaw.live import drift_errors


def proposal_binding(sandbox: str, document: dict) -> dict[str, str]:
    """Create the exact human-review binding without writing or applying anything."""
    if SANDBOX_NAME_RE.fullmatch(sandbox) is None or set(document) != {"preset", "network_policies"}:
        raise ValueError("network_preset_only")
    name = document["preset"]["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", name):
        raise ValueError("invalid_preset_name")
    if policy.validate_preset(document, allow_placeholder=False):
        raise ValueError("invalid_network_preset")
    if set(document["preset"]) - {"name", "description"}:
        raise ValueError("invalid_preset_metadata")
    for entry in document["network_policies"].values():
        if set(entry) - {"name", "endpoints", "binaries"}:
            raise ValueError("unsupported_network_policy_field")
        for endpoint in entry["endpoints"]:
            if endpoint.get("enforcement") != "enforce" or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535:
                raise ValueError("network_enforcement_required")
    digest = hashlib.sha256(policy.render(document).encode()).hexdigest()
    return {"sandbox": sandbox, "preset_sha256": digest}


def apply_approved_policy(runner: NemoClawRunner, ledger: ApprovalLedger, record: ApprovalRecord,
                          sandbox: str, document: dict, *, now: float, audit: Any = None) -> dict:
    """Consume a signed approval before a single additive CLI call; never retry mutations."""
    document = deepcopy(document)
    binding = proposal_binding(sandbox, document)
    if (record.tool != "apply_network_policy" or json.loads(record.args_json) != binding
            or record.amount is not None or not ledger.is_granted(record.key, binding=record, now=now)):
        return {"ok": False, "error": "policy_approval_invalid"}
    if runner.cli.dry_run:
        return {"ok": False, "error": "policy_mutation_dry_run"}
    if audit is None:
        return {"ok": False, "error": "policy_audit_required"}
    if runner.cli.preset_root is None or sandbox not in runner.expected or not runner.status(sandbox)["ok"]:
        return {"ok": False, "error": "policy_preflight_failed"}
    candidate = deepcopy(runner.expected)
    candidate[sandbox][document["preset"]["name"]] = document
    flattened = {f"{sb}:{name}": doc for sb, docs in candidate.items() for name, doc in docs.items()}
    if policy.validate_presets(flattened, allow_placeholder=False):
        return {"ok": False, "error": "policy_cross_sandbox_validation_failed"}
    network = dict(runner.baseline.get(sandbox, {}))
    for name, doc in candidate[sandbox].items():
        network.update({f"nemoclaw_custom__{name}__{key}": value for key, value in doc["network_policies"].items()})
    if drift_errors(json.dumps({"network_policies": network}), candidate[sandbox],
                    closed=runner.security_mode == "closed", baseline=runner.baseline.get(sandbox)):
        return {"ok": False, "error": "closed_trace_egress"}
    shared_hosts = {policy.LANGSMITH_HOST, urlsplit(os.environ.get("LANGSMITH_ENDPOINT", "")).hostname}
    seen: dict[str, str] = {}
    for target, docs in candidate.items():
        endpoints = [endpoint for doc in docs.values() for entry in doc["network_policies"].values()
                     for endpoint in entry["endpoints"]]
        endpoints.extend(endpoint for entry in runner.baseline.get(target, {}).values() for endpoint in entry.get("endpoints", []))
        for endpoint in endpoints:
            if runner.security_mode != "closed" and endpoint["host"] in shared_hosts:
                continue
            hostport = f"{endpoint['host']}:{endpoint['port']}"
            if hostport in seen and seen[hostport] != target:
                return {"ok": False, "error": "policy_cross_sandbox_validation_failed"}
            seen[hostport] = target
    if not ledger.claim(record, now=now):
        return {"ok": False, "error": "policy_approval_consumed"}
    try:
        audit.append({"kind": "policy_change_attempt", **binding})
    except Exception:
        # Nothing has been mutated yet; the consumed approval simply must be re-issued.
        return {"ok": False, "error": "policy_audit_failed"}
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=runner.cli.preset_root, suffix=".yaml", delete=False) as stream:
            path = Path(stream.name)
            stream.write(policy.render(document).encode())
        result = runner.cli.run("policy_add_file", sandbox=sandbox, preset=str(path))
        if not result.ok:
            return {"ok": False, "error": "policy_change_indeterminate"}
        runner.expected = candidate
        state = runner.status(sandbox)
        audit.append({"kind": "policy_change_result", "ok": state["ok"], **binding})
        return {"ok": bool(state["ok"]), "error": None if state["ok"] else "policy_postflight_failed"}
    except Exception:
        return {"ok": False, "error": "policy_change_indeterminate"}
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
