"""Vendoring must retain bytes and reject drift without network access."""

import hashlib
import json

import pytest

from scripts.sync_third_party import check, select_files


def test_plugin_selection_is_data_allowlist():
    blobs = {
        "plugins/p/agents/a.md": b"prompt\r\n",
        "plugins/p/skills/s/agents/grader.md": b"grader",
        "plugins/p/skills/s/references/ref.md": b"reference",
        "plugins/p/hooks/hooks.json": b"{}",
        "plugins/p/hooks/execute.sh": b"must not execute",
        "plugins/p/tests/test_bad.py": b"not selected",
        "plugins/security-guidance/hooks/patterns.py": b"PATTERNS=[]",
        "plugins/hookify/core/rule_engine.py": b"rules",
        "plugins/p/LICENSE": b"license",
        "plugins/p/.claude-plugin/plugin.json": b"{}",
    }
    selected = select_files(blobs, "claude_plugins")
    assert selected["plugins/p/agents/a.md"] == b"prompt\r\n"
    assert len(selected) == 8
    assert "plugins/p/hooks/execute.sh" not in selected


def test_module_selection_flattens_examples_and_preserves_original_tests():
    blobs = {"examples/guardrail.py": b"code", "examples/tests/test_examples.py": b"tests"}
    assert select_files(blobs, "multi_agent_module_lib") == {
        "guardrail.py": b"code", "tests/test_examples.py": b"tests",
    }


def test_check_detects_changed_missing_and_extra_files(tmp_path):
    vendor = tmp_path / "third_party"
    target = vendor / "claude_plugins/plugins/p/a.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    lock = {"schema_version": 1, "sources": {"claude_plugins": {
        "repo": "https://github.com/ai-agent-orchestrator/claude-10-plugin-for-multi-agent.git",
        "commit": "a" * 40, "files": {"plugins/p/a.md": hashlib.sha256(b"original").hexdigest()},
        "generated_files": {},
    }}, "metadata": {}}
    (vendor / "LOCK.json").write_text(json.dumps(lock))
    assert check(tmp_path) == []
    target.write_bytes(b"changed")
    assert any("changed" in error for error in check(tmp_path))
    target.unlink()
    assert any("missing" in error for error in check(tmp_path))
    target.with_name("extra.md").write_bytes(b"extra")
    assert any("unexpected" in error for error in check(tmp_path))


def test_selection_rejects_path_escape():
    with pytest.raises(ValueError, match="path"):
        select_files({"../outside": b"x"}, "claude_plugins")
