"""Strict parsing and content checks for the live OpenShell network policy."""
from __future__ import annotations

import hashlib
import json
import re
import os
from typing import Any
from urllib.parse import urlsplit

import yaml
from .policy import LANGSMITH_HOST

_CUSTOM = re.compile(r"nemoclaw_custom__([a-z][a-z0-9-]{0,62})__(.+)")
# `openshell policy get --base <sb>` (0.0.116 실측): "Key:   value" 헤더 몇 줄, 단독 `---`, 그 뒤 정책 YAML.
_META_LINE = re.compile(r"([A-Z][A-Za-z ]{0,30}):[ \t]+(\S.{0,200})")
_META_KEYS = frozenset({"Version", "Hash", "Status", "Source", "Config rev"})
_POLICY_KEYS = frozenset({"version", "filesystem_policy", "landlock", "process", "network_policies",
                          "network_middlewares"})


def split_policy_output(output: str) -> tuple[dict[str, str], str]:
    """Separate the OpenShell metadata banner from the policy body; unknown banners fail closed."""
    lines = output.splitlines()
    if "---" not in (line.strip() for line in lines):
        return {}, output
    meta: dict[str, str] = {}
    for index, line in enumerate(lines):
        if line.strip() == "---":
            body = "\n".join(lines[index + 1:])
            if not meta or "---" in (rest.strip() for rest in lines[index + 1:]):
                raise ValueError("unrecognized live policy")
            return meta, body
        if not line.strip():
            continue
        match = _META_LINE.fullmatch(line.rstrip())
        if match is None or match[1] not in _META_KEYS or match[1] in meta:
            raise ValueError("unrecognized live policy")
        meta[match[1]] = match[2].strip()
    raise ValueError("unrecognized live policy")


def parse_policy(output: str) -> dict[str, Any]:
    """Require plain YAML/JSON policy (optionally after the OpenShell banner); other shapes fail closed."""
    if len(output.encode()) > 1_048_576:
        raise ValueError("policy output too large")
    meta, output = split_policy_output(output)
    try:
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(output)):
            raise ValueError
        data = yaml.safe_load(output)
        # OpenShell omits an empty network_policies map. Read that as "no network rules" only for
        # a document that is recognizably its own output.
        if (meta and isinstance(data, dict) and "network_policies" not in data and set(data) <= _POLICY_KEYS
                and type(data.get("version")) is int and data["version"] == 1
                and isinstance(data.get("filesystem_policy"), dict)):
            data["network_policies"] = {}
        if not isinstance(data, dict) or not isinstance(data.get("network_policies"), dict):
            raise ValueError
        for key, value in data["network_policies"].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise ValueError("unrecognized live policy") from None
    return data


def parse_presets_from_policy(output: str) -> set[str]:
    """Recognize custom preset namespaces; names alone never establish compliance."""
    keys = parse_policy(output)["network_policies"]
    return {match[1] for key in keys if (match := _CUSTOM.fullmatch(key))}


def revision(policy: dict) -> str:
    """Hash the complete policy without retaining a copy in diagnostics."""
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def drift_errors(output: str, expected: dict[str, dict], *, closed: bool, baseline: dict | None = None) -> list[str]:
    """Compare complete scoped rules and reject every undeclared network policy."""
    actual = parse_policy(output)["network_policies"]
    wanted: dict[str, dict] = dict(baseline or {})
    for name, document in expected.items():
        for key, value in document["network_policies"].items():
            wanted[f"nemoclaw_custom__{name}__{key}"] = value
    errors = []
    if actual.keys() != wanted.keys():
        errors.append("policy_keys_differ")
    if any(actual.get(key) != value for key, value in wanted.items()):
        errors.append("policy_rules_differ")
    trace_hosts = {LANGSMITH_HOST, urlsplit(os.environ.get("LANGSMITH_ENDPOINT", "")).hostname}
    trace_egress = any(endpoint.get("host") in trace_hosts for value in actual.values()
                       for endpoint in value.get("endpoints", []) if isinstance(endpoint, dict))
    if closed and (any("orchestra-langsmith" in key for key in actual) or trace_egress):
        errors.append("closed_trace_egress")
    return errors
