"""Load plugin prompt files (agents/*.md, skills/*/SKILL.md) as system prompts.

Handles optional YAML-style frontmatter without extra dependencies.
Usage: roles = load_prompts("path/to/plugin")  # {"name": Prompt}
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Prompt:
    name: str
    meta: dict[str, str]
    body: str


def parse(text: str, default_name: str) -> Prompt:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip("\"'")
            body = text[end + 4 :].lstrip("\n")
    return Prompt(meta.get("name", default_name), meta, body.strip())


def load_prompts(plugin_dir: str | Path) -> dict[str, Prompt]:
    root = Path(plugin_dir)
    files = list(root.glob("agents/*.md")) + list(root.glob("skills/*/SKILL.md"))
    out: dict[str, Prompt] = {}
    for f in sorted(files):
        default = f.parent.name if f.name == "SKILL.md" else f.stem
        p = parse(f.read_text(encoding="utf-8"), default)
        out[p.name] = p
    return out
