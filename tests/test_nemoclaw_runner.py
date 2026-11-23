import pytest

from orchestra.sandbox.base import ExecRequest, ExecResult
from orchestra.sandbox.nemoclaw import policy
from orchestra.sandbox.nemoclaw.drift import PolicyDriftError, verify_startup
from orchestra.sandbox.nemoclaw.runner import (CommandError, CommandResult, NemoClawRunner,
                                               parse_presets_from_policy)
from orchestra.simulation import run_scenario


class FakeExec:
    def __init__(self, table=None, default=None):
        self.table, self.default, self.calls = table or {}, default, []

    def run(self, argv, *, timeout_s):
        self.calls.append((argv, timeout_s))
        r = self.table.get(argv[1] if len(argv) > 1 else argv[0], self.default)
        return r(argv) if callable(r) else (r or CommandResult(argv, 0, ""))


@pytest.fixture(autouse=True)
def real_payment_host(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDER_HOST", "api.pay.test-provider.com")


def specs():
    return [s for _, s in run_scenario("jeju_trip_payment").app.resolved.tools.values()]


def test_rejects_unsafe_names_and_never_uses_shell():
    r = NemoClawRunner(FakeExec())
    for bad in ["x; rm -rf /", "a b", "../x", "", "UPPER", "$(id)"]:
        with pytest.raises(CommandError):
            r.policy_get(bad)
    assert not r.executor.calls                                 # 실행 시도조차 없음


def test_dry_run_records_argv_only():
    ex = FakeExec()
    r = NemoClawRunner(ex, dry_run=True)
    r.policy_add("orchestra-payments", "orchestra-payments-egress")
    assert not ex.calls and r.history[0].dry_run
    assert r.history[0].argv == ["nemoclaw", "orchestra-payments", "policy-add", "orchestra-payments-egress"]


def test_no_whole_policy_replace_template():
    assert not hasattr(NemoClawRunner(FakeExec()).t, "policy_set")


def test_exec_fails_closed_without_transport_or_on_transport_error():
    req = ExecRequest(sandbox="orchestra-payments", tool="capture_payment")
    assert NemoClawRunner(FakeExec()).exec(req).error.startswith("sandbox_unavailable")

    def boom(_): raise TimeoutError("worker down")
    res = NemoClawRunner(FakeExec(), transport=boom).exec(req)
    assert not res.ok and "sandbox_unavailable" in res.error and "TimeoutError" in res.error
    ok = NemoClawRunner(FakeExec(), transport=lambda r: ExecResult(ok=True, output=1)).exec(req)
    assert ok.ok


def test_doctor_reports_missing_cli():
    r = NemoClawRunner(FakeExec(default=CommandResult([], 127, stderr="command not found: nemoclaw")))
    d = r.doctor()
    assert d["ok"] is False and "not found" in d["problem"]


def test_parse_presets():
    text = "network_policies:\n  x:\n    preset: orchestra-payments-egress\n  y:\n    preset: orchestra-langsmith\n"
    assert parse_presets_from_policy(text) == ["orchestra-langsmith", "orchestra-payments-egress"]


# ------------------------------------------------------------------ drift (R2)
def runner_with(applied: dict[str, str]):
    def run(argv):
        sb = argv[-1]
        if sb not in applied:
            return CommandResult(argv, 1, stderr="no such sandbox")
        return CommandResult(argv, 0, applied[sb])
    return NemoClawRunner(FakeExec(table={"policy": run}))


def policy_text(*names):
    return "".join(f"  p{i}:\n    preset: {n}\n" for i, n in enumerate(names))


def test_startup_ok_when_applied_matches():
    r = runner_with({"orchestra-payments": policy_text("orchestra-payments-egress", "orchestra-langsmith")})
    verify_startup(specs(), r.applied_presets, security_mode="masked")


def test_startup_fails_on_missing_preset():
    r = runner_with({"orchestra-payments": policy_text("orchestra-langsmith")})
    with pytest.raises(PolicyDriftError) as e:
        verify_startup(specs(), r.applied_presets, security_mode="masked")
    assert any("missing presets" in p for p in e.value.problems)


def test_startup_fails_when_langsmith_applied_in_closed_mode():
    r = runner_with({"orchestra-payments": policy_text("orchestra-payments-egress", "orchestra-langsmith")})
    with pytest.raises(PolicyDriftError) as e:
        verify_startup(specs(), r.applied_presets, security_mode="closed")
    assert any("forbids trace egress" in p for p in e.value.problems)


def test_startup_fails_when_policy_unreadable():
    r = runner_with({})
    with pytest.raises(PolicyDriftError) as e:
        verify_startup(specs(), r.applied_presets, security_mode="masked")
    assert any("cannot read applied policy" in p for p in e.value.problems)


def test_startup_fails_on_file_drift(tmp_path):
    presets = policy.build_presets(specs(), security_mode="masked")
    policy.write_all(presets, tmp_path)
    f = next(tmp_path.glob("orchestra-payments-egress.yaml"))
    f.write_text(f.read_text() + "\n# tampered\n    extra: 1\n")
    r = runner_with({"orchestra-payments": policy_text("orchestra-payments-egress", "orchestra-langsmith")})
    with pytest.raises(PolicyDriftError) as e:
        verify_startup(specs(), r.applied_presets, security_mode="masked", preset_dir=tmp_path)
    assert any(p.startswith("file drift") for p in e.value.problems)


def test_startup_fails_on_placeholder_payment_host(monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER_HOST", raising=False)
    r = runner_with({"orchestra-payments": policy_text("orchestra-payments-egress", "orchestra-langsmith")})
    with pytest.raises(PolicyDriftError) as e:
        verify_startup(specs(), r.applied_presets, security_mode="masked")
    assert any("placeholder host" in p for p in e.value.problems)
