import os

import pytest

from orchestra.observability import datasets
from orchestra.observability.evals import main, run_experiment, run_local, target, to_sdk_examples
from orchestra.observability.evaluators import (EVALUATORS, all_invariants_pass, payment_safety, security_block,
                                                summarize, trajectory_match)
from orchestra.observability.safe import safe_call
from orchestra.simulation import list_scenarios, run_scenario


def test_examples_cover_all_scenarios_without_scripts_or_pii():
    ex = datasets.scenario_examples()
    assert {e["metadata"]["scenario_id"] for e in ex} == set(list_scenarios())
    cats = {e["metadata"]["category"] for e in ex}
    assert cats == {"core", "redteam", "security"}
    blob = str(ex)
    assert "script" not in str(ex[0]["inputs"]) and "tool_calls" not in blob      # 스크립트는 저장소가 소유


def test_run_local_passes_on_current_scenarios():
    rep = run_local()
    assert rep.passed and len(rep.rows) == len(list_scenarios()) and rep.failures == []
    assert rep.summary["all_invariants_pass"]["score"] == 1.0


def test_empty_examples_do_not_pass():
    assert run_local([]).passed is False


def test_unknown_scenario_is_a_failure_not_a_crash():
    rep = run_local([{"inputs": {"scenario_id": "nope", "approve": []}, "outputs": {"expect": {}},
                      "metadata": {"scenario_id": "nope"}}])
    assert not rep.passed and any("trajectory_match" in f for f in rep.failures)


def test_cli_exit_code(capsys):
    assert main([]) == 0
    assert "eval gate: PASS" in capsys.readouterr().out


# ---- 평가자가 실제로 위반을 잡는다
def outs(sid="jeju_trip_payment", **patch):
    o = summarize(run_scenario(sid))
    o.update(patch)
    return o


def test_invariant_evaluators_flag_violations_and_missing_results():
    bad = outs(invariants={"no_unapproved_commit": {"score": 0.0, "comment": "x"}})
    got = {ev(  {}, bad, {})["key"]: ev({}, bad, {})["score"] for ev in EVALUATORS}
    assert got["no_unapproved_commit"] == 0.0
    assert got["amount_matches_hold"] == 0.0              # 결과 누락 = 실패 (통과로 보지 않음)


def test_trajectory_match_uses_check_expect():
    assert trajectory_match({}, outs(), {})["score"] == 1.0
    assert trajectory_match({}, outs(expect_errors=["tools_in_order: ..."]), {})["score"] == 0.0
    assert trajectory_match({}, {"scenario_id": "x"}, {})["score"] == 0.0


def test_payment_safety_zero_when_captured_without_interrupt():
    assert payment_safety({}, outs(), {})["score"] == 1.0
    assert payment_safety({}, outs(payment_captured=True, interrupts=0), {})["score"] == 0.0
    assert payment_safety({}, outs(payment_captured=False, interrupts=0), {})["score"] == 1.0


def test_security_block_checks_expected_filter():
    o = summarize(run_scenario("payment_over_limit"))
    ref = {"expect": {"blocked_by": "amount_limit"}}
    assert security_block({}, o, ref)["score"] == 1.0
    assert security_block({}, dict(o, blocked_by=[]), ref)["score"] == 0.0
    assert security_block({}, o, {"expect": {}})["comment"] == "n/a"


def test_summary_evaluator():
    assert all_invariants_pass([], [])["score"] == 0.0
    good, bad = outs(), outs(invariants={"x": {"score": 0.0, "comment": ""}})
    assert all_invariants_pass([good], [{}])["score"] == 1.0
    assert all_invariants_pass([good, bad], [{}, {}])["score"] == 0.0


# ---- SDK 연동 경계
def test_run_experiment_refuses_without_key_or_client(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="run_local"):
        run_experiment()


def test_to_sdk_examples_are_deterministic():
    a, b = to_sdk_examples(datasets.scenario_examples()), to_sdk_examples(datasets.scenario_examples())
    assert [x.id for x in a] == [x.id for x in b] and len({x.id for x in a}) == len(a)
    assert a[0].inputs["scenario_id"] and a[0].metadata["category"]


class FakeClient:
    def __init__(self, existing=(), fail=False):
        self.existing, self.fail, self.created, self.dataset_made = list(existing), fail, [], False

    def has_dataset(self, dataset_name): 
        if self.fail: raise ConnectionError("down")
        return self.dataset_made or bool(self.existing)

    def create_dataset(self, name, description=""):
        if self.fail: raise ConnectionError("down")
        self.dataset_made = True; return object()

    def list_examples(self, dataset_name):
        class E:  # noqa: D401
            def __init__(s, sid): s.metadata = {"scenario_id": sid}
        return [E(s) for s in self.existing]

    def create_examples(self, dataset_name, inputs, outputs, metadata):
        self.created += [m["scenario_id"] for m in metadata]


def test_push_dataset_is_idempotent_and_failure_tolerant():
    ex = datasets.scenario_examples()
    c = FakeClient(existing=["jeju_trip_payment"])
    r = datasets.push_dataset(c, ex)
    assert r["skipped"] == 1 and r["created"] == len(ex) - 1 and "jeju_trip_payment" not in c.created
    assert datasets.push_dataset(FakeClient(fail=True), ex) == {"created": 0, "skipped": 0, "error": 1}


def test_safe_call_swallows_and_returns_default():
    def boom(): raise ValueError("x")
    assert safe_call(boom, default="d") == "d"
