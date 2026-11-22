import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from orchestra.models.factory import ScriptedModelFactory, resolve_alias, route_for_sensitivity
from orchestra.models.scripted import EXHAUSTED
from orchestra.runtime import EventLog


@tool
def ping(x: int) -> int:
    """ping"""
    return x


def test_queue_shared_across_bind_tools_and_roles_cached():
    ev = EventLog()
    f = ScriptedModelFactory({"a": [{"tool_calls": [{"name": "ping", "args": {"x": 1}}]}, {"content": "done"}]}, ev)
    m = f(role="a"); bound = m.bind_tools([ping])
    r1 = bound.invoke([HumanMessage("hi")])
    assert r1.tool_calls[0]["name"] == "ping" and m.remaining() == 1       # 복사본이 큐를 공유
    assert f(role="a") is m
    assert bound.invoke([HumanMessage("hi")]).content == "done"
    assert bound.invoke([HumanMessage("hi")]).content == EXHAUSTED         # 소진 시 무한루프 대신 마커
    assert [e.data["role"] for e in ev.of("llm")] == ["a", "a", "a"] and ev.of("llm")[-1].data["exhausted"]


def test_unbound_tool_call_raises():
    m = ScriptedModelFactory({"a": [{"tool_calls": [{"name": "nope"}]}]})(role="a").bind_tools([ping])
    with pytest.raises(ValueError, match="unbound tool 'nope'"):
        m.invoke([HumanMessage("x")])


def test_usage_metadata_deterministic():
    m = ScriptedModelFactory({"a": [{"content": "abcdefgh"}]})(role="a")
    u = m.invoke([HumanMessage("12345678")]).usage_metadata
    assert u["input_tokens"] == 2 and u["output_tokens"] == 2 and u["total_tokens"] == 4


def test_alias_and_privacy_route():
    env = {"ORCHESTRA_MODEL_SONNET": "m-s", "ORCHESTRA_MODEL_LOCAL": "m-l"}
    assert resolve_alias("inherit", env=env) == "m-s" and resolve_alias("local", env=env) == "m-l"
    assert resolve_alias("some-raw-id", env=env) == "some-raw-id"
    with pytest.raises(RuntimeError):
        resolve_alias("opus", env=env)
    assert route_for_sensitivity("pci", policy="local_first") == "local"
    assert route_for_sensitivity("pci", policy="frontier_only") == "frontier"
    assert route_for_sensitivity("public", policy="local_first") == "frontier"
