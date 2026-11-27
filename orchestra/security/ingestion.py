"""Pin/hash checks and scanner admission before executable catalog exposure."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .scanner import StaticScanner, require_scan


def admit_plugin(directory: Path, catalog_root: Path) -> bool:
    """Scan all plugin bytes; return trusted only for an exact pinned lock snapshot."""
    files = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("plugin_symlink_forbidden")
        if path.is_file():
            if path.stat().st_size > 2_000_000:
                raise ValueError("plugin_asset_too_large")
            files[path.relative_to(catalog_root.parent).as_posix()] = path.read_bytes()
    require_scan(files, StaticScanner())
    lock_path = catalog_root.parent.parent / "LOCK.json"
    if not lock_path.is_file():
        return False
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    source = lock["sources"]["claude_plugins"]
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("plugin_commit_not_pinned")
    prefix = "plugins/" + directory.name + "/"
    expected = {name: digest for name, digest in source["files"].items() if name.startswith(prefix)}
    actual = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if not expected or expected != actual:
        raise ValueError("plugin_lock_mismatch")
    return True
