"""Resolve installed runtime dependency versions for a pinned worker lock and SBOM."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def installed_runtime() -> dict[str, str]:
    """Walk only declared runtime dependencies, excluding dev/optional-extra requirements."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pending = [Requirement(dep) for dep in project["project"]["dependencies"]]
    resolved: dict[str, str] = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in resolved:
            if metadata.version(name) not in requirement.specifier:
                raise ValueError("installed_dependency_constraint_mismatch")
            continue
        dist = metadata.distribution(name)
        if dist.version not in requirement.specifier:
            raise ValueError("installed_dependency_constraint_mismatch")
        resolved[name] = dist.version
        pending.extend(Requirement(dep) for dep in dist.requires or [])
    return dict(sorted(resolved.items()))


def main(argv: list[str] | None = None) -> int:
    """Write or check the exact installed runtime snapshot; no downloads are performed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    installed = installed_runtime()
    lock = "# Runtime versions resolved locally; verify artifact hashes in your build pipeline.\n" + "".join(
        f"{name}=={version}\n" for name, version in installed.items())
    sbom = json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "components": [
        {"type": "library", "name": name, "version": version, "purl": f"pkg:pypi/{name}@{version}"}
        for name, version in installed.items()]}, indent=2) + "\n"
    outputs = {ROOT / "nemoclaw/requirements-worker.lock": lock, ROOT / "nemoclaw/sbom-worker.json": sbom}
    if args.check:
        return int(any(not path.is_file() or path.read_text(encoding="utf-8") != value for path, value in outputs.items()))
    for path, value in outputs.items():
        path.write_text(value, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
