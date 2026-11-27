"""Allowlisted Claude CLI adapter with explicit feature probing and sandbox-only use."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from ..sandbox.nemoclaw.cli import _run_bounded

# No provider API key is forwarded: the CLI must authenticate inside the worker image, not via host env.
_ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}
_REQUIRED_FLAGS = ("--plugin-dir", "--output-format")


class ClaudeCliAgent:
    """CLI execution is unavailable on the host; tool wrappers must route it to a worker."""

    def __init__(self, binary: str = "claude", *, plugin_dir: Path, timeout_s: float = 60.0,
                 workspace: Path | None = None) -> None:
        if not 0 < timeout_s <= 300:
            raise ValueError("invalid_cli_timeout")
        self.binary, self.plugin_dir, self.timeout_s = binary, plugin_dir.resolve(strict=True), timeout_s
        self.workspace = workspace.resolve(strict=True) if workspace is not None else None

    @staticmethod
    def _env() -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}

    def _run(self, argv: list[str], *, timeout: float, input_text: str | None) -> subprocess.CompletedProcess[str]:
        cwd = str(self.workspace) if self.workspace is not None else None
        return _run_bounded(argv, timeout=timeout, input_text=input_text, env=self._env(), cwd=cwd)

    def available(self) -> bool:
        """Probe installed flags without invoking a model or exposing help text."""
        try:
            result = self._run([self.binary, "--help"], timeout=10, input_text=None)
            return result.returncode == 0 and all(flag in result.stdout for flag in _REQUIRED_FLAGS)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def invoke(self, prompt: str) -> dict:
        """Use fixed JSON flags and stdin; no prompt or exception appears in diagnostics."""
        if os.environ.get("ORCHESTRA_WORKER_SANDBOX") != "orchestra-code":
            return {"error": "code_worker_required"}
        if not prompt or len(prompt) > 20_000 or not self.available():
            return {"error": "claude_cli_unavailable"}
        try:
            result = self._run([self.binary, "-p", "--output-format", "json", "--plugin-dir", str(self.plugin_dir)],
                               timeout=self.timeout_s, input_text=prompt)
            if result.returncode:
                return {"error": "claude_cli_failed"}
            output = json.loads(result.stdout)
            return output if isinstance(output, dict) else {"error": "claude_cli_invalid_output"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"error": "claude_cli_failed"}
