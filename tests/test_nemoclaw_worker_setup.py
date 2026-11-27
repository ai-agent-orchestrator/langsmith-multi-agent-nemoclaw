"""Worker sandbox preparation: per-sandbox keys, deny-all policy, image inputs and the setup script."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from orchestra.sandbox.base import ExecRequest
from orchestra.sandbox.nemoclaw.bootstrap import make_runner
from orchestra.sandbox.nemoclaw.live import drift_errors, parse_policy
from orchestra.security.capability import CapabilityAuthority, SandboxKeyedAuthority, derive_sandbox_key

ROOT = Path(__file__).resolve().parents[1]
MASTER = b"m" * 32
BASE = "python:3.11-slim@sha256:" + "a" * 64

_spec = importlib.util.spec_from_file_location("nemoclaw_worker_sandbox", ROOT / "scripts" / "nemoclaw_worker_sandbox.py")
assert _spec is not None and _spec.loader is not None
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def _request(sandbox: str) -> dict:
    return ExecRequest(sandbox=sandbox, tool="read", args={}, agent="reader").model_dump()


def test_each_sandbox_gets_its_own_key():
    one, two = derive_sandbox_key(MASTER, "box-one"), derive_sandbox_key(MASTER, "box-two")
    assert one == derive_sandbox_key(MASTER, "box-one")
    assert one != two and len(one) == 64 and MASTER not in one
    with pytest.raises(ValueError):
        derive_sandbox_key(b"short", "box-one")


def test_host_tokens_verify_only_with_the_key_of_their_sandbox():
    grants = {"reader": {"read"}}
    host = SandboxKeyedAuthority(MASTER, grants)
    request = _request("box-one")
    token = host.issue(request)
    assert CapabilityAuthority(derive_sandbox_key(MASTER, "box-one"), grants).verify(request, token)
    assert host.verify(request, token)
    assert not CapabilityAuthority(derive_sandbox_key(MASTER, "box-two"), grants).verify(request, token)
    assert not CapabilityAuthority(MASTER, grants).verify(request, token)


def test_a_leaked_sandbox_key_cannot_sign_for_another_sandbox():
    grants = {"reader": {"read"}}
    leaked = CapabilityAuthority(derive_sandbox_key(MASTER, "box-one"), grants)
    forged = leaked.issue(_request("box-two"))
    assert not CapabilityAuthority(derive_sandbox_key(MASTER, "box-two"), grants).verify(_request("box-two"), forged)


def test_keyed_authority_needs_a_sandbox_name():
    host = SandboxKeyedAuthority(MASTER, {"reader": {"read"}})
    request = _request("box-one") | {"sandbox": ""}
    with pytest.raises(ValueError, match="capability_sandbox_missing"):
        host.issue(request)
    assert not host.verify(request, "a.b")


def test_bootstrap_signs_with_per_sandbox_keys(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_bytes(MASTER)
    monkeypatch.setenv("ORCHESTRA_CAPABILITY_KEY_FILE", str(key))
    monkeypatch.delenv("NEMOCLAW_REVIEWED_BASELINE", raising=False)
    assert isinstance(make_runner([], {}).authority, SandboxKeyedAuthority)


BANNER = "Version:      1\nHash:         abc\nStatus:       Loaded\n---\n"
NO_NETWORK = "version: 1\nfilesystem_policy:\n  include_workdir: true\nprocess:\n  run_as_user: sandbox\n"


def test_openshell_output_without_network_rules_means_none():
    assert parse_policy(BANNER + NO_NETWORK)["network_policies"] == {}
    assert drift_errors(BANNER + NO_NETWORK, {}, closed=True) == []
    wanted = {"sample": {"network_policies": {"read": {"endpoints": []}}}}
    assert "policy_keys_differ" in drift_errors(BANNER + NO_NETWORK, wanted, closed=True)


@pytest.mark.parametrize("output", [
    NO_NETWORK,  # no OpenShell banner
    BANNER + "version: 2\nfilesystem_policy: {}\n",
    BANNER + "version: true\nfilesystem_policy: {}\n",
    BANNER + "version: 1\n",
    BANNER + "filesystem_policy: {}\n",
    BANNER + NO_NETWORK + "networkPolicies: {a: {}}\n",
    BANNER + NO_NETWORK + "network_policies:\n",
    BANNER + NO_NETWORK + "network_policies: []\n",
])
def test_other_documents_without_network_rules_fail_closed(output):
    with pytest.raises(ValueError):
        parse_policy(output)


def test_worker_policy_denies_network_and_keeps_code_read_only():
    text = (ROOT / "nemoclaw" / "policy-worker.yaml").read_text(encoding="utf-8")
    policy = yaml.safe_load(text)
    assert policy["network_policies"] == {}
    filesystem = policy["filesystem_policy"]
    assert "/opt/orchestra" in filesystem["read_only"]
    assert set(filesystem["read_write"]) == {"/sandbox", "/tmp", "/dev/null"}
    assert policy["landlock"] == {"compatibility": "hard_requirement"}
    assert policy["process"] == {"run_as_user": "sandbox", "run_as_group": "sandbox"}
    assert drift_errors(text, {}, closed=True) == []


def test_image_settings_agree_with_the_setup_script():
    text = (ROOT / "nemoclaw" / "Dockerfile.worker").read_text(encoding="utf-8")
    profile = re.search(r"printf 'export %s\\n' \\\n(.*?)> /etc/profile\.d/orchestra-worker\.sh", text, re.S)
    image_env = re.search(r"\nENV (ORCHESTRA_SECURITY_MODE.*?)\n#", text, re.S)
    assert profile and image_env

    def pairs(block: str) -> dict[str, str]:
        return dict(re.findall(r"([A-Z_]+)=(\S+)", block))

    assert pairs(profile[1]) == pairs(image_env[1])
    assert pairs(profile[1])["ORCHESTRA_WORKER_SANDBOX"] == setup.SANDBOX
    assert pairs(profile[1])["ORCHESTRA_WORKER_CAPABILITY_KEY_FILE"] == setup.KEY_PATH
    assert "iproute2" in text
    assert re.findall(r"^(?:USER|WORKDIR) .*$", text, re.M)[-2:] == ["WORKDIR /sandbox", "USER sandbox"]


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    tracked = {"nemoclaw/Dockerfile.worker": "ARG X\nARG PYTHON_BASE\nFROM ${PYTHON_BASE}\n",
               "nemoclaw/requirements-worker.lock": "x==1\n", "orchestra/__init__.py": "",
               "domains/a.py": "", "orgs/a.yaml": "", "third_party/a.md": ""}
    for name, text in tracked.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, timeout=30)
    for secret in ("orchestra/.env", "orchestra/capability.key", ".env"):
        (root / secret).write_text("secret", encoding="utf-8")
    return root


def test_staging_copies_only_tracked_inputs_and_pins_the_base(tmp_path):
    out = tmp_path / "out"
    assert setup.stage_context(out, BASE, root=_repo(tmp_path)) == 6
    copied = sorted(path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file())
    assert copied == ["Dockerfile", "domains/a.py", "nemoclaw/requirements-worker.lock", "orchestra/__init__.py",
                      "orgs/a.yaml", "third_party/a.md"]
    assert f"\nARG PYTHON_BASE={BASE}\nFROM " in (out / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("base", ["python:3.11-slim", "python@sha256:abc", "x@sha256:" + "a" * 64 + "\nRUN id",
                                  "--flag@sha256:" + "a" * 64])
def test_staging_requires_a_digest_pinned_base(tmp_path, base):
    with pytest.raises(ValueError):
        setup.stage_context(tmp_path / "out", base, root=ROOT)


def test_staging_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        setup.stage_context(tmp_path, BASE, root=ROOT)


def test_create_command_is_fixed(tmp_path):
    command = setup.create_command("openshell", tmp_path / "ctx", tmp_path / "policy.yaml")
    assert command == ["openshell", "sandbox", "create", "--name", "orchestra-code", "--from", str(tmp_path / "ctx"),
                       "--policy", str(tmp_path / "policy.yaml"), "--no-auto-providers", "--no-tty", "--detach",
                       "--", "sleep", "infinity"]


def test_master_key_is_created_private_and_reused(tmp_path):
    path = tmp_path / "config" / "capability.key"
    key = setup.load_or_create_master_key(path)
    assert len(key) == 64 and path.stat().st_mode & 0o777 == 0o600
    assert setup.load_or_create_master_key(path) == key
    path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        setup.load_or_create_master_key(path)
    path.chmod(0o600)
    path.write_bytes(b"short")
    with pytest.raises(ValueError, match="too short"):
        setup.load_or_create_master_key(path)


def test_dry_run_creates_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENSHELL_BIN", str(tmp_path / "no-such-openshell"))
    key = tmp_path / "capability.key"
    assert setup.main(["--python-base", BASE, "--key-file", str(key)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not key.exists()


class _Launcher:
    """Stands in for ssh: the 'remote command' stores stdin, after failing a set number of times."""

    def __init__(self, tmp_path: Path, failures: int) -> None:
        self.target, self.failures, self.argvs = tmp_path / "received", failures, []

    def argv(self, sandbox: str, remote_command: str) -> list[str]:
        self.argvs.append((sandbox, remote_command))
        if len(self.argvs) <= self.failures:
            raise RuntimeError("ssh_config_unavailable")
        code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        return [sys.executable, "-c", code, str(self.target)]


def test_key_is_sent_on_stdin_after_the_sandbox_is_ready(tmp_path):
    launcher = _Launcher(tmp_path, failures=2)
    secret = derive_sandbox_key(MASTER, setup.SANDBOX)
    assert setup.install_key(launcher, secret, wait_s=30, pause_s=0)
    assert launcher.target.read_bytes() == secret
    assert len(launcher.argvs) == 3
    assert all(secret.decode() not in command for _, command in launcher.argvs)
    assert launcher.argvs[0][1].startswith("umask 077 && cat > /sandbox/state/capability.key.new")


def test_key_install_gives_up(tmp_path):
    launcher = _Launcher(tmp_path, failures=10**6)
    assert not setup.install_key(launcher, b"k" * 64, wait_s=0, pause_s=0)
    assert not launcher.target.exists()
