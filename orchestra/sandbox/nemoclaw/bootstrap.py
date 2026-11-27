"""Explicit deployment bootstrap; never install CLIs or mutate sandbox policies."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from ...security.capability import CapabilityAuthority
from ...specs import ToolSpec
from . import policy
from .cli import NemoClawCli
from .runner import NemoClawRunner
from .transport import StdioTransport


def make_runner(specs: Iterable[ToolSpec], grants: dict[str, set[str]], *, security_mode: str = "closed",
                dry_run: bool = False) -> NemoClawRunner:
    """Require explicit dialect and exec verification; default transport is unavailable."""
    specs = list(specs)
    dialect = os.environ.get("NEMOCLAW_CLI_DIALECT", "modern")
    if dialect not in {"legacy", "modern"}:
        raise ValueError("invalid_nemoclaw_cli_dialect")
    cli = NemoClawCli(nemoclaw=os.environ.get("NEMOCLAW_BIN", "nemoclaw"),
                     openshell=os.environ.get("OPENSHELL_BIN", "openshell"),
                     dialect=dialect, dry_run=dry_run)  # type: ignore[arg-type]
    options = {}
    if security_mode != "closed":
        host = urlsplit(os.environ.get("LANGSMITH_ENDPOINT", "")).hostname
        if not host:
            raise ValueError("explicit_langsmith_endpoint_required")
        options["langsmith_host"] = host
    presets = policy.build_presets(specs, security_mode=security_mode, **options)
    errors = policy.validate_presets(presets, tool_specs=specs, allow_placeholder=False)
    if errors:
        raise ValueError("deployment_preset_validation_failed")
    mapping = policy.applied_presets(sorted({s.sandbox_name for s in specs if s.sandbox_name}), presets)
    expected = {sandbox: {name: presets[name] for name in names} for sandbox, names in mapping.items()}
    authority = None
    key_file = os.environ.get("ORCHESTRA_CAPABILITY_KEY_FILE")
    if key_file:
        authority = CapabilityAuthority(Path(key_file).read_bytes(), grants)
    transport = StdioTransport(cli, exec_verified=True) if (
        os.environ.get("NEMOCLAW_EXEC_VERIFIED") == "1" and authority is not None) else None
    runner = NemoClawRunner(cli, expected, transport=transport, security_mode=security_mode, authority=authority)
    baseline_file = os.environ.get("NEMOCLAW_REVIEWED_BASELINE")
    if baseline_file:
        runner.baseline = json.loads(Path(baseline_file).read_text(encoding="utf-8"))
    return runner
