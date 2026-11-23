"""Legacy imports, graph shape, tool contracts, and security remain usable."""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from orchestra.models.factory import ScriptedModelFactory
from orchestra.models.scripted import ScriptedChatModel
from orchestra.org import REPO_ROOT, load_org
from orchestra.registry import build_packs, resolve
from orchestra.specs import SideEffect
from travel_planner import TravelService, build_hierarchical_app
from travel_planner import hierarchical_tools, observability


def test_legacy_organization_reproduces_original_members_and_tools():
    resolved = resolve(load_org(REPO_ROOT / "orgs/travel_legacy.yaml"), build_packs(["travel_legacy"]))
    assert resolved.org.teams["travel_director"].members == ["planning_lead", "booking_lead", "support_lead"]
    assert len(resolved.org.teams) == 4
    assert len(resolved.org.agents) == 8
    assert len(resolved.tools) == 17
    assert all(not a.handoff_to and len(a.tools) <= 3 for a in resolved.org.agents.values())
    for name, (tool, spec) in resolved.tools.items():
        assert tool is getattr(hierarchical_tools, name)
        if name.startswith("book_"):
            assert spec.side_effect == SideEffect.COMMIT


def test_build_shim_delegates_to_orchestra_and_keeps_compiled_graph_api(monkeypatch):
    from orchestra.patterns import hierarchical_graph

    original = hierarchical_graph.build_hierarchical_app
    calls = []

    def build(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(hierarchical_graph, "build_hierarchical_app", build)
    app = build_hierarchical_app(ScriptedModelFactory({"travel_director": [{"content": "완료"}]}))
    assert calls and str(calls[0]).endswith("travel_legacy.yaml")
    assert app.invoke({"messages": [("user", "일정") ]})["messages"][-1].content == "완료"
    assert app.get_graph() and callable(app.stream)
    assert app.checkpointer is None


def test_service_without_thread_id_keeps_response_contract():
    model = ScriptedChatModel("travel_director", [{"content": "일정 완료"}])
    result = TravelService("hierarchical", model, mode="closed").ask("일정")
    assert result["answer"] == "일정 완료"
    assert result["blocked"] is False


@pytest.mark.parametrize("approved", [True, False])
def test_legacy_booking_requires_approval_and_resumes_once(monkeypatch, approved):
    calls = []
    original = hierarchical_tools.book_hotel.func

    def book(**args):
        calls.append(args)
        return original(**args)

    monkeypatch.setattr(hierarchical_tools.book_hotel, "func", book)
    scripts = {
        "travel_director": [
            {"tool_calls": [{"name": "transfer_to_booking_lead", "args": {}}]},
            {"content": "완료"},
        ],
        "booking_lead": [
            {"tool_calls": [{"name": "transfer_to_hotel_agent", "args": {}}]},
            {"content": "예약 완료"},
        ],
        "hotel_agent": [
            {"tool_calls": [{"name": "book_hotel", "args": {
                "hotel_id": "h1", "guests": 2, "checkin": "2026-12-01", "nights": 1,
            }}]},
            {"content": "호텔 완료"},
        ],
    }
    saver = MemorySaver()
    app = build_hierarchical_app(ScriptedModelFactory(scripts), checkpointer=saver)
    config = {"configurable": {"thread_id": "legacy-approval"}, "recursion_limit": 50}
    result = app.invoke({"messages": [("user", "호텔 예약")]}, config)
    assert result["__interrupt__"]
    assert calls == []
    result = app.invoke(Command(resume=approved), config)
    assert len(calls) == int(approved)
    assert not result.get("__interrupt__")
    assert app.checkpointer is saver
    assert isinstance(result["messages"][-1], AIMessage)


def test_legacy_rules_share_core_and_preserve_legacy_masking():
    from orchestra.filters.builtin.pii_mask import LEGACY_MASK_RULES
    from orchestra.filters.builtin.request_gate import BLOCK_RULES

    assert observability.MASK_RULES is LEGACY_MASK_RULES
    assert observability.BLOCK_RULES is BLOCK_RULES
    assert observability.mask_text("1234-5678-1234-5678") == "[REDACTED_CARD]"
    assert observability.mask_text("1234 5678 1234 5678 cvc=123") == "1234 5678 1234 5678 cvc=123"
    assert hierarchical_tools.book_car.invoke({"car_id": "c1", "date": "2026-12-01", "days": 1}) == {
        "status": "confirmed", "car_id": "c1",
    }
