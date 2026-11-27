"""Allowlisted NemoClaw commands; CLI syntax still requires real-install verification."""
from __future__ import annotations

import math
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SANDBOX_NAME_RE = re.compile(r"[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,17}[a-z0-9]|[a-z]")
_PRESET_RE = re.compile(r"[a-z][a-z0-9_-]{0,127}")
Command = Literal["help", "openshell_help", "status", "logs", "policy_list", "policy_get", "policy_add", "policy_add_file", "worker"]
WORKER_COMMAND = ("/opt/orchestra/venv/bin/python", "-m", "orchestra.sandbox.nemoclaw.worker")
MAX_OUTPUT_BYTES = 1_048_576


def _run_bounded(argv: list[str], *, timeout: float, input_text: str | None,
                 env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Drain both pipes concurrently, killing the process when either exceeds its cap."""
    process = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
    output = bytearray()
    overflow = threading.Event()

    def drain(stream: object, *, keep: bool) -> None:
        total = 0
        try:
            while True:
                chunk = getattr(stream, "read")(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    overflow.set()
                    process.kill()
                    break
                if keep:
                    output.extend(chunk)
        finally:
            getattr(stream, "close")()

    def write() -> None:
        try:
            if process.stdin is not None:
                process.stdin.write((input_text or "").encode())
        except (BrokenPipeError, OSError):
            pass
        finally:
            if process.stdin is not None:
                process.stdin.close()

    threads = [threading.Thread(target=drain, args=(stream,), kwargs={"keep": keep}, daemon=True)
               for stream, keep in ((process.stdout, True), (process.stderr, False))]
    if input_text is not None:
        threads.append(threading.Thread(target=write, daemon=True))
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=1)
    if overflow.is_set():
        raise ValueError("cli_output_too_large")
    if any(thread.is_alive() for thread in threads):
        raise ValueError("cli_pipe_incomplete")
    return subprocess.CompletedProcess(argv, process.returncode, output.decode("utf-8", errors="replace"), "")


@dataclass(frozen=True)
class CommandResult:
    """Return codes only: subprocess output is intentionally excluded from repr."""

    argv: tuple[str, ...]
    ok: bool
    stdout: str = field(default="", repr=False)
    error: str | None = None
    dry_run: bool = False


class NemoClawCli:
    """Execute fixed command shapes without a shell or caller-supplied flags."""

    def __init__(self, *, nemoclaw: str = "nemoclaw", openshell: str = "openshell",
                 timeout_s: float = 15.0, dry_run: bool = False,
                 dialect: Literal["legacy", "modern"] = "legacy", preset_root: Path | None = None) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self.nemoclaw = nemoclaw
        self.openshell = openshell
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        if dialect not in {"legacy", "modern"}:
            raise ValueError("unsupported CLI dialect")
        self.dialect = dialect
        self.preset_root = preset_root.resolve(strict=True) if preset_root is not None else None

    def command(self, action: Command, *, sandbox: str | None = None,
                preset: str | None = None) -> tuple[str, ...]:
        """Validate identifiers before producing an immutable argument vector."""
        if action in {"help", "openshell_help"}:
            if sandbox is not None or preset is not None:
                raise ValueError("unexpected command arguments")
            return (self.nemoclaw if action == "help" else self.openshell, "--help")
        if sandbox is None or SANDBOX_NAME_RE.fullmatch(sandbox) is None:
            raise ValueError("invalid sandbox name")
        if action == "policy_add_file":
            if preset is None or self.preset_root is None:
                raise ValueError("reviewed_preset_root_required")
            path = Path(preset).resolve(strict=True)
            if not path.is_relative_to(self.preset_root) or Path(preset).is_symlink() or path.suffix != ".yaml":
                raise ValueError("preset_file_not_admitted")
            if path.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError("preset_file_too_large")
            operation = ("policy", "add") if self.dialect == "modern" else ("policy-add",)
            return (self.nemoclaw, sandbox, *operation, "--from-file", str(path))
        if action == "policy_add":
            if preset is None or _PRESET_RE.fullmatch(preset) is None:
                raise ValueError("invalid preset name")
            operation = ("policy", "add") if self.dialect == "modern" else ("policy-add",)
            return (self.nemoclaw, sandbox, *operation, preset)
        if preset is not None:
            raise ValueError("unexpected preset")
        if action == "policy_get":
            return (self.openshell, "policy", "get", "--full", sandbox)
        if action == "worker":
            return (self.nemoclaw, sandbox, "exec", "--", *WORKER_COMMAND)
        if action == "policy_list" and self.dialect == "modern":
            return (self.nemoclaw, sandbox, "policy", "list")
        subcommands = {"status": "status", "logs": "logs", "policy_list": "policy-list"}
        if action not in subcommands:
            raise ValueError("command is not allowlisted")
        return (self.nemoclaw, sandbox, subcommands[action])

    def run(self, action: Command, *, sandbox: str | None = None,
            preset: str | None = None, input_text: str | None = None,
            timeout_s: float | None = None) -> CommandResult:
        """Run with a deadline; suppress raw stderr and exception messages."""
        argv = self.command(action, sandbox=sandbox, preset=preset)
        timeout = min(self.timeout_s, timeout_s) if timeout_s is not None else self.timeout_s
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid command timeout")
        if input_text is not None and (action != "worker" or len(input_text.encode()) > 1_048_576):
            raise ValueError("invalid command input")
        if self.dry_run:
            return CommandResult(argv, True, dry_run=True)
        try:
            result = _run_bounded(list(argv), timeout=timeout, input_text=input_text)
        except subprocess.TimeoutExpired:
            return CommandResult(argv, False, error="cli_timeout")
        except OSError:
            return CommandResult(argv, False, error="cli_unavailable")
        except ValueError:
            return CommandResult(argv, False, error="cli_output_invalid")
        if result.returncode:
            return CommandResult(argv, False, error="cli_failed")
        return CommandResult(argv, True, stdout=result.stdout)
