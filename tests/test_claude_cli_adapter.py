"""ClaudeCliAgent against a fake executable: flag probing, bounded output, env scrubbing, worker-only use."""
import json
import os
import stat
import sys

import pytest

from orchestra.modules.cli_adapter import ClaudeCliAgent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script with a shebang")

SCRIPT = '''#!{python}
import json, os, sys
if "--help" in sys.argv:
    print("{help_text}")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
prompt = sys.stdin.read()
if mode == "big":
    sys.stdout.write("x" * 2_000_000)
elif mode == "list":
    print("[]")
elif mode == "fail":
    sys.exit(3)
elif mode == "hang":
    import time; time.sleep(30)
else:
    print(json.dumps({{"result": prompt, "leaked_key": os.environ.get("ANTHROPIC_API_KEY"), "cwd": os.getcwd(),
                       "argv": sys.argv[1:]}}))
'''


def make(tmp_path, help_text="--plugin-dir --output-format", **kwargs):
    binary = tmp_path / "claude"
    binary.write_text(SCRIPT.format(python=sys.executable, help_text=help_text))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    plugins = tmp_path / "plugins"
    plugins.mkdir(exist_ok=True)
    return ClaudeCliAgent(str(binary), plugin_dir=plugins, **kwargs)


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_WORKER_SANDBOX", "orchestra-code")


def test_host_is_refused_without_worker_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_WORKER_SANDBOX", raising=False)
    assert make(tmp_path).invoke("hi") == {"error": "code_worker_required"}


def test_success_uses_fixed_flags_stdin_and_scrubbed_env(tmp_path, worker, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = make(tmp_path, workspace=workspace).invoke("hello")
    assert out["result"] == "hello" and out["leaked_key"] is None
    assert os.path.realpath(out["cwd"]) == str(workspace.resolve())
    assert out["argv"][:3] == ["-p", "--output-format", "json"] and out["argv"][3] == "--plugin-dir"


def test_missing_flags_make_cli_unavailable(tmp_path, worker):
    agent = make(tmp_path, help_text="--other")
    assert not agent.available() and agent.invoke("hi") == {"error": "claude_cli_unavailable"}


@pytest.mark.parametrize("mode,error", [("fail", "claude_cli_failed"), ("big", "claude_cli_failed"),
                                        ("list", "claude_cli_invalid_output")])
def test_failures_return_codes_only(tmp_path, worker, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_MODE", mode)
    # FAKE_MODE is not on the env allowlist, so the fake would run in "ok" mode; allow it for this test only.
    monkeypatch.setattr("orchestra.modules.cli_adapter._ENV_ALLOWLIST", {"PATH", "FAKE_MODE"})
    assert make(tmp_path).invoke("hi") == {"error": error}


def test_timeout_is_enforced(tmp_path, worker, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr("orchestra.modules.cli_adapter._ENV_ALLOWLIST", {"PATH", "FAKE_MODE"})
    agent = make(tmp_path, timeout_s=0.5)
    assert agent.invoke("hi") == {"error": "claude_cli_failed"}


@pytest.mark.parametrize("prompt", ["", "a" * 20_001])
def test_prompt_bounds(tmp_path, worker, prompt):
    assert make(tmp_path).invoke(prompt) == {"error": "claude_cli_unavailable"}


def test_missing_binary_is_unavailable(tmp_path, worker):
    agent = ClaudeCliAgent(str(tmp_path / "nope"), plugin_dir=tmp_path)
    assert agent.invoke("hi") == {"error": "claude_cli_unavailable"}
