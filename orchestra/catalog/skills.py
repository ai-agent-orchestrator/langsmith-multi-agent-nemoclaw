"""Progressive disclosure of registered skills and references through READ tools."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from langchain_core.tools import BaseTool, tool

from ..filters.builtin.pii_mask import mask_text
from ..specs import SideEffect, ToolSpec
from .assets import read_asset
from .frontmatter import parse_markdown

HARD_MAX_CHARS = 20_000


@dataclass
class Skill:
    """A skill summary retains file locations, never its prompt body."""

    id: str
    name: str
    description: str
    path: Path
    root: Path
    references: dict[str, Path] = field(default_factory=dict)


class SkillLoader:
    """Load only known catalog assets on demand, with a hard character ceiling."""

    def __init__(self, skills: dict[str, Skill], max_chars: int | None = None) -> None:
        try:
            configured = int(os.environ.get("ORCHESTRA_SKILL_MAX_CHARS", str(HARD_MAX_CHARS))) \
                if max_chars is None else max_chars
        except ValueError:
            raise ValueError("invalid ORCHESTRA_SKILL_MAX_CHARS") from None
        if configured <= 0:
            raise ValueError("skill character limit must be positive")
        self.max_chars = min(configured, HARD_MAX_CHARS)
        self.skills = skills

    def index(self) -> list[dict[str, str]]:
        """Expose names and descriptions without loading full skill bodies."""
        return [
            {"id": skill.id, "name": skill.name, "description": skill.description}
            for _, skill in sorted(self.skills.items())
        ]

    def _skill(self, name: str) -> Skill:
        if name in self.skills:
            return self.skills[name]
        matches = [s for s in self.skills.values() if s.name == name]
        if len(matches) != 1:
            raise ValueError("unknown or ambiguous skill; use catalog id")
        return matches[0]

    def _bounded(self, text: str) -> str:
        text = mask_text(text)
        if len(text) <= self.max_chars:
            return text
        headings = [line.strip() for line in text.splitlines() if line.startswith("#")]
        toc = ("[truncated]\nContents:\n" + "\n".join(headings))[:self.max_chars // 3]
        header = toc + "\n\n"
        return (header + text[:max(0, self.max_chars - len(header))])[:self.max_chars]

    def load_skill(self, name: str) -> str:
        """Return the requested skill body, or contents and prefix when truncated."""
        skill = self._skill(name)
        prompt = parse_markdown(read_asset(skill.path, skill.root), skill.name)
        return self._bounded(prompt.body)

    def load_skill_reference(self, name: str, file: str) -> str:
        """Read a registered reference; user input is never joined to a file path."""
        skill = self._skill(name)
        path = PurePosixPath(file)
        if path.is_absolute() or ".." in path.parts or "\\" in file or ":" in file:
            raise ValueError("invalid skill reference")
        if file not in skill.references:
            raise ValueError("unknown skill reference")
        return self._bounded(read_asset(skill.references[file], skill.root))


def make_skill_tools(skills: dict[str, Skill]) -> dict[str, tuple[BaseTool, ToolSpec]]:
    """Create only READ tools; this catalog provides no shell or file-writing tools."""
    loader = SkillLoader(skills)

    @tool
    def load_skill(name: str) -> str:
        """Read a catalog skill by plugin/skill ID; content is capped at 20000 characters."""
        return loader.load_skill(name)

    @tool
    def load_skill_reference(name: str, file: str) -> str:
        """Read a named reference registered under a catalog skill."""
        return loader.load_skill_reference(name, file)

    return {
        t.name: (t, ToolSpec(name=t.name, side_effect=SideEffect.READ))
        for t in (load_skill, load_skill_reference)
    }
