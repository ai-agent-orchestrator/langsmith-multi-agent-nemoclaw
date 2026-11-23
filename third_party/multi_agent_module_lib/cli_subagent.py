"""Method A: delegate work to the `claude` CLI as a sub-agent via subprocess.

Requires Claude Code installed and authenticated. Uses print mode (`-p`).
Not exercised by the test-suite (needs the CLI); check `claude --help` for
flags available in your installed version.
"""
from __future__ import annotations

import json
import subprocess


def ask_claude(prompt: str, cwd: str = ".", timeout: int = 600, extra_args: list[str] | None = None) -> str:
    cmd = ["claude", "-p", prompt, "--output-format", "json", *(extra_args or [])]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    try:
        return json.loads(proc.stdout).get("result", proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
