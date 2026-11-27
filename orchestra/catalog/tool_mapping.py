"""Claude Code read tools as local tools confined to `ORCHESTRA_WORKSPACE` (DESIGN §7.3).

Every path is opened one component at a time relative to the workspace descriptor without following
symlinks, so a link swapped in between a check and a read cannot redirect it. Each tool bounds its own
work so it ends well inside the worker's time limit; search takes plain text, not a regular expression,
which keeps its cost proportional to the input.
"""
from __future__ import annotations

import os
import stat
import time
from contextlib import contextmanager
from fnmatch import fnmatchcase
from functools import wraps
from typing import Any, Callable, Iterable, Iterator

from langchain_core.tools import BaseTool, tool

from ..specs import SideEffect, ToolSpec

WORKSPACE_ENV = "ORCHESTRA_WORKSPACE"
MAX_PATH_CHARS = 512
MAX_PATTERN_CHARS = 200
MAX_QUERY_CHARS = 200
MAX_FILE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 100_000
MAX_LINES = 2_000
MAX_ENTRIES = 500
MAX_SCANNED = 5_000
MAX_DEPTH = 12
MAX_MATCHES = 100
MAX_LINE_CHARS = 300
MAX_SEARCH_BYTES = 20_000_000
SEARCH_BUDGET_S = 10.0

CLAUDE_TOOLS = {"Read": "fs_read", "LS": "fs_list", "Glob": "fs_glob", "Grep": "fs_grep"}

_DENIED_NAMES = frozenset({".git", ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ecdsa",
                           "id_ed25519", "credentials", "credentials.json"})
_DENIED_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx")
_ALLOWED_NAMES = frozenset({".env.example"})


class _Refused(Exception):
    """Carries only a fixed error code; paths and file contents never travel in messages."""


def map_claude_tools(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split Claude Code tool names into local tool names and the names that have no local equivalent."""
    mapped: list[str] = []
    dropped: list[str] = []
    for name in names:
        if name in CLAUDE_TOOLS:
            mapped.append(CLAUDE_TOOLS[name])
        else:
            dropped.append(name)
    return mapped, dropped


def _denied(name: str) -> bool:
    lowered = name.lower()
    if lowered in _ALLOWED_NAMES:
        return False
    return lowered in _DENIED_NAMES or any(fnmatchcase(lowered, pattern) for pattern in _DENIED_PATTERNS)


def _parts(path: str) -> list[str]:
    if len(path) > MAX_PATH_CHARS or "\x00" in path or "\\" in path or path.startswith("/"):
        raise _Refused("path_not_allowed")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." or _denied(part) for part in parts):
        raise _Refused("path_not_allowed")
    return parts


@contextmanager
def _open(parts: list[str]) -> Iterator[int]:
    """Yield a read descriptor for a workspace entry, refusing symlinks at every step."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise _Refused("workspace_unsupported_platform")
    root = os.environ.get(WORKSPACE_ENV, "")
    if not os.path.isabs(root) or os.path.realpath(root) == os.sep:
        raise _Refused("workspace_not_configured")
    try:
        descriptor = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise _Refused("workspace_not_configured") from None
    try:
        # O_NONBLOCK keeps a named pipe from stalling the open; the type check follows.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        for index, part in enumerate(parts):
            try:
                if stat.S_ISLNK(os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode):
                    raise _Refused("path_not_allowed")
                following = os.open(part, flags | (os.O_DIRECTORY if index < len(parts) - 1 else 0), dir_fd=descriptor)
            except FileNotFoundError:
                raise _Refused("not_found") from None
            except OSError:
                raise _Refused("path_not_allowed") from None
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _read_text(parts: list[str]) -> str:
    with _open(parts) as descriptor:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _Refused("not_a_file")
        if info.st_nlink > 1:  # a second name could point at a file outside the workspace
            raise _Refused("path_not_allowed")
        if info.st_size > MAX_FILE_BYTES:
            raise _Refused("file_too_large")
        chunks, size = [], 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    if size > MAX_FILE_BYTES:
        raise _Refused("file_too_large")
    data = b"".join(chunks)
    if b"\x00" in data:
        raise _Refused("not_text")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise _Refused("not_text") from None


def _entries(parts: list[str], limit: int) -> tuple[list[tuple[str, str, int]], bool]:
    """Return up to `limit` visible (name, type, size) entries of one directory and whether more exist."""
    with _open(parts) as descriptor:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _Refused("not_a_directory")
        found: list[tuple[str, str, int]] = []
        with os.scandir(descriptor) as scan:
            for entry in scan:
                if _denied(entry.name):
                    continue
                if len(found) >= limit:
                    return sorted(found), True
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = ("symlink" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode)
                        else "file" if stat.S_ISREG(info.st_mode) else "other")
                found.append((entry.name, kind, info.st_size if kind == "file" else 0))
    return sorted(found), False


class _Walk:
    """Depth-first, name-ordered walk of regular files that records whether it had to stop early."""

    def __init__(self, base: list[str], deadline: float) -> None:
        self.base, self.deadline, self.truncated = base, deadline, False

    def files(self) -> Iterator[list[str]]:
        scanned = 0
        # The base must be a readable directory; failures below it only skip that branch.
        listing, more = _entries(self.base, MAX_SCANNED)
        self.truncated = more
        stack = [(self.base, name, kind) for name, kind, _ in reversed(listing)]
        while stack:
            parent, name, kind = stack.pop()
            scanned += 1
            if scanned > MAX_SCANNED or time.monotonic() >= self.deadline:
                self.truncated = True
                return
            path = [*parent, name]
            if kind == "file":
                yield path
            elif kind == "dir":
                if len(path) - len(self.base) >= MAX_DEPTH:
                    self.truncated = True
                    continue
                try:
                    children, more = _entries(path, MAX_SCANNED)
                except _Refused:
                    continue
                self.truncated = self.truncated or more
                stack.extend((path, child, child_kind) for child, child_kind, _ in reversed(children))


def _pattern(pattern: str) -> list[str]:
    if not pattern or len(pattern) > MAX_PATTERN_CHARS or "\x00" in pattern or pattern.startswith("/"):
        raise _Refused("pattern_not_allowed")
    segments = [segment for segment in pattern.split("/") if segment not in ("", ".")]
    if not segments or ".." in segments:
        raise _Refused("pattern_not_allowed")
    return segments


def _matches(pattern: list[str], path: list[str]) -> bool:
    """Segment-wise glob where `**` spans any number of directories, without backtracking."""
    reachable = {0}
    for segment in pattern:
        if segment == "**":
            reachable = set(range(min(reachable), len(path) + 1))
        else:
            reachable = {i + 1 for i in reachable if i < len(path) and fnmatchcase(path[i], segment)}
        if not reachable:
            return False
    return len(path) in reachable


def _codes(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return function(*args, **kwargs)
        except _Refused as refused:
            return {"error": str(refused)}
        except OSError:
            return {"error": "cannot_read"}

    return run


@tool
@_codes
def fs_read(path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
    """Read lines of a UTF-8 text file. `path` is relative to the workspace."""
    if start_line < 1 or not 1 <= max_lines <= MAX_LINES:
        raise _Refused("invalid_range")
    parts = _parts(path)
    lines = _read_text(parts).splitlines(keepends=True)
    content = "".join(lines[start_line - 1:start_line - 1 + max_lines])
    truncated = start_line - 1 + max_lines < len(lines) or len(content) > MAX_OUTPUT_CHARS
    return {"path": "/".join(parts), "start_line": start_line, "content": content[:MAX_OUTPUT_CHARS],
            "total_lines": len(lines), "truncated": truncated}


@tool
@_codes
def fs_list(path: str = ".") -> dict[str, Any]:
    """List one workspace directory. `path` is relative to the workspace."""
    parts = _parts(path)
    entries, truncated = _entries(parts, MAX_ENTRIES)
    return {"path": "/".join(parts) or ".", "truncated": truncated,
            "entries": [{"name": name, "type": kind, "size": size} for name, kind, size in entries]}


@tool
@_codes
def fs_glob(pattern: str, path: str = ".") -> dict[str, Any]:
    """Find files by glob (`*`, `?`, `**`) below a workspace directory."""
    base, segments = _parts(path), _pattern(pattern)
    walk = _Walk(base, time.monotonic() + SEARCH_BUDGET_S)
    matches: list[str] = []
    for file in walk.files():
        if _matches(segments, file[len(base):]):
            if len(matches) >= MAX_ENTRIES:
                walk.truncated = True
                break
            matches.append("/".join(file))
    return {"matches": matches, "truncated": walk.truncated}


@tool
@_codes
def fs_grep(query: str, path: str = ".", glob: str = "**") -> dict[str, Any]:
    """Search for plain text (not a regular expression) in one file or below a workspace directory."""
    if not 0 < len(query) <= MAX_QUERY_CHARS:
        raise _Refused("invalid_query")
    base, segments = _parts(path), _pattern(glob)
    with _open(base) as descriptor:
        single = not stat.S_ISDIR(os.fstat(descriptor).st_mode)
    walk = _Walk(base, time.monotonic() + SEARCH_BUDGET_S)
    matches: list[dict[str, Any]] = []
    budget = MAX_SEARCH_BYTES
    for file in [base] if single else walk.files():
        if not single and not _matches(segments, file[len(base):]):
            continue
        try:
            text = _read_text(file)
        except _Refused:
            if single:
                raise
            continue
        budget -= len(text.encode())
        if budget < 0:
            walk.truncated = True
            break
        for number, line in enumerate(text.splitlines(), 1):
            if query in line:
                if len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True}
                matches.append({"path": "/".join(file), "line": number, "text": line[:MAX_LINE_CHARS]})
    return {"matches": matches, "truncated": walk.truncated}


def workspace_tools(sandbox_name: str) -> dict[str, tuple[BaseTool, ToolSpec]]:
    """The read tools with specs that route them to the given sandbox."""
    tools: tuple[BaseTool, ...] = (fs_read, fs_list, fs_glob, fs_grep)
    return {t.name: (t, ToolSpec(name=t.name, side_effect=SideEffect.READ, sensitivity="internal",
                                 sandbox="required", sandbox_name=sandbox_name)) for t in tools}
