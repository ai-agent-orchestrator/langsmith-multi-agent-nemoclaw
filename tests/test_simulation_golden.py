"""실제 컴파일된 계층형 그래프를 ScriptedChatModel 로 끝까지 실행한 결과 검증 (LLM·네트워크 불필요)."""
import re
from pathlib import Path

import pytest
import yaml

from orchestra.simulation import check_expect, list_scenarios, load_scenario, render, run_scenario

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("sid", list_scenarios())
def test_scenario_meets_expectations(sid):
    res = run_scenario(sid)
    assert check_expect(res) == [], render(res)


def test_handoff_does_not_wake_team_supervisor_midway():
    """weather→restaurant handoff 도중 planning_lead 가 끼어들면 안 된다 (고정 엣지 제거 회귀 방지)."""
    res = run_scenario("jeju_trip_payment")
    roles = [e.data["role"] for e in res.events.of("llm")]
    i = roles.index("weather_agent")
    j = max(k for k, r in enumerate(roles) if r == "weather_agent")
    assert roles[j + 1] == "restaurant_agent"
    assert not any(e.data.get("exhausted") for e in res.events.of("llm"))


def test_payment_rejected_never_captures():
    res = run_scenario("jeju_payment_rejected")
    assert res.decisions == [False]
    prov = res.app.runtime.services["payments"]
    assert [i.status for i in prov.intents.values()] == ["created"]
    assert not any(e.data["tool"] == "capture_payment" for e in res.events.of("tool"))


def test_approval_resume_runs_capture_exactly_once():
    """interrupt 재개 시 툴 노드가 재실행돼도 결제는 1번만 일어난다."""
    res = run_scenario("jeju_trip_payment")
    prov = res.app.runtime.services["payments"]
    assert len(prov.intents) == 1 and prov.intents["pi_0001"].status == "confirmed"
    assert sum(e.data["tool"] == "capture_payment" for e in res.events.of("tool")) == 1
    assert [c.tool for c in res.sandbox.calls].count("capture_payment") == 1
    assert [h["decision"] for h in res.app.runtime.approvals.history] == [True]
    assert res.app.runtime.approvals._granted == set()                  # 승인은 1회용: 소진됨


def test_unanswered_interrupt_fails_closed():
    res = run_scenario("jeju_trip_payment", approve=[])
    assert res.decisions == [False]
    assert res.app.runtime.services["payments"].intents["pi_0001"].status == "created"


def test_commit_blocked_even_if_filters_misconfigured():
    """필터 체인에서 승인 필터를 모두 꺼도 COMMIT 툴은 승인 장부 없이는 실행되지 않는다 (마지막 방어선)."""
    sc = load_scenario("jeju_trip_payment")
    org = yaml.safe_load((ROOT / sc["org"]).read_text())
    org["filters"] += [{"name": n, "stages": ["pre_tool"], "enabled": False} for n in ("side_effect_policy", "amount_limit")]
    sc = {**sc, "org": org}
    res = run_scenario(sc)
    prov = res.app.runtime.services["payments"]
    assert prov.intents["pi_0001"].status == "created" and not res.interrupts
    assert any(e.data.get("by") == "tool_wrapper" and e.data["reason"] == "commit_without_approval"
               for e in res.events.of("blocked"))


def test_sandbox_off_fails_closed_for_nemoclaw_mode():
    from orchestra.models.factory import ScriptedModelFactory
    from orchestra.patterns.hierarchical_graph import build_hierarchical_app
    from orchestra.runtime import Runtime
    from orchestra.service import OrchestraService

    sc = load_scenario("jeju_trip_payment")
    svc_services = {"today": "2026-11-22"}
    rt = Runtime(sandbox_mode="nemoclaw", services=svc_services)          # 러너 없음 = 샌드박스 불가
    app = build_hierarchical_app(sc["org"], ScriptedModelFactory(sc["script"], rt.events), services=svc_services, runtime=rt)
    OrchestraService(app).ask(sc["request"], "t1")
    assert any(e.data["error"] == "sandbox_unavailable" for e in rt.events.of("sandbox"))
    assert not svc_services["payments"].intents                            # 결제 의도조차 만들어지지 않음


def test_render_has_expected_markers():
    out = render(run_scenario("jeju_trip_payment"))
    for marker in ("[HITL]", "[SANDBOX orchestra-payments]", "⇢ handoff weather_agent → restaurant_agent", "[REPORT"):
        assert marker in out
    assert re.search(r"슈퍼바이저\(총괄/팀장\) LLM 호출 \d+회", out)


def test_core_never_imports_domains():
    """코어는 도메인을 모른다 (DESIGN §2-1). 동적 import 문자열(registry)만 허용."""
    bad = []
    for f in (ROOT / "orchestra").rglob("*.py"):
        for line in f.read_text().splitlines():
            if re.match(r"\s*(from|import)\s+domains\b", line):
                bad.append(f"{f.name}: {line.strip()}")
    assert bad == []
