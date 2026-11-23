"""Read-only Claude plugin prompt catalog."""

from .frontmatter import Prompt, parse_markdown
from .claude_plugins import PluginCatalog

__all__ = ["Prompt", "parse_markdown", "PluginCatalog"]
