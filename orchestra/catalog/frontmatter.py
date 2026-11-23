"""Parse only leading YAML frontmatter; normalize Claude prompt metadata."""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Any

import yaml

_HEADER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)
_MODELS = frozenset({"haiku", "sonnet", "opus", "inherit", "local"})


@dataclass
class Prompt:
    """Normalized metadata and body, compatible with the upstream Prompt fields."""

    name: str
    meta: dict[str, Any]
    body: str
    warnings: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        """Return the description without flattening multiline YAML scalars."""
        return str(self.meta.get("description", ""))

    @property
    def tools(self) -> list[str]:
        """Return normalized tool names in declaration order."""
        return list(self.meta.get("tools", []))

    @property
    def model(self) -> str:
        """Return a supported model alias, defaulting to inherit."""
        return str(self.meta.get("model", "inherit"))


def _commentless(value: str) -> str:
    return value.split("#", 1)[0].strip()


def parse_markdown(text: str, default_name: str) -> Prompt:
    """Read leading safe YAML, using the original line parser only on YAML errors."""
    notices: list[str] = []

    def warn(message: str) -> None:
        notices.append(message)
        warnings.warn(message, UserWarning, stacklevel=3)

    match = _HEADER.match(text)
    meta: dict[str, Any] = {}
    body = text
    if match:
        header = match.group(1)
        body = text[match.end():]
        try:
            loaded = yaml.safe_load(header)
            if loaded is not None and not isinstance(loaded, dict):
                raise yaml.YAMLError("metadata must be a mapping")
            meta = {k: v for k, v in (loaded or {}).items() if isinstance(k, str)}
        except yaml.YAMLError:
            # Reuse the vendored parser without letting its permissive delimiter search read the body.
            from third_party.multi_agent_module_lib.prompt_loader import parse

            warn("invalid frontmatter YAML; using legacy parser")
            legacy = parse("---\n" + header.replace("\r\n", "\n") + "\n---\n", default_name)
            meta = dict(legacy.meta)

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        name = default_name
    meta["name"] = name.strip()
    description = meta.get("description", "")
    if not isinstance(description, str):
        warn("invalid description type; using empty description")
        description = ""
    meta["description"] = description

    raw_tools = meta.get("tools", [])
    if isinstance(raw_tools, str):
        raw_tools = [raw_tools]
    if not isinstance(raw_tools, list):
        warn("invalid tools type; using empty tools")
        raw_tools = []
    tools: list[str] = []
    for raw in raw_tools:
        if not isinstance(raw, str):
            warn("invalid tool entry; ignored")
            continue
        for line in raw.splitlines():
            for token in _commentless(line).split(","):
                tool = token.strip()
                if tool and tool not in tools:
                    tools.append(tool)
    meta["tools"] = tools

    raw_model = meta.get("model", "inherit")
    model = _commentless(raw_model).lower() if isinstance(raw_model, str) else ""
    if model not in _MODELS:
        warn("unsupported model; using inherit")
        model = "inherit"
    meta["model"] = model
    return Prompt(meta["name"], meta, body.strip(), notices)
