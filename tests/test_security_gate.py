import json
import subprocess
import sys
from pathlib import Path

from scripts.security_gate import main

ROOT = Path(__file__).resolve().parent.parent


def garak(tmp_path, passed, total):
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps({"entry_type": "eval", "probe": "xss.A", "detector": "d", "passed": passed, "total": total}))
    return str(p)


def findings(tmp_path, sev):
    p = tmp_path / "f.json"
    item = {"verdict": "confirmed", "fingerprint": "f1", "title": "t", "evidence": [{"file": "a.py", "line": 1, "description": "d"}],
            "severity": {"overall_severity": sev}}
    p.write_text(json.dumps([item]))
    return str(p)


def test_pass_and_fail_exit_codes(tmp_path, capsys):
    assert main(["--garak", garak(tmp_path, 10, 10)]) == 0
    assert main(["--garak", garak(tmp_path, 9, 10)]) == 1
    assert main(["--garak", garak(tmp_path, 9, 10), "--max-fail-rate", "0.2"]) == 0
    assert main(["--findings", findings(tmp_path, "high")]) == 1
    assert main(["--findings", findings(tmp_path, "medium")]) == 0
    assert main(["--findings", findings(tmp_path, "medium"), "--fail-on", "medium"]) == 1


def test_bad_inputs_are_not_a_pass(tmp_path):
    assert main([]) == 2
    assert main(["--garak", str(tmp_path / "missing.jsonl")]) == 2
    empty = tmp_path / "e.jsonl"; empty.write_text("")
    assert main(["--garak", str(empty)]) == 2
    bad = tmp_path / "b.json"; bad.write_text("{not json")
    assert main(["--findings", str(bad)]) == 2


def test_script_runs_as_cli(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "security_gate.py"), "--garak", garak(tmp_path, 10, 10)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "PASS" in r.stdout
