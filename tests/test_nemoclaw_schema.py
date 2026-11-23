"""생성된 preset 이 NemoClaw 공식 JSON 스키마(vendor/nemoclaw-schemas)를 통과하는지 — R0 실측 근거."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from orchestra.sandbox.nemoclaw import policy
from orchestra.simulation import run_scenario
from orchestra.specs import SANDBOX_NAME_RE, ToolSpec

SCHEMAS = Path(__file__).resolve().parent.parent / "vendor" / "nemoclaw-schemas"


@pytest.fixture(autouse=True)
def host(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDER_HOST", "api.pay.test-provider.com")


def validator():
    docs = {n: json.loads((SCHEMAS / f"{n}.schema.json").read_text(encoding="utf-8"))
            for n in ("policy-preset", "network-policy")}
    reg = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs.values())
    return Draft202012Validator(docs["policy-preset"], registry=reg)


def presets(mode="masked"):
    specs = [s for _, s in run_scenario("jeju_trip_payment").app.resolved.tools.values()]
    return policy.build_presets(specs, security_mode=mode)


@pytest.mark.parametrize("mode", ["off", "masked", "closed", "self_hosted"])
def test_generated_presets_match_official_schema(mode):
    v = validator()
    for name, doc in presets(mode).items():
        errs = [e.message for e in v.iter_errors(doc)]
        assert errs == [], (name, errs)


def test_schema_actually_rejects_bad_presets():
    v = validator()
    bad = presets()["orchestra-payments-egress"]
    del bad["network_policies"][next(iter(bad["network_policies"]))]["binaries"]
    assert list(v.iter_errors(bad))
    worse = {"preset": {"name": "x"}, "network_policies": {}}
    assert list(v.iter_errors(worse))


def test_sandbox_name_rule_matches_nemoclaw_docs():
    ok = ["orchestra-payments", "a", "abc-def-1", "a" * 19]
    bad = ["", "A", "1abc", "a--b", "a-", "-a", "a" * 20, "has space", "under_score"]
    assert all(SANDBOX_NAME_RE.match(n) for n in ok)
    assert not any(SANDBOX_NAME_RE.match(n) for n in bad)


def test_tool_spec_rejects_invalid_sandbox_name():
    with pytest.raises(Exception):
        ToolSpec(name="t", side_effect="exec", sandbox="always", sandbox_name="Bad_Name")
