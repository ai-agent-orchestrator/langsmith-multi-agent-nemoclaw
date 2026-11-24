"""Offline catalog list/show/validate CLI. Hooks and declared tools remain inert."""
from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any

from ..filters.builtin.pii_mask import mask_any
from .claude_plugins import PluginCatalog
from .skills import SkillLoader


def _show(catalog: PluginCatalog, identifier: str) -> dict[str, Any] | None:
    if identifier in catalog.plugins:
        plugin = catalog.plugins[identifier]
        return {"id": identifier, "manifest": plugin.manifest, "agents": plugin.agents,
                "skills": plugin.skills, "commands": plugin.commands, "hooks": plugin.hooks,
                "availability": plugin.availability}
    for kind, entries in (("agent", catalog.agents), ("command", catalog.commands)):
        if identifier in entries:
            entry = entries[identifier]
            return {"id": entry.id, "kind": kind, "metadata": entry.prompt.meta,
                    "source": entry.path.relative_to(catalog.root).as_posix(), "body": entry.prompt.body}
    for summary in SkillLoader(catalog.skills).index():
        if summary["id"] == identifier:
            return {"kind": "skill", **summary}
    return None


def main(argv: list[str] | None = None) -> int:
    """Inspect catalog metadata locally and report invalid data with a nonzero exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="plugin root (default: CLAUDE_PLUGINS_DIR or vendored snapshot)")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list")
    show = subparsers.add_parser("show")
    show.add_argument("id")
    subparsers.add_parser("validate")
    args = parser.parse_args(argv)
    catalog = PluginCatalog.load(args.root)
    if args.action == "list":
        print("PLUGIN                       AGENTS SKILLS COMMANDS HOOKS AVAILABILITY")
        for name, plugin in sorted(catalog.plugins.items()):
            availability = ",".join(f"{k}:{'available' if v else 'unavailable'}"
                                    for k, v in sorted(plugin.availability.items())) or "data-only"
            print(f"{name:<28} {len(plugin.agents):>6} {len(plugin.skills):>6} "
                  f"{len(plugin.commands):>8} {str(bool(plugin.hooks)):>5} {availability}")
        print(", ".join(f"{count} {kind}" for kind, count in catalog.summary().items()))
    elif args.action == "show":
        result = _show(catalog, args.id)
        if result is None:
            print("Unknown catalog id")
            return 1
        print(json.dumps(mask_any(result), ensure_ascii=False, indent=2, default=str))
    else:
        print("INVALID" if catalog.errors else "VALID")
        print(json.dumps(catalog.summary(), sort_keys=True))
    for warning in catalog.warnings:
        print(f"WARNING: {warning}")
    for error in catalog.errors:
        print(f"ERROR: {error}")
    return int(bool(catalog.errors))


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
