"""Vendor allowlisted Git blobs at an exact commit; --check is entirely offline."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPOS = {
    "claude_plugins": "https://github.com/ai-agent-orchestrator/claude-10-plugin-for-multi-agent.git",
    "multi_agent_module_lib": "https://github.com/ai-agent-orchestrator/multi-agent-module-lib.git",
}
MODULES = {"guardrail", "loop_control", "review_aggregator", "prompt_loader", "orchestrator", "cli_subagent"}
GIT_COMMANDS = frozenset({"clone", "archive", "rev-parse", "remote"})
NOTICE = """# Third-party notices

See LOCK.json for repository URLs, exact commits, source paths, and SHA-256 hashes.
Original selected files are copied byte-for-byte from Git blobs. Only generated
__init__.py package markers and this notice are local additions.

## Claude plugins
Anthropic and the contributors named in the retained manifests and notices.
Apache License 2.0: see LICENSE, individual plugin LICENSE files, and the original
claude_plugins/THIRD_PARTY_NOTICES.md for authorship and upstream adaptations.
Only the DESIGN §7.1 data/code allowlist is redistributed; hooks are never run.

## Multi-agent module library
Source: ai-agent-orchestrator/multi-agent-module-lib, examples/*.py and original tests.
The pinned repository contains no LICENSE or copyright grant for these examples.
The plugin Apache license must not be interpreted as licensing these independently
authored examples. License clarification remains necessary before redistribution
outside this repository's authorized development context.
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError("unsafe vendor path")
    return p


def _git(cwd: Path, verb: str, *args: str) -> bytes:
    if verb not in GIT_COMMANDS:
        raise ValueError("git command not allowlisted")
    return subprocess.run(
        ["git", verb, *args], cwd=cwd, shell=False, timeout=120,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def select_files(blobs: dict[str, bytes], component: str) -> dict[str, bytes]:
    """Select the design allowlist; flatten module examples for package imports."""
    if component not in REPOS:
        raise ValueError("unknown component")
    out = {}
    for name, data in sorted(blobs.items()):
        p = _safe_path(name)
        if component == "multi_agent_module_lib":
            if name in {f"examples/{m}.py" for m in MODULES} or name == "examples/tests/test_examples.py":
                out[name.removeprefix("examples/")] = data
            elif p.name.startswith(("LICENSE", "NOTICE")):
                out[name] = data
            continue
        parts = p.parts
        if name in {"THIRD_PARTY_NOTICES.md", "plugins.lock.json"}:
            out[name] = data
            continue
        if len(parts) < 3 or parts[0] != "plugins":
            continue
        sub = PurePosixPath(*parts[2:])
        allowed = (
            p.name.startswith(("LICENSE", "NOTICE"))
            or sub.match("agents/*.md") and len(sub.parts) == 2
            or sub.match("commands/*.md") and len(sub.parts) == 2
            or sub.as_posix() == ".claude-plugin/plugin.json"
            or sub.as_posix() == "hooks/hooks.json"
            or (sub.parts[0] == "skills" and (
                p.name == "SKILL.md" or p.suffix == ".md" and any(
                    d in sub.parts[1:-1] for d in ("agents", "references")
                )
            ))
            or parts[1] == "hookify" and (
                sub.match("examples/*.local.md") or sub.match("core/*.py")
            )
            or name == "plugins/security-guidance/hooks/patterns.py"
        )
        if allowed:
            out[name] = data
    return out


def _snapshot(source: str, commit: str, component: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError("commit must be a full 40-character Git SHA")

    def read(repo: Path) -> dict[str, bytes]:
        remote = _git(repo, "remote", "get-url", "origin").decode().strip()
        if remote.removesuffix(".git") != REPOS[component].removesuffix(".git"):
            raise ValueError("source origin does not match allowlisted repository")
        actual = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        if actual != commit.lower():
            raise ValueError("commit mismatch")
        blobs = {}
        with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", commit))) as archive:
            for member in archive:
                _safe_path(member.name)
                if member.issym() or member.islnk():
                    raise ValueError("symlink in source archive")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is not None:
                        blobs[member.name] = stream.read()
        return blobs

    if source.startswith("https://"):
        if source.removesuffix(".git") != REPOS[component].removesuffix(".git"):
            raise ValueError("URL not allowlisted")
        with tempfile.TemporaryDirectory(prefix="orchestra-vendor-") as tmp:
            repo = Path(tmp) / "source"
            _git(Path(tmp), "clone", "--no-checkout", "--", source, str(repo))
            return read(repo)
    return read(Path(source).resolve(strict=True))


def _write(root: Path, relative: str, data: bytes) -> None:
    path = root / _safe_path(relative)
    if root.resolve() not in path.resolve().parents or path.is_symlink():
        raise ValueError("vendor path escapes destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def sync(source: str, commit: str, component: str, root: Path = ROOT) -> None:
    """Copy selected immutable Git blobs and update the lock; never edit source repos."""
    blobs = _snapshot(source, commit, component)
    selected = select_files(blobs, component)
    if not selected:
        raise ValueError("no allowlisted files in source")
    vendor = root / "third_party"
    lock_path = vendor / "LOCK.json"
    lock: dict[str, Any] = json.loads(lock_path.read_text()) if lock_path.exists() else {
        "schema_version": 1, "sources": {}, "metadata": {},
    }
    generated = {"__init__.py": b'"""Generated package marker; original files are unmodified."""\n'}
    if component == "multi_agent_module_lib":
        missing = {f"{m}.py" for m in MODULES} - selected.keys()
        if missing or "tests/test_examples.py" not in selected:
            raise ValueError("module snapshot is incomplete")
    destination = vendor / component
    previous = lock["sources"].get(component, {})
    for name in set(previous.get("files", {})) | set(previous.get("generated_files", {})):
        if name not in selected and name not in generated:
            path = destination / _safe_path(name)
            if destination.resolve() not in path.resolve().parents or path.is_symlink():
                raise ValueError("unsafe stale vendor path")
            path.unlink(missing_ok=True)
    for name, data in {**selected, **generated}.items():
        _write(vendor, f"{component}/{name}", data)
    lock["sources"][component] = {
        "repo": REPOS[component], "commit": commit.lower(),
        "files": {n: _digest(b) for n, b in selected.items()},
        "source_paths": {n: f"examples/{n}" if component == "multi_agent_module_lib" and (
            n.endswith(".py")
        ) else n for n in selected},
        "generated_files": {n: _digest(b) for n, b in generated.items()},
    }
    if component == "claude_plugins":
        license_data = blobs["plugins/feature-dev/LICENSE"]
        _write(vendor, "LICENSE", license_data)
        lock["metadata"]["LICENSE"] = _digest(license_data)
    notice_data = NOTICE.encode("utf-8")
    _write(vendor, "NOTICE.md", notice_data)
    lock["metadata"]["NOTICE.md"] = _digest(notice_data)
    _write(vendor, "LOCK.json", (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode())


def check(root: Path = ROOT) -> list[str]:
    """Detect missing, changed, unexpected files and symlinks using only the lock."""
    vendor = root / "third_party"
    lock_path = vendor / "LOCK.json"
    if not lock_path.is_file():
        return ["missing LOCK.json"]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected = dict(lock["metadata"])
    errors = []
    for component, record in lock["sources"].items():
        if component not in REPOS or record["repo"] != REPOS[component]:
            errors.append("unknown source")
        if not re.fullmatch(r"[0-9a-f]{40}", record["commit"]):
            errors.append("invalid commit")
        for name, digest in {**record["files"], **record["generated_files"]}.items():
            expected[f"{component}/{name}"] = digest
    for name, digest in expected.items():
        path = vendor / _safe_path(name)
        if vendor.resolve() not in path.resolve().parents or path.is_symlink():
            errors.append(f"unsafe path: {name}")
        elif not path.is_file():
            errors.append(f"missing: {name}")
        elif _digest(path.read_bytes()) != digest:
            errors.append(f"changed: {name}")
    for path in vendor.rglob("*"):
        relative = path.relative_to(vendor).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            errors.append(f"symlink: {relative}")
        elif path.is_file() and relative != "LOCK.json" and relative not in expected:
            errors.append(f"unexpected: {relative}")
    return errors


def main() -> int:
    """Run explicit source synchronization or an offline integrity check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--component", choices=sorted(REPOS))
    parser.add_argument("--source")
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.check:
        if args.source or args.commit or args.component:
            parser.error("--check cannot be combined with sync arguments")
    else:
        if not all((args.source, args.commit, args.component)):
            parser.error("sync requires --component, --source, and --commit")
        sync(args.source, args.commit, args.component)
    errors = check()
    for error in errors:
        print(error)
    if not errors:
        print("OK (no drift)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
