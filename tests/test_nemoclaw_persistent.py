"""Persistent worker transport: real processes stand in for the sandbox, so no NemoClaw is needed."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import textwrap
import threading
import time

import pytest

from orchestra.sandbox.nemoclaw.bootstrap import make_runner
from orchestra.sandbox.nemoclaw.cli import CommandResult, NemoClawCli, SERVE_COMMAND
from orchestra.sandbox.nemoclaw.persistent import PersistentTransport
from orchestra.sandbox.nemoclaw.ssh_launcher import OpenShellSshLauncher, parse_ssh_config
from orchestra.sandbox.nemoclaw.transport import StdioTransport
from orchestra.sandbox.nemoclaw.worker import MAX_BYTES, MAX_LINE_BYTES, serve

ROOT = Path(__file__).resolve().parents[1]
BOX = "test-box"

# The real Worker and serve loop with one READ tool, so the round trip covers capability checks too.
REAL_WORKER = textwrap.dedent('''
    import os, subprocess, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from langchain_core.tools import tool
    from orchestra.filters.chain import FilterChain
    from orchestra.sandbox.nemoclaw.worker import Worker, serve, take_protocol_streams
    from orchestra.security.capability import CapabilityAuthority
    from orchestra.specs import SideEffect, ToolSpec

    @tool
    def whoami(text: str) -> str:
        """Echo the text with the worker's process id."""
        print("stray tool output")
        os.write(1, b"stray fd output\\n")
        subprocess.run([sys.executable, "-c", "import sys; print('child'); sys.stdin.read()"], check=True)
        return f"{os.getpid()}:{text}"

    source, sink = take_protocol_streams()
    grants = {"reader": {"whoami"}}
    spec = ToolSpec(name="whoami", side_effect=SideEffect.READ, sandbox_name="test-box")
    worker = Worker("test-box", {"whoami": (whoami, spec)}, FilterChain([]),
                    journal=Path(sys.argv[2]) / "journal.sqlite", grants=grants,
                    authority=CapabilityAuthority(b"k" * 32, grants))
    raise SystemExit(serve(worker, source, sink))
''')

# A protocol peer that misbehaves on demand. The request names the behaviour.
FAKE_WORKER = textwrap.dedent('''
    import json, os, subprocess, sys, time
    mode, log = sys.argv[1], sys.argv[2]
    out = sys.stdout.buffer

    def send(raw):
        out.write(raw)
        out.flush()

    def record(text):
        with open(log, "a") as handle:
            handle.write(text + "\\n")

    record(f"start {os.getpid()}")
    ready = json.dumps({"orchestra_worker": 1, "ready": True}).encode() + b"\\n"
    if mode == "exit-at-start":
        raise SystemExit(1)
    if mode == "silent-start":
        time.sleep(60)
    if mode == "banner":
        send(b"Welcome to the sandbox\\n")
    if mode == "wrong-version":
        ready = json.dumps({"orchestra_worker": 2, "ready": True}).encode() + b"\\n"
    send(ready)
    for line in sys.stdin.buffer:
        frame = json.loads(line)
        action = frame["request"]
        record(f"request {action}")
        reply = json.dumps({"id": frame["id"], "response": f"{os.getpid()} {action}"}).encode() + b"\\n"
        if action == "hang":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            record(f"child {child.pid}")
            time.sleep(60)
        elif action == "exit":
            raise SystemExit(1)
        elif action == "garbage":
            send(b"not json\\n")
        elif action == "deep":
            send(b"[" * 200000 + b"\\n")
        elif action == "wrong-id":
            send(json.dumps({"id": "other", "response": "x"}).encode() + b"\\n")
        elif action == "extra-field":
            send(json.dumps({"id": frame["id"], "response": "x", "more": 1}).encode() + b"\\n")
        elif action == "two-lines":
            send(reply + reply)
        elif action == "partial":
            send(reply[:10])
        elif action == "flood":
            send(b"x" * (3 * 1024 * 1024))
            time.sleep(60)
        elif action == "big":
            send(json.dumps({"id": frame["id"], "response": "x" * (1024 * 1024 + 1)}).encode() + b"\\n")
        elif action == "late-noise":
            send(reply)
            time.sleep(0.1)
            send(b"noise\\n")
        elif action == "die-after":
            send(reply)
            raise SystemExit(0)
        elif action.startswith("slow"):
            time.sleep(0.5)
            send(reply)
        else:
            send(reply)
''')


class Fake:
    """A transport wired to a scripted peer, with a log of what the peer saw."""

    def __init__(self, tmp_path: Path, mode: str = "ok", **options: float) -> None:
        self.script, self.log = tmp_path / "fake_worker.py", tmp_path / "log"
        self.script.write_text(FAKE_WORKER, encoding="utf-8")
        self.mode, self.sandboxes = mode, []
        self.transport = PersistentTransport(self.launch, **options)

    def launch(self, sandbox: str) -> list[str]:
        self.sandboxes.append(sandbox)
        return [sys.executable, str(self.script), self.mode, str(self.log)]

    def lines(self, kind: str) -> list[str]:
        text = self.log.read_text() if self.log.exists() else ""
        return [line.split(" ", 1)[1] for line in text.splitlines() if line.startswith(kind + " ")]

    def ask(self, action: str, timeout_s: float = 10.0) -> str:
        return self.transport.exchange(BOX, action, timeout_s)


@pytest.fixture
def fake(tmp_path):
    made: list[Fake] = []

    def make(mode: str = "ok", **options: float) -> Fake:
        made.append(Fake(tmp_path, mode, **options))
        return made[-1]

    yield make
    for item in made:
        item.transport.close()


def _gone(pid: int) -> bool:
    """True once the process has exited (a zombie counts as exited)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.02)
    return False


def test_real_worker_round_trip_reuses_one_process_and_keeps_stdout_clean(tmp_path):
    from orchestra.sandbox.base import ExecRequest
    from orchestra.security.capability import CapabilityAuthority

    script = tmp_path / "real_worker.py"
    script.write_text(REAL_WORKER, encoding="utf-8")
    launches: list[str] = []

    def launch(sandbox: str) -> list[str]:
        launches.append(sandbox)
        return [sys.executable, str(script), str(ROOT), str(tmp_path)]

    authority = CapabilityAuthority(b"k" * 32, {"reader": {"whoami"}})
    transport = PersistentTransport(launch)
    try:
        outputs = []
        for text in ("첫째", 'second "quoted"\nline'):
            request = ExecRequest(sandbox=BOX, tool="whoami", args={"text": text}, agent="reader")
            request.capability_token = authority.issue(request.model_dump())
            result = json.loads(transport.exchange(BOX, request.model_dump_json(), 20))
            assert result["ok"] is True, result
            pid, echoed = result["output"].split(":", 1)
            assert echoed == text
            outputs.append(int(pid))
        forged = ExecRequest(sandbox=BOX, tool="whoami", args={"text": "x"}, agent="reader", capability_token="bad")
        refused = json.loads(transport.exchange(BOX, forged.model_dump_json(), 20))
        assert refused["error"] == "worker_capability_invalid"
        assert outputs[0] == outputs[1] != os.getpid()
        assert launches == [BOX]
    finally:
        transport.close()
    assert _gone(outputs[0])


@pytest.mark.parametrize("line", [
    b"not json\n", b"[]\n", b'{"id":"a"}\n', b'{"id":"a","request":"b","extra":1}\n',
    b'{"id":1,"request":"b"}\n', b'{"id":"","request":"b"}\n', b'{"id":"a","request":{}}\n',
    b'{"id":"a","request":"unterminated"}', b"x" * (MAX_LINE_BYTES + 1) + b"\n",
])
def test_serve_ends_the_session_on_a_bad_frame(line):
    class Never:
        def handle(self, payload: str) -> str:
            raise AssertionError("a bad frame must not reach the worker")

    sink = io.BytesIO()
    assert serve(Never(), io.BytesIO(line), sink) == 1  # type: ignore[arg-type]
    assert sink.getvalue() == b'{"orchestra_worker": 1, "ready": true}\n'


def test_serve_answers_each_line_and_stops_at_eof():
    class Echo:
        def handle(self, payload: str) -> str:
            return payload.upper()

    source = io.BytesIO(b'{"id":"1","request":"a"}\n{"id":"2","request":"b"}\n')
    sink = io.BytesIO()
    assert serve(Echo(), source, sink) == 0  # type: ignore[arg-type]
    assert [json.loads(line) for line in sink.getvalue().splitlines()[1:]] == [
        {"id": "1", "response": "A"}, {"id": "2", "response": "B"}]


@pytest.mark.parametrize("arguments", [["--serve"], ["--unknown"]])
def test_unconfigured_worker_fails_without_a_ready_line(arguments):
    import subprocess

    env = {key: value for key, value in os.environ.items() if not key.startswith("ORCHESTRA_")}
    done = subprocess.run([sys.executable, "-m", "orchestra.sandbox.nemoclaw.worker", *arguments], cwd=ROOT,
                          env=env, input=b"", capture_output=True, timeout=60)
    assert done.returncode == 1
    assert b"ready" not in done.stdout


@pytest.mark.parametrize("action,error", [
    ("exit", "worker_exited"), ("garbage", "worker_protocol_error"), ("deep", "worker_protocol_error"),
    ("wrong-id", "worker_protocol_error"), ("extra-field", "worker_protocol_error"),
    ("two-lines", "worker_protocol_error"), ("flood", "worker_output_too_large"),
    ("big", "worker_output_too_large"),
])
def test_bad_reply_fails_closed_without_resending_then_restarts(fake, action, error):
    peer = fake()
    with pytest.raises(RuntimeError) as caught:
        peer.ask(action)
    assert str(caught.value) == error
    first = int(peer.lines("start")[0])
    assert _gone(first)
    assert peer.ask("again").endswith(" again")
    assert peer.lines("request") == [action, "again"]
    assert len(peer.lines("start")) == 2


@pytest.mark.parametrize("action", ["hang", "partial"])
def test_timeout_kills_the_whole_process_group(fake, action):
    peer = fake()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="^worker_timeout$"):
        peer.ask(action, timeout_s=1.0)
    assert time.monotonic() - started < 6
    assert _gone(int(peer.lines("start")[0]))
    for child in peer.lines("child"):
        assert _gone(int(child))
    assert peer.lines("request") == [action]


@pytest.mark.parametrize("mode,error", [
    ("exit-at-start", "worker_exited"), ("silent-start", "worker_timeout"),
    ("banner", "worker_handshake_failed"), ("wrong-version", "worker_handshake_failed"),
])
def test_failed_handshake_sends_nothing(fake, mode, error):
    peer = fake(mode, startup_timeout_s=2.0 if mode == "silent-start" else 30.0)
    with pytest.raises(RuntimeError) as caught:
        peer.ask("never")
    # A banner may arrive in the same read as the ready line, which is also a framing error.
    assert str(caught.value) in {error, "worker_protocol_error"} if mode == "banner" else str(caught.value) == error
    assert peer.lines("request") == []
    assert _gone(int(peer.lines("start")[0]))


def test_restarts_are_limited_until_the_cooldown_passes(fake):
    peer = fake("exit-at-start", max_failures=2, cooldown_s=60.0)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="^worker_exited$"):
            peer.ask("x")
    with pytest.raises(RuntimeError, match="^worker_restart_limited$"):
        peer.ask("x")
    assert len(peer.sandboxes) == 2
    peer.transport._slots[BOX].blocked_until = 0.0
    peer.mode = "ok"
    assert peer.ask("x").endswith(" x")
    assert peer.transport._slots[BOX].failures == 0


@pytest.mark.parametrize("action", ["die-after", "late-noise"])
def test_a_session_unusable_while_idle_is_replaced_before_sending(fake, action):
    peer = fake()
    first = peer.ask(action).split()[0]
    time.sleep(0.4)
    assert peer.ask("next").split()[0] != first
    assert peer.lines("request") == [action, "next"]


def test_launch_errors_are_reduced_to_a_fixed_code():
    def broken(sandbox: str) -> list[str]:
        raise OSError("/home/someone/private")

    for launcher in (broken, lambda sandbox: ["/nonexistent/worker"]):
        with pytest.raises(RuntimeError) as caught:
            PersistentTransport(launcher).exchange(BOX, "{}", 5)
        assert str(caught.value) == "worker_launch_failed"
        assert caught.value.__suppress_context__


def test_size_and_timeout_limits_are_checked_before_starting(fake):
    peer = fake()
    for payload in ("x" * (MAX_BYTES + 1), "\x01" * MAX_BYTES):
        with pytest.raises(RuntimeError, match="^worker_request_too_large$"):
            peer.transport.exchange(BOX, payload, 5)
    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(RuntimeError, match="^worker_invalid_timeout$"):
            peer.ask("x", timeout_s=timeout)
    assert peer.sandboxes == []
    assert peer.ask("y" * (MAX_BYTES - 32), timeout_s=20).endswith("y")  # the reply adds a pid prefix


def test_requests_to_one_sandbox_are_serialized(fake):
    peer = fake()
    peer.ask("warm")
    results: dict[str, str] = {}
    threads = [threading.Thread(target=lambda name=name: results.__setitem__(name, peer.ask(name)))
               for name in ("slow-a", "slow-b")]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    with pytest.raises(RuntimeError, match="^worker_busy$"):
        peer.ask("impatient", timeout_s=0.1)
    for thread in threads:
        thread.join(timeout=20)
    assert {name: value.split()[1] for name, value in results.items()} == {"slow-a": "slow-a", "slow-b": "slow-b"}
    assert len(peer.sandboxes) == 1


def test_each_sandbox_gets_its_own_worker(fake):
    peer = fake()
    one = peer.transport.exchange("box-one", "a", 10).split()[0]
    two = peer.transport.exchange("box-two", "b", 10).split()[0]
    assert one != two
    assert peer.sandboxes == ["box-one", "box-two"]


def test_close_stops_workers_and_interrupts_a_request_in_flight(fake):
    peer = fake()
    peer.ask("warm")
    errors: list[str] = []

    def stuck() -> None:
        try:
            peer.ask("hang", timeout_s=30)
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=stuck)
    thread.start()
    deadline = time.monotonic() + 5
    while not peer.lines("child") and time.monotonic() < deadline:
        time.sleep(0.02)
    peer.transport.close()
    thread.join(timeout=10)
    assert errors == ["worker_exited"]
    for pid in (*peer.lines("start"), *peer.lines("child")):
        assert _gone(int(pid))
    with pytest.raises(RuntimeError, match="^worker_transport_closed$"):
        peer.ask("after")


SSH_CONFIG = """Host openshell-test-box.default
    User sandbox
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    GlobalKnownHostsFile /dev/null
    LogLevel ERROR
    ServerAliveInterval 15
    ServerAliveCountMax 3
    ProxyCommand /usr/local/bin/openshell ssh-proxy --gateway-name nemoclaw --name test-box --workspace default
"""
PROXY = "/usr/local/bin/openshell ssh-proxy --gateway-name nemoclaw --name test-box --workspace default"


def test_ssh_config_parser_returns_host_and_proxy():
    assert parse_ssh_config(SSH_CONFIG, BOX, "openshell") == ("openshell-test-box.default", PROXY)


@pytest.mark.parametrize("old,new", [
    ("/usr/local/bin/openshell ssh-proxy", "/bin/sh ssh-proxy"),
    ("ssh-proxy", "exec"),
    ("--name test-box", "--name other-box"),
    ("--workspace default", "--workspace default --token x"),
    ("--workspace default", "--workspace 'default;id'"),
    ("--gateway-name nemoclaw", "--gateway-name $(id)"),
    ("--gateway-name nemoclaw", "--gateway-name %h"),
    ("--gateway-name nemoclaw", "--server nemoclaw"),
    ("Host openshell-test-box.default", "Host -oProxyCommand=id"),
    ("Host openshell-test-box.default", "Host a b"),
    ("Host openshell-test-box.default\n", "Host one\nHost two\n"),
    ("    ProxyCommand", "    #ProxyCommand"),
    ("--workspace default\n", "--workspace default\n    ProxyCommand /bin/true\n"),
    ("--workspace default", "--workspace 'unclosed"),
])
def test_ssh_config_parser_refuses_anything_else(old, new):
    assert old in SSH_CONFIG
    with pytest.raises(ValueError):
        parse_ssh_config(SSH_CONFIG.replace(old, new), BOX, "openshell")


class _ConfigCli(NemoClawCli):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(dialect="modern")
        self.result, self.calls = result, []

    def run(self, action, *, sandbox=None, preset=None, input_text=None, timeout_s=None):
        self.calls.append((action, sandbox))
        return self.result


def test_ssh_launcher_builds_a_fixed_command():
    cli = _ConfigCli(CommandResult((), True, stdout=SSH_CONFIG))
    argv = OpenShellSshLauncher(cli)(BOX)
    assert cli.calls == [("ssh_config", BOX)]
    assert argv[:4] == ["ssh", "-F", "/dev/null", "-T"]
    assert argv[-2] == "sandbox@openshell-test-box.default"
    assert argv[-1] == ("/opt/orchestra/venv/bin/python -P -m orchestra.sandbox.nemoclaw.worker --serve")
    assert argv[-1].split() == list(SERVE_COMMAND)
    options = [argv[index + 1] for index, part in enumerate(argv) if part == "-o"]
    assert f"ProxyCommand={PROXY}" in options
    for required in ("BatchMode=yes", "ForwardAgent=no", "IdentityAgent=none", "ClearAllForwardings=yes",
                     "PubkeyAuthentication=no", "PasswordAuthentication=no"):
        assert required in options


@pytest.mark.parametrize("result", [CommandResult((), False, error="cli_failed"),
                                    CommandResult((), True, dry_run=True)])
def test_ssh_launcher_refuses_without_a_live_config(result):
    with pytest.raises(RuntimeError, match="^ssh_config_unavailable$"):
        OpenShellSshLauncher(_ConfigCli(result))(BOX)


def test_wrapper_commands_for_the_worker():
    cli = NemoClawCli(dry_run=True, dialect="modern")
    one_shot = cli.command("worker", sandbox=BOX)
    assert one_shot[:5] == ("nemoclaw", BOX, "exec", "--stdin", "--")
    assert cli.command("ssh_config", sandbox=BOX) == ("openshell", "sandbox", "ssh-config", BOX)
    for sandbox in (None, "-x", "a b", "a;id"):
        with pytest.raises(ValueError):
            cli.command("ssh_config", sandbox=sandbox)


@pytest.mark.parametrize("kind,verified,expected", [
    (None, "1", StdioTransport), ("stdio_via_cli", "1", StdioTransport),
    ("ssh_stream", "1", PersistentTransport), ("ssh_stream", "0", type(None)),
])
def test_bootstrap_selects_the_transport_only_when_verified(monkeypatch, tmp_path, kind, verified, expected):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    for name in ("NEMOCLAW_TRANSPORT", "NEMOCLAW_REVIEWED_BASELINE", "NEMOCLAW_CLI_DIALECT"):
        monkeypatch.delenv(name, raising=False)
    if kind:
        monkeypatch.setenv("NEMOCLAW_TRANSPORT", kind)
    monkeypatch.setenv("NEMOCLAW_EXEC_VERIFIED", verified)
    monkeypatch.setenv("ORCHESTRA_CAPABILITY_KEY_FILE", str(key))
    assert type(make_runner([], {}).transport) is expected


def test_bootstrap_rejects_an_unknown_transport(monkeypatch):
    monkeypatch.setenv("NEMOCLAW_TRANSPORT", "http_forward")
    with pytest.raises(ValueError, match="invalid_nemoclaw_transport"):
        make_runner([], {})


@pytest.mark.nemoclaw
def test_live_persistent_worker_round_trip():
    """Real install only. Sends a request for a tool that does not exist, so nothing is executed.

    Needs ORCHESTRA_NEMOCLAW_LIVE_SANDBOX naming a sandbox whose image has the worker installed.
    """
    sandbox = os.environ.get("ORCHESTRA_NEMOCLAW_LIVE_SANDBOX")
    if not sandbox:
        pytest.skip("ORCHESTRA_NEMOCLAW_LIVE_SANDBOX is not set")
    cli = NemoClawCli(openshell=os.environ.get("OPENSHELL_BIN", "openshell"), dialect="modern")
    launcher = OpenShellSshLauncher(cli, ssh=os.environ.get("SSH_BIN", "ssh"))
    launches: list[str] = []

    def launch(name: str) -> list[str]:
        launches.append(name)
        return launcher(name)

    transport = PersistentTransport(launch)
    try:
        payload = json.dumps({"sandbox": sandbox, "tool": "orchestra_live_probe"})
        replies = [json.loads(transport.exchange(sandbox, payload, 30)) for _ in range(3)]
    finally:
        transport.close()
    assert [reply["error"] for reply in replies] == ["worker_tool_forbidden"] * 3
    assert launches == [sandbox]
