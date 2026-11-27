"""Create the OpenShell worker sandbox, give it its key, and probe the persistent worker.

Run inside WSL2 from a checkout with the project installed. Without --yes nothing outside the
staging directory changes; with --yes it creates one new sandbox and writes the host key file if
it is missing. It never changes, rebuilds or deletes an existing sandbox.

    python scripts/nemoclaw_worker_sandbox.py --python-base 'python:3.11-slim@sha256:<digest>'
    python scripts/nemoclaw_worker_sandbox.py --python-base ... --yes
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
# The image's profile settings name this sandbox, so the name is not configurable here.
SANDBOX = "orchestra-code"
KEY_PATH = "/sandbox/state/capability.key"
CONTEXT_PATHS = ("nemoclaw/requirements-worker.lock", "orchestra", "domains", "orgs", "third_party")
_BASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}")
_INSTALL_KEY = f"umask 077 && cat > {KEY_PATH}.new && chmod 400 {KEY_PATH}.new && mv -f {KEY_PATH}.new {KEY_PATH}"


def stage_context(destination: Path, python_base: str, root: Path = ROOT) -> int:
    """Copy only git-tracked image inputs into an empty directory; return the number of files."""
    if _BASE_RE.fullmatch(python_base) is None:
        raise ValueError("python base must be an image reference with a sha256 digest")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("staging directory is not empty")
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *CONTEXT_PATHS], shell=False,
                            check=True, capture_output=True, timeout=60).stdout
    names = [name for name in listed.decode("utf-8").split("\0") if name]
    if not names:
        raise ValueError("no tracked image inputs found")
    for name in names:
        source, target = root / name, destination / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"not a regular file: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    dockerfile = (root / "nemoclaw" / "Dockerfile.worker").read_text(encoding="utf-8")
    if dockerfile.count("\nARG PYTHON_BASE\nFROM ") != 1:
        raise ValueError("unexpected Dockerfile layout")
    (destination / "Dockerfile").write_text(
        dockerfile.replace("\nARG PYTHON_BASE\nFROM ", f"\nARG PYTHON_BASE={python_base}\nFROM "), encoding="utf-8")
    return len(names) + 1


def create_command(openshell: str, context: Path, policy: Path) -> list[str]:
    """Fixed argv; `sleep infinity` keeps the main process alive because its exit stops the sandbox."""
    return [openshell, "sandbox", "create", "--name", SANDBOX, "--from", str(context), "--policy", str(policy),
            "--no-auto-providers", "--no-tty", "--detach", "--", "sleep", "infinity"]


def load_or_create_master_key(path: Path) -> bytes:
    """Read the host master key, creating it with owner-only permissions the first time."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_hex(32).encode())
    if path.stat().st_mode & 0o077:
        raise ValueError("the key file is readable by other users; run chmod 600 on it")
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("the key file is too short")
    return key


def install_key(launcher: object, key: bytes, *, wait_s: float = 180, pause_s: float = 5) -> bool:
    """Write the key over ssh stdin (never argv), retrying while the new sandbox becomes ready."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            argv = launcher.argv(SANDBOX, _INSTALL_KEY)  # type: ignore[attr-defined]
            if subprocess.run(argv, shell=False, input=key, capture_output=True, timeout=60).returncode == 0:
                return True
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(pause_s)


def _probe(launcher: object) -> bool:
    from orchestra.sandbox.nemoclaw.persistent import PersistentTransport

    transport = PersistentTransport(launcher, startup_timeout_s=60)  # type: ignore[arg-type]
    try:
        payload = json.dumps({"sandbox": SANDBOX, "tool": "orchestra_live_probe"})
        for index in range(3):
            started = time.monotonic()
            reply = json.loads(transport.exchange(SANDBOX, payload, 30))
            print(f"  probe {index + 1}: error={reply.get('error')} ({time.monotonic() - started:.3f}s)")
            if reply.get("error") != "worker_tool_forbidden":
                return False
        return True
    except RuntimeError as error:
        print(f"  probe failed: {error}")
        return False
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    """Stage the context; with --yes also create the sandbox, install its key and probe the worker."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python-base", help="reviewed base image, e.g. python:3.11-slim@sha256:<digest>")
    parser.add_argument("--yes", action="store_true", help="create the sandbox (otherwise only show the plan)")
    parser.add_argument("--probe-only", action="store_true", help="probe an existing worker sandbox")
    parser.add_argument("--key-file", type=Path, default=None,
                        help="host master key (default: $ORCHESTRA_CAPABILITY_KEY_FILE or ~/.config/orchestra/)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from orchestra.sandbox.nemoclaw.cli import NemoClawCli
    from orchestra.sandbox.nemoclaw.ssh_launcher import OpenShellSshLauncher
    from orchestra.security.capability import derive_sandbox_key

    openshell, ssh = os.environ.get("OPENSHELL_BIN", "openshell"), os.environ.get("SSH_BIN", "ssh")
    launcher = OpenShellSshLauncher(NemoClawCli(openshell=openshell, dialect="modern"), ssh=ssh)
    if args.probe_only:
        passed = _probe(launcher)
        print(f"WORKER_PROBE={'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    if not args.python_base:
        parser.error("--python-base is required")
    key_file = args.key_file or Path(os.environ.get("ORCHESTRA_CAPABILITY_KEY_FILE")
                                     or Path.home() / ".config" / "orchestra" / "capability.key")
    policy = ROOT / "nemoclaw" / "policy-worker.yaml"
    with tempfile.TemporaryDirectory(prefix="orchestra-worker-") as directory:
        context = Path(directory)
        try:
            count = stage_context(context, args.python_base)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f"FAIL staging: {error}")
            return 1
        command = create_command(openshell, context, policy)
        print(f"staged {count} files")
        print("create:", shlex.join(command))
        print(f"then: install the derived key at {KEY_PATH} over ssh and probe the worker")
        if not args.yes:
            print("DRY RUN - nothing was created. Add --yes to create the sandbox.")
            return 0
        if shutil.which(openshell) is None or shutil.which(ssh) is None:
            print("FAIL openshell and ssh must both be on PATH")
            return 1
        try:
            master = load_or_create_master_key(key_file)
            subprocess.run(command, shell=False, check=True, timeout=1800)
            if not install_key(launcher, derive_sandbox_key(master, SANDBOX)):
                print("FAIL could not install the key; the sandbox exists but the worker cannot start")
                print(f"remove with: {openshell} sandbox delete {SANDBOX}")
                return 1
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as error:
            # Messages here come from argument lists and fixed codes; key bytes are only ever stdin.
            print(f"FAIL {type(error).__name__}: {error}")
            return 1
    print("key installed")
    passed = _probe(launcher)
    print(f"WORKER_PROBE={'PASS' if passed else 'FAIL'}")
    print(f"remove with: {openshell} sandbox delete {SANDBOX}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
