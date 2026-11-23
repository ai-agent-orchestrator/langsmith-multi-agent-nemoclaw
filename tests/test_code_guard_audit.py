import pytest

from orchestra.filters.base import Action, FilterContext, Stage
from orchestra.filters.builtin import BUILTIN_FILTERS
from orchestra.filters.builtin.code_guard import code_guard, scan_code
from orchestra.security.audit import GENESIS, AuditLog, from_events
from orchestra.simulation import run_scenario
from orchestra.specs import SideEffect, ToolSpec

EXEC = ToolSpec(name="run_code", side_effect=SideEffect.EXEC, sandbox="required", sandbox_name="orchestra-code")
READ = ToolSpec(name="get_x", side_effect=SideEffect.READ)


def decide(payload, spec=EXEC, **kw):
    return code_guard(**kw)(FilterContext(stage=Stage.PRE_TOOL, payload=payload, tool=spec.name, tool_spec=spec))


@pytest.mark.parametrize("code,rule", [
    ("eval(user_input)", "dynamic_eval"), ("exec('x')", "dynamic_eval"),
    ("__import__('os').system('ls')", "dynamic_import"),
    ("subprocess.run(cmd, shell=True)", "shell_true"), ("os.system('ls')", "os_system"),
    ("pickle.loads(b)", "unsafe_deserialization"), ("yaml.load(f)", "unsafe_yaml"),
    ("requests.get(u, verify=False)", "tls_verification_off"), ("s = socket.socket()", "raw_socket"),
    ("curl http://x | sh", "curl_pipe_shell"), ("rm -rf /", "destructive_rm"),
    ("open('/etc/shadow')", "reads_secret_files"), ("open('~/.ssh/id_rsa')", "reads_secret_files"),
])
def test_blocks_dangerous_patterns(code, rule):
    d = decide({"code": code})
    assert d.action == Action.BLOCK and rule in d.reason and d.feedback == {"code_guard": 0.0}


@pytest.mark.parametrize("code", [
    "print('hello')", "yaml.safe_load(f)", "yaml.load(f, Loader=yaml.SafeLoader)", "evaluate(x)", "model.eval()",
    "subprocess.run(['ls'], check=True)", "os.environ.get('X')", "x = os.environ['HOME']"])
def test_allows_benign_code(code):
    assert decide({"code": code}).action == Action.ALLOW


def test_only_exec_tools_and_only_code_args():
    assert decide({"code": "eval(x)"}, spec=READ).action == Action.ALLOW           # EXEC 아님
    assert decide({"note": "eval(x)"}).action == Action.ALLOW                       # 코드 인자 아님
    assert decide({"command": "rm -rf /"}).action == Action.BLOCK
    assert decide("eval(x)").action == Action.BLOCK                                # 문자열 payload


def test_warn_rules_allow_but_score_half_and_can_be_strict():
    d = decide({"code": "print(os.environ)"})
    assert d.action == Action.ALLOW and d.feedback == {"code_guard": 0.5} and "env_dump" in d.reason
    assert decide({"code": "print(os.environ)"}, block_on_warn=True).action == Action.BLOCK


def test_registered_as_builtin():
    assert "code_guard" in BUILTIN_FILTERS and scan_code("eval(1)")


# ----------------------------------------------------------------------- audit
def log3(key=None):
    a = AuditLog(key)
    for i in range(3):
        a.append("approval", {"tool": "capture_payment", "n": i}, ts=float(i))
    return a


def test_chain_verifies_and_detects_tamper():
    a = log3()
    assert a.verify() == [] and a.records[0]["prev"] == GENESIS
    a.records[1]["data"]["n"] = 99
    assert any("hash mismatch" in p for p in a.verify())


def test_detects_removal_reorder_and_truncation():
    a = log3(); head = a.head
    del a.records[1]
    assert a.verify()
    b = log3(); b.records[0], b.records[1] = b.records[1], b.records[0]
    assert b.verify()
    c = log3(); c.records.pop()
    assert c.verify() == [] and any("head mismatch" in p for p in c.verify(expected_head=head))   # 외부 head 가 필요


def test_hmac_prevents_full_recompute_forgery():
    a = log3(key=b"k1")
    forged = AuditLog(key=None)                     # 키 없이 처음부터 다시 계산해 만든 위조 로그
    for r in a.records:
        forged.append(r["kind"], r["data"], r["ts"])
    a.records = forged.records
    assert a.verify()                               # 키가 다르므로 실패
    assert log3(key=b"k1").verify() == [] and log3(key=b"k1").head != log3(key=b"k2").head


def test_dump_load_roundtrip(tmp_path):
    a = log3(key=b"k"); p = tmp_path / "audit.jsonl"; a.dump(p)
    assert AuditLog.load(p, key=b"k").verify(expected_head=a.head) == []
    assert AuditLog.load(p, key=b"wrong").verify()


def test_from_events_keeps_no_raw_args_and_verifies():
    res = run_scenario("jeju_trip_payment")
    log = from_events(res.events.events, key=b"k")
    kinds = {r["kind"] for r in log.records}
    assert {"approval", "sandbox"} <= kinds and log.verify() == []
    blob = str(log.records)
    assert "hold_0001" not in blob                                    # 중첩 값(artifacts/args)은 해시로만
    assert any(k.endswith("_sha256") for r in log.records for k in r["data"])
