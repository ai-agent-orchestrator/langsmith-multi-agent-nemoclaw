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


def test_catalog_loads_all_original_plugins_and_namespaced_agents():
    from orchestra.catalog import PluginCatalog

    catalog = PluginCatalog.load(PLUGINS)
    assert not catalog.errors
    assert len(catalog.plugins) == 14
    assert len(catalog.agents) == 16
    assert len(catalog.skills) == 12 and len(catalog.commands) == 12
    assert "feature-dev/code-reviewer" in catalog.agents
    assert "pr-review-toolkit/code-reviewer" in catalog.agents
    assert {"skill-creator/grader", "skill-creator/comparator", "skill-creator/analyzer"} <= catalog.agents.keys()
    assert catalog.agents["plugin-dev/agent-creator"].prompt.description != "|"
    assert catalog.plugins["security-guidance"].hooks["hooks"]["Stop"]


def test_catalog_keeps_directory_namespace_when_manifest_has_alias():
    from orchestra.catalog import PluginCatalog

    plugin = PluginCatalog.load(PLUGINS).plugins["claude-md-management"]
    assert plugin.manifest["name"] == "project-md-management"
    assert "claude-md-management/claude-md-improver" in plugin.skills


def test_skill_index_discloses_metadata_and_load_is_explicit():
    from orchestra.catalog import PluginCatalog
    from orchestra.catalog.skills import SkillLoader, make_skill_tools
    from orchestra.specs import SideEffect

    catalog = PluginCatalog.load(PLUGINS)
    loader = SkillLoader(catalog.skills)
    index = loader.index()
    assert set(index[0]) == {"id", "name", "description"}
    body = loader.load_skill("plugin-dev/agent-development")
    assert "# Agent Development" in body and len(body) <= 20000
    tools = make_skill_tools(catalog.skills)
    tool, spec = tools["load_skill"]
    assert spec.side_effect == SideEffect.READ and spec.sandbox == "never"
    assert tool.invoke({"name": "plugin-dev/agent-development"}) == body


def test_skill_reference_is_registered_read_only_data():
    from orchestra.catalog import PluginCatalog
    from orchestra.catalog.skills import SkillLoader

    loader = SkillLoader(PluginCatalog.load(PLUGINS).skills)
    text = loader.load_skill_reference("claude-md-management/claude-md-improver", "quality-criteria.md")
    assert text and len(text) <= 20000
    for filename in ("../../LICENSE", "/etc/passwd", "C:\\private.txt", "missing.md"):
        with pytest.raises(ValueError, match="reference"):
            loader.load_skill_reference("claude-md-management/claude-md-improver", filename)


def test_skill_truncation_includes_toc_and_enforces_hard_cap(monkeypatch):
    from orchestra.catalog import PluginCatalog
    from orchestra.catalog.skills import SkillLoader

    skills = PluginCatalog.load(PLUGINS).skills
    monkeypatch.setenv("ORCHESTRA_SKILL_MAX_CHARS", "300")
    output = SkillLoader(skills).load_skill("plugin-dev/agent-development")
    assert len(output) <= 300 and "Contents" in output and "truncated" in output
    monkeypatch.setenv("ORCHESTRA_SKILL_MAX_CHARS", "999999")
    assert len(SkillLoader(skills).load_skill("plugin-dev/agent-development")) <= 20000


def test_catalog_hooks_are_never_executed(monkeypatch):
    import subprocess

    from orchestra.catalog import PluginCatalog

    def forbidden(*args, **kwargs):
        raise AssertionError("catalog must not execute subprocesses")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert not PluginCatalog.load(PLUGINS).errors


def test_catalog_cli_list_show_validate(capsys):
    from orchestra.catalog.__main__ import main

    assert main(["--root", str(PLUGINS), "list"]) == 0
    output = capsys.readouterr().out
    assert "PLUGIN" in output and "14 plugins" in output and "16 agents" in output
    assert main(["--root", str(PLUGINS), "show", "pr-review-toolkit/code-reviewer"]) == 0
    assert '"model": "opus"' in capsys.readouterr().out
    assert main(["--root", str(PLUGINS), "validate"]) == 0
    assert "VALID" in capsys.readouterr().out
    assert main(["--root", str(PLUGINS), "show", "missing"]) == 1


def test_invalid_catalog_json_reports_error_without_content(tmp_path, capsys):
    from orchestra.catalog import PluginCatalog
    from orchestra.catalog.__main__ import main

    manifest = tmp_path / "broken/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("secret-invalid-content")
    catalog = PluginCatalog.load(tmp_path)
    assert catalog.errors and "secret-invalid-content" not in str(catalog.errors)
    assert main(["--root", str(tmp_path), "validate"]) == 1
    assert "secret-invalid-content" not in capsys.readouterr().out


def test_catalog_root_can_be_selected_with_env(monkeypatch):
    from orchestra.catalog import PluginCatalog

    monkeypatch.setenv("CLAUDE_PLUGINS_DIR", str(PLUGINS))
    assert len(PluginCatalog.load().plugins) == 14


def test_skill_content_is_masked_before_character_limit(tmp_path):
    from orchestra.catalog.skills import Skill, SkillLoader

    path = tmp_path / "SKILL.md"
    path.write_text("# Example\n" + "private@example.com 1234-5678-1234-5678\n" * 50)
    skill = Skill("example/demo", "demo", "summary", path, tmp_path)
    output = SkillLoader({skill.id: skill}, max_chars=500).load_skill(skill.id)
    assert len(output) <= 500
    assert "private@example.com" not in output and "1234-5678-1234-5678" not in output
    assert "[REDACTED_EMAIL]" in output and "[REDACTED_CARD]" in output
