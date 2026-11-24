"""Translate inert hookify rules to deterministic filters without running hooks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...catalog.frontmatter import parse_markdown
from ...specs import STAGES, FilterSpec
from ..base import FilterContext, FilterDecision
from .pii_mask import mask_text

EVENTS = {"bash": (["pre_tool"], ["shell_run"]),
          "file": (["pre_tool"], ["fs_write", "fs_edit"]),
          "prompt": (["pre_request"], ["*"]), "stop": (["pre_response"], ["*"]),
          "all": (list(STAGES), ["*"])}
OPERATORS = {"regex_match", "contains", "not_contains", "equals", "starts_with", "ends_with"}


@dataclass
class HookifyRule:
    """Validated rule conditions; evaluation reads only the supplied context."""

    name: str
    action: str
    conditions: list[dict[str, str]]
    message: str

    def __call__(self, ctx: FilterContext) -> FilterDecision:
        """Block or emit a masked warning when every condition matches."""
        values: dict[str, Any] = dict(ctx.payload) if isinstance(ctx.payload, dict) else {"content": ctx.payload}
        values.setdefault("content", values.get("new_string", ""))
        values.setdefault("new_text", values.get("new_string", values.get("content", "")))
        values.setdefault("old_text", values.get("old_string", ""))
        values.setdefault("file_path", values.get("path", ""))
        values.setdefault("user_prompt", values.get("content", ""))
        # Transcript is supplied by the host, never loaded from a path in untrusted payloads.
        values["transcript"] = ctx.state.get("transcript", "")
        for condition in self.conditions:
            field, operator, pattern = (condition[k] for k in ("field", "operator", "pattern"))
            if field not in values:
                return FilterDecision.allow()
            value = str(values[field])
            if operator == "regex_match":
                matches = bool(re.search(pattern, value))
            elif operator in ("contains", "not_contains"):
                matches = any(part in value for part in pattern.split("|"))
                if operator == "not_contains":
                    matches = not matches
            elif operator == "equals":
                matches = value == pattern
            elif operator == "starts_with":
                matches = value.startswith(pattern)
            else:
                matches = value.endswith(pattern)
            if not matches:
                return FilterDecision.allow()
        reason = mask_text(self.message or "hookify rule matched")
        if self.action == "block":
            return FilterDecision.block(reason)
        # MODIFY with identical payload records the warning without adding invalid tool kwargs.
        return FilterDecision.modify(ctx.payload, reason)


def parse_rule(text: str) -> tuple[FilterSpec, HookifyRule]:
    """Validate a local Markdown rule and return a spec/implementation pair."""
    prompt = parse_markdown(text, "unnamed")
    data = prompt.meta
    if prompt.warnings or data.get("event") not in EVENTS:
        raise ValueError("invalid hookify frontmatter or event")
    action = data.get("action", "warn")
    if action not in ("block", "warn") or not isinstance(data.get("enabled", True), bool):
        raise ValueError("invalid hookify action or enabled value")
    event = data["event"]
    conditions = data.get("conditions", [])
    if not conditions and isinstance(data.get("pattern"), str):
        field = "command" if event == "bash" else "new_text" if event == "file" else "content"
        conditions = [{"field": field, "operator": "regex_match", "pattern": data["pattern"]}]
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("hookify rule requires conditions")
    normalized = []
    for condition in conditions:
        if not isinstance(condition, dict) or not all(isinstance(condition.get(k), str) for k in ("field", "pattern")):
            raise ValueError("invalid hookify condition")
        operator = condition.get("operator", "regex_match")
        if operator not in OPERATORS:
            raise ValueError("unsupported hookify operator")
        if operator == "regex_match":
            try:
                re.compile(condition["pattern"])
            except re.error:
                raise ValueError("invalid hookify regex") from None
        normalized.append({"field": condition["field"], "operator": operator, "pattern": condition["pattern"]})
    stages, targets = EVENTS[event]
    matcher = data.get("tool_matcher")
    if matcher and matcher != "*":
        aliases = {"Bash": "shell_run", "Write": "fs_write", "Edit": "fs_edit", "MultiEdit": "fs_edit"}
        if not isinstance(matcher, str):
            raise ValueError("invalid hookify tool matcher")
        requested = [aliases.get(part, part) for part in matcher.split("|")]
        if not all(part in {"shell_run", "fs_write", "fs_edit"} for part in requested):
            raise ValueError("unsupported hookify tool matcher")
        targets = [part for part in requested if "*" in targets or part in targets]
        if not targets:
            raise ValueError("hookify tool matcher contradicts event")
    spec = FilterSpec(name=f"hookify:{prompt.name}", stages=stages, applies_to=targets, enabled=data.get("enabled", True))
    return spec, HookifyRule(spec.name, action, normalized, prompt.body)


def load_rule(path: str | Path) -> tuple[FilterSpec, HookifyRule]:
    """Read an explicitly selected local rule; no directory scanning or hook execution."""
    return parse_rule(Path(path).read_text(encoding="utf-8"))
