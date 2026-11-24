"""Scan vendored plugin declarations as data, preserving plugin namespaces."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..org import REPO_ROOT
from .assets import read_asset
from .frontmatter import Prompt, parse_markdown
from .skills import Skill

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LSP_COMMANDS = frozenset({"pyright-langserver", "jdtls"})


@dataclass
class CatalogPrompt:
    """Agent or command data; declared tools have no execution implementation here."""

    id: str
    plugin: str
    path: Path
    prompt: Prompt


@dataclass
class Plugin:
    """Original manifest and inert hooks alongside namespaced catalog entries."""

    id: str
    manifest: dict[str, Any]
    hooks: dict[str, Any] = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    availability: dict[str, bool] = field(default_factory=dict)


@dataclass
class PluginCatalog:
    """Read-only catalog of agents, skill summaries, commands and hook metadata."""

    root: Path
    plugins: dict[str, Plugin] = field(default_factory=dict)
    agents: dict[str, CatalogPrompt] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    commands: dict[str, CatalogPrompt] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: str | Path | None = None) -> PluginCatalog:
        """Scan a selected root or CLAUDE_PLUGINS_DIR; never import or run plugin code."""
        selected = Path(root if root is not None else os.environ.get(
            "CLAUDE_PLUGINS_DIR", str(REPO_ROOT / "third_party/claude_plugins/plugins"),
        )).resolve()
        catalog = cls(selected)
        if not selected.is_dir():
            catalog.errors.append("plugin root is unavailable")
            return catalog
        for directory in sorted(selected.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if not _NAME.fullmatch(directory.name):
                catalog.errors.append("invalid plugin directory name")
                continue
            try:
                catalog._scan_plugin(directory)
            except (ValueError, OSError):
                catalog.errors.append(f"{directory.name}: invalid catalog data")
        if not catalog.plugins:
            catalog.errors.append("no valid plugins found")
        return catalog

    def _json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(read_asset(path, self.root))
        except json.JSONDecodeError:
            raise ValueError("invalid catalog JSON") from None
        if not isinstance(data, dict):
            raise ValueError("catalog JSON must be an object")
        return data

    def _prompt(self, path: Path, default: str) -> Prompt:
        prompt = parse_markdown(read_asset(path, self.root), default)
        if not _NAME.fullmatch(prompt.name):
            raise ValueError("invalid catalog prompt name")
        relative = path.relative_to(self.root).as_posix()
        self.warnings.extend(f"{relative}: {message}" for message in prompt.warnings)
        return prompt

    def _add_prompt(self, path: Path, plugin: Plugin, *, command: bool = False) -> None:
        prompt = self._prompt(path, path.stem)
        identifier = f"{plugin.id}/{prompt.name}"
        entries = self.commands if command else self.agents
        ids = plugin.commands if command else plugin.agents
        if identifier in entries:
            raise ValueError("duplicate catalog prompt id")
        entries[identifier] = CatalogPrompt(identifier, plugin.id, path, prompt)
        ids.append(identifier)

    def _scan_plugin(self, directory: Path) -> None:
        manifest = self._json(directory / ".claude-plugin/plugin.json")
        if not isinstance(manifest.get("name"), str) or not manifest["name"]:
            raise ValueError("manifest name missing")
        plugin = Plugin(directory.name, manifest)
        self.plugins[plugin.id] = plugin
        hook_file = directory / "hooks/hooks.json"
        if hook_file.exists():
            plugin.hooks = self._json(hook_file)
            if not isinstance(plugin.hooks.get("hooks"), dict):
                raise ValueError("invalid hooks mapping")
        servers = manifest.get("lspServers", {})
        if isinstance(servers, dict):
            for server in servers.values():
                command = server.get("command") if isinstance(server, dict) else None
                if isinstance(command, str):
                    plugin.availability[command] = command in _LSP_COMMANDS and shutil.which(command) is not None
        for path in sorted((directory / "agents").glob("*.md")):
            self._add_prompt(path, plugin)
        for path in sorted((directory / "commands").glob("*.md")):
            self._add_prompt(path, plugin, command=True)
        for path in sorted((directory / "skills").rglob("SKILL.md")):
            prompt = self._prompt(path, path.parent.name)
            identifier = f"{plugin.id}/{prompt.name}"
            if identifier in self.skills:
                raise ValueError("duplicate skill id")
            references = {
                ref.relative_to(path.parent / "references").as_posix(): ref
                for ref in sorted((path.parent / "references").rglob("*.md"))
            }
            self.skills[identifier] = Skill(
                identifier, prompt.name, prompt.description, path, path.parent.resolve(strict=True), references,
            )
            plugin.skills.append(identifier)
            for agent in sorted((path.parent / "agents").glob("*.md")):
                self._add_prompt(agent, plugin)

    def summary(self) -> dict[str, int]:
        """Return counts without loading skill bodies."""
        return {"plugins": len(self.plugins), "agents": len(self.agents),
                "skills": len(self.skills), "commands": len(self.commands)}
