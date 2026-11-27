"""Optional CPU and memory limits for the worker sandbox: only given, well-formed values reach OpenShell."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "python:3.11-slim@sha256:" + "a" * 64

_spec = importlib.util.spec_from_file_location("nemoclaw_worker_sandbox", ROOT / "scripts" / "nemoclaw_worker_sandbox.py")
assert _spec is not None and _spec.loader is not None
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def command(**limits: str) -> list[str]:
    return setup.create_command("openshell", Path("context"), Path("policy.yaml"), **limits)


def test_no_limit_is_requested_unless_given():
    assert not {"--cpu", "--memory"} & set(command())


def test_limits_go_before_the_sandbox_command():
    argv = command(cpu="500m", memory="1Gi")
    assert argv[argv.index("--cpu") + 1] == "500m"
    assert argv[argv.index("--memory") + 1] == "1Gi"
    assert argv.index("--memory") < argv.index("--")
    assert argv[argv.index("--"):] == ["--", "sleep", "infinity"]
    assert "--memory" not in command(cpu="2")


@pytest.mark.parametrize("value", ["1", "2", "0.5", "500m", "1.250", "100m"])
def test_cpu_values_openshell_accepts(value):
    assert setup.cpu_quantity(value) == value


@pytest.mark.parametrize("value", ["", "0", "0.0", "0m", "-1", "1.5m", "m", "inf", "nan", "1e3", " 1", "1;id",
                                   "--policy", "1\n"])
def test_bad_cpu_values_are_refused(value):
    with pytest.raises(argparse.ArgumentTypeError):
        setup.cpu_quantity(value)


@pytest.mark.parametrize("value", ["512Mi", "1Gi", "8G", "1048576Ki", "10M"])
def test_memory_values_with_a_unit(value):
    assert setup.memory_quantity(value) == value


@pytest.mark.parametrize("value", ["", "4", "4096", "0Gi", "00M", "1.5Gi", "-1Gi", "1GB", "1gi", "Gi", "1Gi;id",
                                   "1Gi\n"])
def test_bad_memory_values_are_refused(value):
    with pytest.raises(argparse.ArgumentTypeError):
        setup.memory_quantity(value)


def test_a_dry_run_shows_the_limits_and_creates_nothing(monkeypatch, capsys):
    real = subprocess.run
    seen: list[list[str]] = []

    def run(argv, **options):
        seen.append(list(argv))
        assert argv[0] == "git", "a dry run may only list the files to stage"
        return real(argv, **options)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.main(["--python-base", BASE, "--cpu", "1", "--memory", "2Gi"]) == 0
    printed = capsys.readouterr().out
    assert "--cpu 1 --memory 2Gi" in printed and "limits: cpu=1 memory=2Gi" in printed
    assert "DRY RUN" in printed and len(seen) == 1


def test_limits_cannot_be_given_with_probe_only(capsys):
    with pytest.raises(SystemExit):
        setup.main(["--probe-only", "--memory", "1Gi"])
    assert "only when a sandbox is created" in capsys.readouterr().err
