"""Hookify example conversion without enabling any EXEC tools."""
from pathlib import Path

import pytest

from orchestra.filters.base import Action, FilterContext, Stage
from orchestra.filters.builtin.hookify_rules import load_rule, parse_rule
from orchestra.filters.chain import FilterChain

EXAMPLES = Path(__file__).resolve().parents[1] / "third_party/claude_plugins/plugins/hookify/examples"


@pytest.mark.parametrize("file,tool,payload,action", [
    ("dangerous-rm.local.md", "shell_run", {"command": "rm -rf ./data"}, Action.BLOCK),
    ("console-log-warning.local.md", "fs_write", {"content": "console.log(1)"}, Action.MODIFY),
    ("sensitive-files-warning.local.md", "fs_edit", {"path": ".env", "new_string": "x"}, Action.MODIFY),
])
def test_original_tool_examples(file, tool, payload, action):
    spec, implementation = load_rule(EXAMPLES / file)
    result = FilterChain([(spec, implementation)]).run(FilterContext(Stage.PRE_TOOL, payload, tool=tool))
    assert result.action == (Action.BLOCK if action == Action.BLOCK else Action.ALLOW)
    assert result.trail[0].action == action
    assert result.payload == payload
    assert result.trail[0].reason
    assert FilterChain([(spec, implementation)]).run(
        FilterContext(Stage.PRE_TOOL, payload, tool="unrelated")
    ).trail == []


def test_original_stop_example_stays_disabled_until_enabled():
    text = (EXAMPLES / "require-tests-stop.local.md").read_text(encoding="utf-8")
    spec, implementation = parse_rule(text)
    assert not spec.enabled
    assert FilterChain([(spec, implementation)]).run(FilterContext(Stage.PRE_RESPONSE, "done")).allowed
    spec, implementation = parse_rule(text.replace("enabled: false", "enabled: true"))
    chain = FilterChain([(spec, implementation)])
    assert chain.run(FilterContext(Stage.PRE_RESPONSE, "done", state={"transcript": "none"})).action == Action.BLOCK
    assert chain.run(FilterContext(Stage.PRE_RESPONSE, "done", state={"transcript": "pytest -q"})).allowed


@pytest.mark.parametrize("event,stage", [("prompt", Stage.PRE_REQUEST), ("stop", Stage.PRE_RESPONSE),
                                        ("all", Stage.POST_TOOL)])
def test_event_mapping(event, stage):
    spec, implementation = parse_rule(f"---\nname: example\nevent: {event}\naction: block\npattern: forbidden\n---\nWarning")
    assert FilterChain([(spec, implementation)]).run(FilterContext(stage, "forbidden")).action == Action.BLOCK


@pytest.mark.parametrize("entry", ["event: unknown", "action: unknown", "pattern: '['"])
def test_invalid_rules_fail_closed_at_load(entry):
    with pytest.raises(ValueError):
        parse_rule(f"---\nname: invalid\nevent: prompt\npattern: x\n{entry}\n---\nbody")


def test_warnings_mask_secrets_and_do_not_modify_tool_arguments():
    spec, implementation = parse_rule("---\nname: warning\nevent: file\npattern: x\naction: warn\n---\nprivate@example.com")
    payload = {"new_string": "x"}
    result = FilterChain([(spec, implementation)]).run(FilterContext(Stage.PRE_TOOL, payload, tool="fs_edit"))
    assert result.payload == payload
    assert "private@example.com" not in result.trail[0].reason
