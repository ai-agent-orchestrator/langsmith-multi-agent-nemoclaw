"""Read-only workspace tools: confinement and size limits (DESIGN §7.3)."""
from __future__ import annotations

import os

import pytest

from orchestra.catalog import tool_mapping
from orchestra.catalog.tool_mapping import map_claude_tools, workspace_tools
from orchestra.specs import SideEffect

BOX = "orchestra-code"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\nvalue = 1\nprint(value)\n", encoding="utf-8")
    (root / "src" / "deep" / "note.md").write_text("value of the note\n", encoding="utf-8")
    (root / "README.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside value\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRA_WORKSPACE", str(root))
    return root


def call(name: str, **args):
    return workspace_tools(BOX)[name][0].invoke(args)


def test_every_tool_is_read_only_and_runs_in_the_named_sandbox():
    tools = workspace_tools(BOX)
    assert set(tools) == {"fs_read", "fs_list", "fs_glob", "fs_grep"}
    for name, (tool, spec) in tools.items():
        assert tool.name == spec.name == name
        assert (spec.side_effect, spec.sandbox, spec.sandbox_name, spec.egress) == (SideEffect.READ, "required", BOX, [])


def test_claude_tool_names_map_to_local_tools_and_report_the_rest():
    mapped, dropped = map_claude_tools(["Glob", "Grep", "LS", "Read", "NotebookRead", "KillShell", "mcp__x__y"])
    assert mapped == ["fs_glob", "fs_grep", "fs_list", "fs_read"]
    assert dropped == ["NotebookRead", "KillShell", "mcp__x__y"]


def test_read_returns_the_requested_lines(workspace):
    result = call("fs_read", path="README.md", start_line=2, max_lines=1)
    assert result == {"path": "README.md", "start_line": 2, "content": "second\n", "total_lines": 3, "truncated": True}
    assert call("fs_read", path="./README.md")["truncated"] is False


def test_list_glob_and_grep(workspace):
    listing = call("fs_list")
    assert [(entry["name"], entry["type"]) for entry in listing["entries"]] == [("README.md", "file"), ("src", "dir")]
    assert call("fs_glob", pattern="**/*.md")["matches"] == ["README.md", "src/deep/note.md"]
    assert call("fs_glob", pattern="*.py", path="src")["matches"] == ["src/app.py"]
    found = call("fs_grep", query="value", path="src")
    assert [(match["path"], match["line"]) for match in found["matches"]] == [
        ("src/app.py", 2), ("src/app.py", 3), ("src/deep/note.md", 1)]
    assert call("fs_grep", query="value", glob="**/*.md")["matches"][0]["path"] == "src/deep/note.md"
    assert call("fs_grep", query="second", path="README.md")["matches"] == [
        {"path": "README.md", "line": 2, "text": "second"}]


def test_grep_treats_the_query_as_text_not_as_a_pattern(workspace):
    (workspace / "a.txt").write_text("(a+)+$ literal\naaaa\n", encoding="utf-8")
    assert [match["line"] for match in call("fs_grep", query="(a+)+$", path="a.txt")["matches"]] == [1]


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "a\x00b", "x" * 600,
                                  "src\\app.py"])
def test_paths_cannot_leave_the_workspace(workspace, path):
    assert call("fs_read", path=path) == {"error": "path_not_allowed"}
    assert call("fs_list", path=path) == {"error": "path_not_allowed"}
    assert call("fs_grep", query="value", path=path) == {"error": "path_not_allowed"}
    assert call("fs_glob", pattern="*", path=path) == {"error": "path_not_allowed"}


@pytest.mark.parametrize("pattern", ["../*", "/etc/*", "a/../b", "", "x" * 300])
def test_glob_patterns_are_confined(workspace, pattern):
    assert call("fs_glob", pattern=pattern) == {"error": "pattern_not_allowed"}


def test_symlinks_are_never_followed(workspace):
    (workspace / "link.txt").symlink_to(workspace.parent / "outside.txt")
    (workspace / "linkdir").symlink_to(workspace.parent, target_is_directory=True)
    (workspace / "inner.txt").symlink_to(workspace / "README.md")
    for path in ("link.txt", "linkdir/outside.txt", "inner.txt"):
        assert call("fs_read", path=path) == {"error": "path_not_allowed"}
    assert call("fs_list", path="linkdir") == {"error": "path_not_allowed"}
    assert "linkdir/outside.txt" not in call("fs_glob", pattern="**")["matches"]
    assert not call("fs_grep", query="outside value")["matches"]
    kinds = {entry["name"]: entry["type"] for entry in call("fs_list")["entries"]}
    assert kinds["link.txt"] == kinds["linkdir"] == "symlink"


def test_a_hard_link_to_an_outside_file_is_refused(workspace):
    os.link(workspace.parent / "outside.txt", workspace / "hard.txt")
    assert call("fs_read", path="hard.txt") == {"error": "path_not_allowed"}
    assert not call("fs_grep", query="outside value")["matches"]


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "capability.key", "ID_RSA", ".git/config",
                                  ".ssh/known_hosts"])
def test_credential_files_are_hidden(workspace, name):
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hidden value\n", encoding="utf-8")
    assert call("fs_read", path=name) == {"error": "path_not_allowed"}
    assert name not in call("fs_glob", pattern="**")["matches"]
    assert not call("fs_grep", query="hidden value")["matches"]
    assert name.split("/")[0] not in [entry["name"] for entry in call("fs_list")["entries"]]


def test_the_example_environment_file_stays_readable(workspace):
    (workspace / ".env.example").write_text("NAME=\n", encoding="utf-8")
    assert call("fs_read", path=".env.example")["content"] == "NAME=\n"


@pytest.mark.parametrize("root", [None, "", "relative/dir", "/", "/definitely/not/here"])
def test_an_unusable_workspace_setting_fails_closed(workspace, monkeypatch, root):
    if root is None:
        monkeypatch.delenv("ORCHESTRA_WORKSPACE")
    else:
        monkeypatch.setenv("ORCHESTRA_WORKSPACE", root)
    for name, args in (("fs_read", {"path": "README.md"}), ("fs_list", {}), ("fs_glob", {"pattern": "*"}),
                       ("fs_grep", {"query": "value"})):
        assert call(name, **args) == {"error": "workspace_not_configured"}


def test_special_binary_missing_and_large_files(workspace, monkeypatch):
    os.mkfifo(workspace / "pipe")
    (workspace / "blob.bin").write_bytes(b"\x00\x01value")
    (workspace / "latin.txt").write_bytes(b"\xff\xfe value")
    assert call("fs_read", path="pipe") == {"error": "not_a_file"}
    assert call("fs_read", path="src") == {"error": "not_a_file"}
    assert call("fs_read", path="blob.bin") == {"error": "not_text"}
    assert call("fs_read", path="latin.txt") == {"error": "not_text"}
    assert call("fs_read", path="nope.txt") == {"error": "not_found"}
    assert call("fs_list", path="README.md") == {"error": "not_a_directory"}
    assert {match["path"] for match in call("fs_grep", query="value")["matches"]} == {
        "src/app.py", "src/deep/note.md"}
    monkeypatch.setattr(tool_mapping, "MAX_FILE_BYTES", 4)
    assert call("fs_read", path="README.md") == {"error": "file_too_large"}


def test_results_are_bounded_and_say_so(workspace, monkeypatch):
    for index in range(8):
        (workspace / f"f{index}.txt").write_text("value\n" * 3, encoding="utf-8")
    monkeypatch.setattr(tool_mapping, "MAX_ENTRIES", 3)
    monkeypatch.setattr(tool_mapping, "MAX_MATCHES", 4)
    assert len(call("fs_list")["entries"]) == 3 and call("fs_list")["truncated"]
    globbed = call("fs_glob", pattern="**/*.txt")
    assert len(globbed["matches"]) == 3 and globbed["truncated"]
    found = call("fs_grep", query="value")
    assert len(found["matches"]) == 4 and found["truncated"]
    monkeypatch.setattr(tool_mapping, "MAX_SCANNED", 2)
    assert call("fs_glob", pattern="**")["truncated"]
    monkeypatch.setattr(tool_mapping, "MAX_OUTPUT_CHARS", 5)
    clipped = call("fs_read", path="README.md")
    assert clipped["content"] == "first" and clipped["truncated"]


def test_search_stops_at_its_time_and_byte_budgets(workspace, monkeypatch):
    monkeypatch.setattr(tool_mapping, "MAX_SEARCH_BYTES", 1)
    assert call("fs_grep", query="value") == {"matches": [], "truncated": True}
    monkeypatch.setattr(tool_mapping, "MAX_SEARCH_BYTES", 10**9)
    monkeypatch.setattr(tool_mapping, "SEARCH_BUDGET_S", 0.0)
    assert call("fs_grep", query="value") == {"matches": [], "truncated": True}


@pytest.mark.parametrize("args", [{"start_line": 0}, {"max_lines": 0}, {"max_lines": 10**6}])
def test_invalid_ranges_are_refused(workspace, args):
    assert call("fs_read", path="README.md", **args) == {"error": "invalid_range"}


@pytest.mark.parametrize("query", ["", "q" * 300])
def test_invalid_queries_are_refused(workspace, query):
    assert call("fs_grep", query=query) == {"error": "invalid_query"}
