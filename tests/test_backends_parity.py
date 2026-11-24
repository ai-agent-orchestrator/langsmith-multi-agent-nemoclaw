"""Run the same actual graphs through both installed agent implementations."""
import warnings

import pytest

from orchestra.simulation import check_expect, list_scenarios, run_scenario


@pytest.mark.parametrize("scenario", list_scenarios())
def test_all_scenarios_have_identical_tool_order_and_safety(scenario):
    prebuilt = run_scenario(scenario, backend="prebuilt")
    with warnings.catch_warnings(record=True) as caught:
        v1 = run_scenario(scenario, backend="v1")
    assert not any("create_react_agent" in str(w.message) for w in caught)
    assert prebuilt.app.backend == "prebuilt" and v1.app.backend == "v1"
    assert v1.app.config()["metadata"]["backend"] == "v1"
    assert check_expect(prebuilt) == check_expect(v1) == []
    assert [e.data["tool"] for e in prebuilt.events.of("tool")] == [e.data["tool"] for e in v1.events.of("tool")]
    assert [e.data for e in prebuilt.events.of("handoff")] == [e.data for e in v1.events.of("handoff")]
    assert prebuilt.decisions == v1.decisions
    assert prebuilt.final["answer"] == v1.final["answer"]
    assert prebuilt.events.feedback() == v1.events.feedback()
    def calls(result):
        return [(event.data["role"], call["name"], call["args"])
                for event in result.events.of("llm") for call in event.data["tool_calls"]]

    assert calls(prebuilt) == calls(v1)
    assert [call.tool for call in prebuilt.sandbox.calls] == [call.tool for call in v1.sandbox.calls]
    if scenario == "jeju_trip_payment":
        assert [call.tool for call in v1.sandbox.calls].count("capture_payment") == 1


def test_env_selects_v1_and_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_AGENT_BACKEND", "v1")
    assert check_expect(run_scenario("jeju_trip_payment")) == []
    monkeypatch.setenv("ORCHESTRA_AGENT_BACKEND", "invalid")
    with pytest.raises(ValueError, match="backend"):
        run_scenario("jeju_trip_payment")


def test_pre_model_block_never_calls_model_on_either_backend():
    from orchestra.backends import get_backend
    from orchestra.backends.base import ModelBlocked
    from orchestra.filters.base import FilterDecision
    from orchestra.filters.chain import FilterChain
    from orchestra.models.scripted import ScriptedChatModel
    from orchestra.specs import AgentSpec, FilterSpec

    class Block:
        name = "deny"

        def __call__(self, context):
            return FilterDecision.block("denied")

    chain = FilterChain([(FilterSpec(name="deny", stages=["pre_model"]), Block())])
    for backend in ("prebuilt", "v1"):
        model = ScriptedChatModel("agent", [{"content": "must not execute"}])
        app = get_backend(backend).agent(AgentSpec(id="example/agent", name="agent"), model, [], chain)
        with pytest.raises(ModelBlocked):
            app.invoke({"messages": [("user", "hello")]})
        assert model.remaining() == 1


@pytest.mark.parametrize("backend", ["prebuilt", "v1"])
@pytest.mark.parametrize("output_mode", ["last_message", "full_history"])
def test_async_handoff_returns_without_waking_supervisor_midway(backend, output_mode):
    import asyncio

    from orchestra.models.factory import ScriptedModelFactory
    from orchestra.patterns.hierarchical_graph import build_hierarchical_app
    from orchestra.runtime import Runtime
    from orchestra.specs import AgentSpec, OrgSpec, TeamSpec

    org = OrgSpec(name="async_example", root="director", domains=[],
                  teams={"director": TeamSpec(name="director", members=["example/a", "example/b"], output_mode=output_mode)},
                  agents={"example/a": AgentSpec(id="example/a", name="worker_a", handoff_to=["example/b"]),
                          "example/b": AgentSpec(id="example/b", name="worker_b")})
    runtime = Runtime(security_mode="closed")
    factory = ScriptedModelFactory({
        "director": [{"tool_calls": [{"name": "transfer_to_worker_a"}]}, {"content": "done"}],
        "worker_a": [{"tool_calls": [{"name": "transfer_to_worker_b"}]}],
        "worker_b": [{"content": "worker done"}],
    }, runtime.events)
    app = build_hierarchical_app(org, factory, runtime=runtime, backend=backend)
    result = asyncio.run(app.graph.ainvoke({"messages": [("user", "hello")]}, app.config("async-thread")))
    assert result["messages"][-1].content == "done"
    assert [e.data["role"] for e in runtime.events.of("llm")] == ["director", "worker_a", "worker_b", "director"]


def test_missing_v1_dependency_is_a_clear_error_without_silent_fallback(monkeypatch):
    import builtins

    from orchestra.backends import get_backend

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "v1":
            raise ImportError("missing dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match="optional.*v1"):
        get_backend("v1")


@pytest.mark.parametrize("backend", ["prebuilt", "v1"])
def test_pre_model_modify_uses_invocation_copy_and_config_thread_id(backend):
    from langchain_core.messages import HumanMessage

    from orchestra.backends import get_backend
    from orchestra.filters.base import FilterDecision
    from orchestra.filters.chain import FilterChain
    from orchestra.models.scripted import ScriptedChatModel
    from orchestra.specs import AgentSpec, FilterSpec

    seen = []

    class Replace:
        name = "replace"

        def __call__(self, context):
            seen.append(context.state["thread_id"])
            return FilterDecision.modify([HumanMessage(content="sanitized")])

    class Probe(ScriptedChatModel):
        def _generate(self, messages, *args, **kwargs):
            assert messages[-1].content == "sanitized"
            return super()._generate(messages, *args, **kwargs)

    chain = FilterChain([(FilterSpec(name="replace", stages=["pre_model"]), Replace())])
    app = get_backend(backend).agent(AgentSpec(id="example/agent", name="agent"),
                                     Probe("agent", [{"content": "done"}]), [], chain)
    result = app.invoke({"messages": [HumanMessage(content="original")]},
                        {"configurable": {"thread_id": "thread-A"}})
    assert result["messages"][0].content == "original"
    assert seen == ["thread-A"]
