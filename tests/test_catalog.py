"""Catalog frontmatter contracts from DESIGN §7.2, using original snapshots."""

from pathlib import Path

import pytest

from orchestra.catalog.frontmatter import parse_markdown

PLUGINS = Path(__file__).resolve().parents[1] / "third_party/claude_plugins/plugins"


def test_multiline_agent_description_from_original():
    path = PLUGINS / "plugin-dev/agents/agent-creator.md"
    prompt = parse_markdown(path.read_text(encoding="utf-8"), path.stem)
    assert prompt.name == "agent-creator"
    assert "<example>" in prompt.description and "\n" in prompt.description
    assert prompt.description != "|"
    assert prompt.body and "Read" in prompt.tools


def test_frontmatterless_original_grader():
    path = PLUGINS / "skill-creator/skills/skill-creator/agents/grader.md"
    text = path.read_text(encoding="utf-8")
    prompt = parse_markdown(text, "grader")
    assert prompt.name == "grader" and prompt.body == text.strip()
    assert prompt.tools == [] and prompt.model == "inherit"


def test_original_reviewer_opus_and_string_tools():
    path = PLUGINS / "pr-review-toolkit/agents/code-reviewer.md"
    prompt = parse_markdown(path.read_text(encoding="utf-8"), path.stem)
    assert prompt.name == "code-reviewer" and prompt.model == "opus"


def test_frontmatter_examples_inside_skill_body_are_ignored():
    prompt = parse_markdown("# Skill\n\n```yaml\n---\nmodel: gpt4\nname: injected\n---\n```", "skill")
    assert prompt.name == "skill" and prompt.model == "inherit"
    assert "model: gpt4" in prompt.body and not prompt.warnings
    path = PLUGINS / "plugin-dev/skills/agent-development/SKILL.md"
    original = parse_markdown(path.read_text(encoding="utf-8"), "agent-development")
    assert original.name == "agent-development" and "---" in original.body
    assert original.model == "inherit" and not original.warnings


@pytest.mark.parametrize("tools", [
    '"Read, Grep # note, Bash"',
    '[Read, "Grep # note", Read]',
    '\n  - Read\n  - Grep # note',
])
def test_tools_and_model_comments_are_normalized(tools):
    prompt = parse_markdown(f'---\ntools: {tools}\nmodel: "Sonnet # balanced"\n---\nBody', "agent")
    assert prompt.tools == ["Read", "Grep"]
    assert prompt.model == "sonnet" and prompt.meta["tools"] == prompt.tools


def test_unsupported_model_warns_without_echoing_untrusted_value():
    with pytest.warns(UserWarning, match="unsupported model") as caught:
        prompt = parse_markdown("---\nmodel: private-token-value\n---\nBody", "agent")
    assert prompt.model == "inherit" and prompt.warnings
    assert "private-token-value" not in str(caught[0].message)


def test_invalid_yaml_falls_back_to_original_line_parser():
    with pytest.warns(UserWarning, match="legacy parser"):
        prompt = parse_markdown("---\nname: fallback\ndescription: [broken\ntools: Read, Grep\n---\nBody", "agent")
    assert prompt.name == "fallback" and prompt.body == "Body"
    assert prompt.tools == ["Read", "Grep"]


def test_only_complete_delimiter_lines_are_frontmatter():
    text = "---\nname: secret\n---not-a-delimiter\nBody"
    prompt = parse_markdown(text, "default")
    assert prompt.name == "default" and prompt.body == text
    assert parse_markdown("---\r\nname: windows\r\n---\r\nBody", "default").name == "windows"


def test_yaml_object_tags_are_never_constructed():
    with pytest.warns(UserWarning, match="legacy parser"):
        prompt = parse_markdown("---\nvalue: !!python/object/apply:os.system ['false']\n---\nBody", "safe")
    assert prompt.body == "Body"
