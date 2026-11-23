import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import guardrail
import loop_control
import orchestrator
import prompt_loader
import review_aggregator as ra


def test_guardrail_blocks_and_allows():
    assert not guardrail.check("x = eval(user_input)").allowed
    assert not guardrail.check('api_key = "abcdef123456"').allowed
    assert guardrail.check("print('hello')").allowed


def test_guardrail_verifier_drops_false_positive():
    v = guardrail.check("eval(x)", verifier=lambda t, f: [])
    assert v.allowed


def test_guard_wrapper():
    safe = guardrail.guard(lambda s: s.upper())
    assert safe("ok") == "OK"
    with pytest.raises(PermissionError):
        safe("eval(x)")
    with pytest.raises(PermissionError):
        guardrail.guard(lambda s: "eval(x)")("ok")


def test_loop_stops_on_done_and_caps():
    r = loop_control.run_loop(lambda o, i: o + "x", lambda o: len(o) >= 3, "", max_iterations=5)
    assert r.passed and r.iterations == 3 and r.stop_reason == "done"
    r = loop_control.run_loop(lambda o, i: o, lambda o: False, "", max_iterations=4)
    assert not r.passed and r.iterations == 4 and r.stop_reason == "max_iterations"
    r = loop_control.run_loop(lambda o, i: o, lambda o: False, "", max_iterations=10, max_cost=1.0, cost_per_step=0.4)
    assert r.stop_reason == "max_cost" and r.iterations == 2


def test_review_aggregation():
    issues = [
        ra.Issue("a.py", 1, "bug", 90, "r1"),
        ra.Issue("a.py", 2, "nit", 50, "r1"),
        ra.Issue("a.py", 3, "maybe", 78, "r1"),
        ra.Issue("a.py", 3, "maybe", 78, "r2"),  # agreement boosts 78 -> 83
    ]
    out = ra.aggregate(issues)
    assert [(i.line, i.confidence) for i in out] == [(1, 90), (3, 83)]


def test_run_reviewers_parallel():
    rs = {"a": lambda d: [ra.Issue("f", 1, "x", 85)], "b": lambda d: []}
    out = ra.run_reviewers("diff", rs)
    assert len(out) == 1 and out[0].reviewer == "a"


def test_prompt_loader(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "arch.md").write_text("---\nname: architect\ndescription: d\n---\nDesign things.\n")
    (tmp_path / "skills" / "s1").mkdir(parents=True)
    (tmp_path / "skills" / "s1" / "SKILL.md").write_text("No frontmatter body\n")
    p = prompt_loader.load_prompts(tmp_path)
    assert p["architect"].body == "Design things." and p["architect"].meta["description"] == "d"
    assert p["s1"].body == "No frontmatter body"


def fake_llm_factory(approve_on_round):
    calls = {"n": 0}

    def llm(system, user):
        if "review" in system:
            calls["n"] += 1
            return "APPROVE" if calls["n"] >= approve_on_round else "fix: add tests"
        return f"out({system[:8]})"

    return llm


def test_pipeline_approves_after_retry():
    s = orchestrator.run_pipeline("t", fake_llm_factory(2), max_rounds=3)
    assert s["approved"] and s["rounds"] == 2


def test_pipeline_round_cap():
    s = orchestrator.run_pipeline("t", fake_llm_factory(99), max_rounds=2)
    assert not s["approved"] and s["rounds"] == 2


def test_langgraph_matches_plain():
    pytest.importorskip("langgraph")
    g = orchestrator.build_langgraph(fake_llm_factory(2), max_rounds=3)
    s = g.invoke({"task": "t", "rounds": 0})
    assert s["approved"] and s["rounds"] == 2
