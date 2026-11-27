"""Exercise actual OS pipes/deadlines with Python as a fake external executable."""
import subprocess
import sys
from unittest.mock import patch

import pytest

from orchestra.sandbox.nemoclaw.cli import _run_bounded
from orchestra import hitl


def test_real_subprocess_stdin_roundtrip_and_stderr_drain():
    result = _run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('diagnostic'); print(sys.stdin.read())"],
                          timeout=5, input_text="request")
    assert result.returncode == 0 and result.stdout.strip() == "request" and result.stderr == ""


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_stops_child(stream):
    with pytest.raises(ValueError):
        _run_bounded([sys.executable, "-c", f"import sys; sys.{stream}.write('a' * 2000000)"], timeout=5, input_text=None)


def test_deadline_terminates_child():
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1, input_text=None)


@pytest.mark.parametrize("decision", ["false", "yes", 1, {}, {"approved": "false"}, {"approved": 1}])
def test_only_explicit_boolean_human_approval_is_accepted(decision):
    with patch("langgraph.types.interrupt", return_value=decision):
        assert hitl.request({}) is False
