"""Read-only Claude plugin prompt catalog."""

from .frontmatter import Prompt, parse_markdown

__all__ = ["Prompt", "parse_markdown"]
