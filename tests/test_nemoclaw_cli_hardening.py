"""Defenses against observed NemoClaw CLI failure modes (v0.0.145, WSL2); no NemoClaw needed."""
import fcntl
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestra.sandbox.nemoclaw import cli as cli_module
from orchestra.sandbox.nemoclaw.cli import NemoClawCli, _run_bounded
from orchestra.sandbox.nemoclaw.live import parse_policy, split_policy_output

FIXTURES = Path(__file__).parent / "fixtures" / "nemoclaw"


def test_real_openshell_base_output_parses_with_banner():
    output = (FIXTURES / "openshell-0.0.116-policy-get-base.txt").read_text()
    meta, _ = split_policy_output(output)
    assert meta["Version"] == "6" and meta["Status"] == "Effective" and meta["Source"] == "sandbox"
    policy = parse_policy(output)
    keys = set(policy["network_policies"])
    # Built-in presets appear under their own names. --base omits provider-composed _provider_* entries,
    # which only `nemoclaw <sb> status` / `openshell policy get --full` show.
    assert {"pypi", "huggingface", "brew-balanced", "managed_inference"} <= keys
    assert not any(key.startswith("_provider_") for key in keys)
    # Binary globs are accepted by the live engine (REALITY ⚠️3).
    pypi_bins = [b["path"] for b in policy["network_policies"]["pypi"]["binaries"]]
    assert "/usr/bin/python3*" in pypi_bins


@pytest.mark.parametrize("output", [
    "Unexpected banner\n---\nnetwork_policies: {}\n",
    "Version: 6\nVersion: 7\n---\nnetwork_policies: {}\n",
    "Version: 6\n---\nnetwork_policies: {}\n---\nnetwork_policies: {}\n",
    "---\nnetwork_policies: {}\n",
])
def test_unknown_or_ambiguous_banners_fail_closed(output):
    with pytest.raises(ValueError):
        parse_policy(output)


def test_plain_yaml_still_parses():
    assert parse_policy("network_policies:\n  a: {endpoints: []}\n")["network_policies"] == {"a": {"endpoints": []}}


def test_policy_mutation_is_non_interactive_and_preview_is_dry_run(tmp_path):
    preset = tmp_path / "p.yaml"
    preset.write_text("preset: {name: p}\n")
    cli = NemoClawCli(preset_root=tmp_path)
    assert cli.command("policy_add_file", sandbox="box", preset=str(preset))[-1] == "--yes"
    assert cli.command("policy_add_file_preview", sandbox="box", preset=str(preset))[-1] == "--dry-run"
    assert cli.command("status_json", sandbox="box") == ("nemoclaw", "box", "status", "--json")


def test_host_lock_contention_is_classified_without_leaking_stderr():
    stderr = "Error: Failed to acquire lock on /home/secret/.nemoclaw-portable-host.lock after 120 retries."
    with patch.object(cli_module, "_run_bounded", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
        result = NemoClawCli().run("policy_list", sandbox="box")
    assert not result.ok and result.error == "nemoclaw_host_lock_busy"
    assert "secret" not in repr(result) and result.stdout == ""


def test_timeout_kills_whole_process_group():
    marker = f"31.{uuid.uuid4().int % 10_000:04d}"
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded(["bash", "-c", f"sleep {marker} & sleep {marker}; wait"], timeout=0.5, input_text=None)
    time.sleep(0.2)
    leftover = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert leftover.stdout.strip() == ""


def test_our_calls_are_single_flight(tmp_path, monkeypatch):
    lock = tmp_path / "cli.lock"
    monkeypatch.setattr(cli_module, "_LOCK_PATH", lock)
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with patch.object(cli_module, "_run_bounded") as run:
            started = time.monotonic()
            result = NemoClawCli(timeout_s=0.3).run("status", sandbox="box")
        assert result.error == "cli_busy" and time.monotonic() - started < 2
        run.assert_not_called()
    # openshell reads do not take the nemoclaw lock.
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with patch.object(cli_module, "_run_bounded", return_value=subprocess.CompletedProcess([], 0, "ok", "")):
            assert NemoClawCli(timeout_s=0.3).run("policy_get", sandbox="box").ok


def test_telemetry_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRA_NEMOCLAW_TELEMETRY", raising=False)
    monkeypatch.delenv("NEMOCLAW_DISABLE_TELEMETRY", raising=False)
    with patch.object(cli_module, "_run_bounded", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        NemoClawCli().run("status", sandbox="box")
    assert run.call_args.kwargs["env"]["NEMOCLAW_DISABLE_TELEMETRY"] == "1"
    monkeypatch.setenv("ORCHESTRA_NEMOCLAW_TELEMETRY", "1")
    assert "NEMOCLAW_DISABLE_TELEMETRY" not in cli_module._child_env()
