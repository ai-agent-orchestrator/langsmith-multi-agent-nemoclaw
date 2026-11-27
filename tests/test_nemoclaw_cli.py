"""CLI boundary regression checks require neither NemoClaw nor network."""
import subprocess
from unittest.mock import patch

import pytest

from orchestra.sandbox.nemoclaw.cli import NemoClawCli


@pytest.mark.parametrize("name", ["-help", "a--b", "a-", "../escape", "a;whoami", "A", "a" * 20, "a\n"])
def test_reject_sandbox_before_launch(name):
    with patch("orchestra.sandbox.nemoclaw.cli._run_bounded") as run:
        with pytest.raises(ValueError):
            NemoClawCli().run("status", sandbox=name)
        run.assert_not_called()


@pytest.mark.parametrize("name", ["a", "orchestra-payments", "a" * 19])
def test_valid_sandbox_and_dry_run(name):
    with patch("orchestra.sandbox.nemoclaw.cli._run_bounded") as run:
        result = NemoClawCli(dry_run=True).run("policy_get", sandbox=name)
        assert result.argv == ("openshell", "policy", "get", "--full", name)
        assert result.ok and result.dry_run
        run.assert_not_called()


def test_policy_replacement_and_arbitrary_exec_forbidden():
    cli = NemoClawCli()
    for action in ("policy_set", "exec", "connect", "onboard"):
        with pytest.raises(ValueError):
            cli.command(action, sandbox="sandbox")
    for preset in ("--from-file", "../policy", "a;echo", ""):
        with pytest.raises(ValueError):
            cli.command("policy_add", sandbox="sandbox", preset=preset)
    assert cli.command("policy_add", sandbox="sandbox", preset="orchestra-egress") == (
        "nemoclaw", "sandbox", "policy-add", "orchestra-egress")


def test_subprocess_boundary_and_private_output():
    with patch("orchestra.sandbox.nemoclaw.cli._run_bounded", return_value=subprocess.CompletedProcess([], 0, "private", "")) as run:
        result = NemoClawCli(timeout_s=3).run("status", sandbox="sandbox")
        assert result.stdout == "private"
        assert "private" not in repr(result)
        assert run.call_args.args == (["nemoclaw", "sandbox", "status"],)
        assert run.call_args.kwargs["timeout"] == 3
        assert run.call_args.kwargs["input_text"] is None


@pytest.mark.parametrize("failure,error", [
    (FileNotFoundError("secret"), "cli_unavailable"),
    (subprocess.TimeoutExpired("secret", 1, output="secret"), "cli_timeout"),
])
def test_fail_closed_without_exception_leak(failure, error):
    with patch("orchestra.sandbox.nemoclaw.cli._run_bounded", side_effect=failure):
        result = NemoClawCli().run("help")
    assert not result.ok and result.error == error
    assert "secret" not in repr(result)


def test_nonzero_output_is_not_exposed():
    with patch("orchestra.sandbox.nemoclaw.cli._run_bounded", return_value=subprocess.CompletedProcess([], 1, "secret", "secret")):
        result = NemoClawCli().run("help")
    assert not result.ok and result.error == "cli_failed" and result.stdout == ""


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        NemoClawCli(timeout_s=timeout)
