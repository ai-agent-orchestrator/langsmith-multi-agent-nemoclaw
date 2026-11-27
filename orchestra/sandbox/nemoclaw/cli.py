"""Allowlisted NemoClaw/OpenShell commands.

Syntax verified against nemoclaw v0.0.145 / openshell 0.0.116 on WSL2 (2026-11-27,
docs/NEMOCLAW_REALITY.md). Known CLI failure modes this wrapper defends against:
sandbox-scoped nemoclaw commands can hang without output, and an orphaned nemoclaw
process can hold ~/.nemoclaw-portable-host.lock and block every later call.
"""
from __future__ import annotations

import contextlib
import fcntl
import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SANDBOX_NAME_RE = re.compile(r"[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,17}[a-z0-9]|[a-z]")
_PRESET_RE = re.compile(r"[a-z][a-z0-9_-]{0,127}")
Command = Literal["help", "openshell_help", "status", "status_json", "logs", "policy_list", "policy_get",
                  "policy_add", "policy_add_file", "policy_add_file_preview", "worker", "ssh_config"]
WORKER_COMMAND = ("/opt/orchestra/venv/bin/python", "-m", "orchestra.sandbox.nemoclaw.worker")
# -P keeps the (agent-writable) working directory off sys.path, so a file planted in the workspace
# cannot replace the worker package. The image must make /opt/orchestra importable by itself.
SERVE_COMMAND = (WORKER_COMMAND[0], "-P", *WORKER_COMMAND[1:], "--serve")
MAX_OUTPUT_BYTES = 1_048_576
_STDERR_TAIL_BYTES = 4_096
_HOST_LOCK_MARKER = "Failed to acquire lock"
_LOCK_PATH = Path(os.environ.get("ORCHESTRA_NEMOCLAW_CLI_LOCK",
                                 Path.home() / ".cache" / "orchestra" / "nemoclaw-cli.lock"))


def _child_env() -> dict[str, str]:
    """NemoClaw sends operation telemetry by default (v0.0.141+); keep it off unless explicitly enabled."""
    env = dict(os.environ)
    if os.environ.get("ORCHESTRA_NEMOCLAW_TELEMETRY") != "1":
        env["NEMOCLAW_DISABLE_TELEMETRY"] = "1"
    return env


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the whole process group so no nemoclaw child keeps the host lock."""
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError):
            os.killpg(pgid, sig)
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


@contextlib.contextmanager
def _single_flight(deadline: float):
    """Serialize our own nemoclaw calls so they never contend for NemoClaw's host lock."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "a+") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cli_busy") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_bounded(argv: list[str], *, timeout: float, input_text: str | None,
                 env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Drain both pipes concurrently, killing the process when either exceeds its cap.

    Raw stderr never leaves this function: the returned stderr is empty, or only the fixed
    host-lock marker when the child reported it.
    """
    process = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                               start_new_session=True)
    output = bytearray()
    errors = bytearray()
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
                    _kill_group(process)
                    break
                if keep:
                    output.extend(chunk)
                else:
                    # stderr is only classified, never returned; keep a bounded tail.
                    errors.extend(chunk)
                    del errors[:-_STDERR_TAIL_BYTES]
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
        _kill_group(process)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=1)
    if overflow.is_set():
        raise ValueError("cli_output_too_large")
    if any(thread.is_alive() for thread in threads):
        raise ValueError("cli_pipe_incomplete")
    marker = _HOST_LOCK_MARKER if _HOST_LOCK_MARKER.encode() in errors else ""
    return subprocess.CompletedProcess(argv, process.returncode, output.decode("utf-8", errors="replace"), marker)


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
                 dialect: Literal["legacy", "modern"] = "modern", preset_root: Path | None = None) -> None:
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
        if action in {"policy_add_file", "policy_add_file_preview"}:
            if preset is None or self.preset_root is None:
                raise ValueError("reviewed_preset_root_required")
            path = Path(preset).resolve(strict=True)
            if not path.is_relative_to(self.preset_root) or Path(preset).is_symlink() or path.suffix != ".yaml":
                raise ValueError("preset_file_not_admitted")
            if path.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError("preset_file_too_large")
            operation = ("policy", "add") if self.dialect == "modern" else ("policy-add",)
            # 비대화 실행에서는 확인 프롬프트가 stdin 없이 멈추므로 --yes 가 필요하다. 사람 승인은 호출자가 먼저 확보한다.
            confirm = ("--dry-run",) if action == "policy_add_file_preview" else ("--yes",)
            return (self.nemoclaw, sandbox, *operation, "--from-file", str(path), *confirm)
        if action == "policy_add":
            if preset is None or _PRESET_RE.fullmatch(preset) is None:
                raise ValueError("invalid preset name")
            operation = ("policy", "add") if self.dialect == "modern" else ("policy-add",)
            return (self.nemoclaw, sandbox, *operation, preset)
        if preset is not None:
            raise ValueError("unexpected preset")
        if action == "policy_get":
            # 라운드트립 가능한 base 정책 (provider 합성 항목 제외). nemoclaw policy get 보다 빠르고 같은 내용이다.
            return (self.openshell, "policy", "get", "--base", sandbox)
        if action == "status_json":
            return (self.nemoclaw, sandbox, "status", "--json")
        if action == "worker":
            # exec forwards a piped stdin only when asked to.
            return (self.nemoclaw, sandbox, "exec", "--stdin", "--", *WORKER_COMMAND)
        if action == "ssh_config":
            return (self.openshell, "sandbox", "ssh-config", sandbox)
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
        deadline = time.monotonic() + timeout
        guard = _single_flight(deadline) if argv[0] == self.nemoclaw else contextlib.nullcontext()
        try:
            with guard:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return CommandResult(argv, False, error="cli_busy")
                result = _run_bounded(list(argv), timeout=remaining, input_text=input_text, env=_child_env())
        except TimeoutError:
            return CommandResult(argv, False, error="cli_busy")
        except subprocess.TimeoutExpired:
            return CommandResult(argv, False, error="cli_timeout")
        except OSError:
            return CommandResult(argv, False, error="cli_unavailable")
        except ValueError:
            return CommandResult(argv, False, error="cli_output_invalid")
        if result.returncode:
            busy = _HOST_LOCK_MARKER in (result.stderr or "") or _HOST_LOCK_MARKER in result.stdout
            return CommandResult(argv, False, error="nemoclaw_host_lock_busy" if busy else "cli_failed")
        return CommandResult(argv, True, stdout=result.stdout)
