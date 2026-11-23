"""Prompt Hub operations with local fallback and an explicit in-process cache."""
from difflib import unified_diff
from typing import Any, Callable

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage

from .masking import mask_text
from .capabilities import sdk_call


class PromptHub:
    """Optional remote prompt source; local prompts remain usable during outages."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.cache: dict[str, Any] = {}

    def push(self, name: str, body: str, tags: list[str] | None = None) -> Any:
        """Push a private sanitized template and return the SDK's commit URL."""
        return sdk_call(self.client, "push_prompt", name,
                        object=ChatPromptTemplate.from_messages([SystemMessage(content=mask_text(body))]),
                        tags=tags or [], is_public=False)

    def pull(self, name: str, fallback: Any) -> Any:
        """Pull once and cache successes; unsupported SDKs/outages use the local fallback."""
        if name not in self.cache:
            remote = sdk_call(self.client, "pull_prompt", name)
            if remote is not None and not isinstance(remote, dict):
                self.cache[name] = remote
        return self.cache.get(name, fallback)

    def resolve(self, declaration: str, fallback: Any) -> Any:
        """Resolve a hub: declaration without changing normal local prompt strings."""
        return self.pull(declaration[4:], fallback) if declaration.startswith("hub:") else declaration

    def resolver(self, fallbacks: dict[str, str]) -> Callable[[str], str]:
        """Return a builder-injectable resolver with an explicit local fallback per hub ID."""
        def resolve_text(declaration: str) -> str:
            name = declaration.removeprefix("hub:")
            if name not in fallbacks:
                raise ValueError("hub prompt requires a local fallback")
            prompt = self.pull(name, fallbacks[name])
            if isinstance(prompt, str):
                return prompt
            return prompt.format_prompt().to_string()

        return resolve_text

    def push_agents(self, agents: dict) -> dict[str, Any]:
        """Push all supplied AgentSpec or CatalogPrompt declarations with stable namespaces."""
        from ..org import read_prompt

        results = {}
        for identifier, agent in agents.items():
            body = agent.prompt.body if hasattr(agent.prompt, "body") else read_prompt(agent.prompt)
            results[identifier] = self.push("orchestra-" + identifier.replace("/", "--"), body, ["orchestra"])
        return results


def prompt_diff(before: str, after: str) -> str:
    """Render a masked unified diff for review before any push."""
    return "\n".join(unified_diff(mask_text(before).splitlines(), mask_text(after).splitlines(),
                                 fromfile="local", tofile="hub", lineterm=""))
