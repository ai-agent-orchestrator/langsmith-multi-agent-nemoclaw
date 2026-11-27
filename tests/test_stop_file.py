"""An operator stops a running process from outside it by creating the configured stop file."""
from __future__ import annotations

import os

import pytest

from orchestra.filters.base import Action, FilterContext, Stage
from orchestra.filters.chain import FilterChain
from orchestra.models.factory import ScriptedModelFactory
from orchestra.patterns.hierarchical_graph import build_hierarchical_app
from orchestra.runtime import Runtime
from orchestra.security.control import STOP_FILE_ENV, SecurityControl
from orchestra.security.decision import ReasonCode, from_chain_result
from orchestra.service import OrchestraService
from orchestra.simulation import load_scenario


@pytest.fixture
def stop_file(tmp_path, monkeypatch):
    path = tmp_path / "stop"
    monkeypatch.delenv("ORCHESTRA_KILL_SWITCH", raising=False)
    monkeypatch.setenv(STOP_FILE_ENV, str(path))
    return path


def test_the_file_stops_and_removing_it_releases(stop_file):
    control = SecurityControl()
    assert control.guard("agent") is None
    stop_file.touch()
    assert control.guard("agent") == "global_kill_switch"
    stop_file.unlink()
    assert control.guard("agent") is None


def test_without_the_setting_no_file_is_consulted(monkeypatch):
    monkeypatch.delenv("ORCHESTRA_KILL_SWITCH", raising=False)
    monkeypatch.delenv(STOP_FILE_ENV, raising=False)
    assert SecurityControl().guard("agent") is None
    monkeypatch.setenv(STOP_FILE_ENV, "")
    assert SecurityControl().guard("agent") is None


@pytest.mark.parametrize("kind", ["directory", "dangling-link", "empty-content", "off-content"])
def test_anything_at_the_path_stops(stop_file, kind):
    if kind == "directory":
        stop_file.mkdir()
    elif kind == "dangling-link":
        stop_file.symlink_to(stop_file.with_name("nowhere"))
    else:
        stop_file.write_text("" if kind == "empty-content" else "off")
    assert SecurityControl().guard("agent") == "global_kill_switch"


@pytest.mark.parametrize("value", ["stop", "./stop", "~/stop", "   "])
def test_a_relative_path_stops(monkeypatch, value):
    monkeypatch.setenv(STOP_FILE_ENV, value)
    assert SecurityControl().guard("agent") == "global_kill_switch"


def test_an_unusable_location_stops(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.write_text("x")
    for path in (tmp_path / "missing" / "stop", plain / "stop", tmp_path / ("n" * 5000)):
        monkeypatch.setenv(STOP_FILE_ENV, str(path))
        assert SecurityControl().guard("agent") == "global_kill_switch", path


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_location_stops(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setenv(STOP_FILE_ENV, str(locked / "stop"))
    locked.chmod(0)
    try:
        assert SecurityControl().guard("agent") == "global_kill_switch"
    finally:
        locked.chmod(0o700)


def test_a_status_error_stops(stop_file, monkeypatch):
    def broken(path):
        raise PermissionError

    monkeypatch.setattr("orchestra.security.control.os.lstat", broken)
    assert SecurityControl().guard("agent") == "global_kill_switch"


def test_the_filter_chain_reports_the_kill_switch_reason(stop_file):
    chain = FilterChain([], security_control=SecurityControl())
    stop_file.touch()
    for stage in (Stage.PRE_REQUEST, Stage.PRE_MODEL, Stage.PRE_TOOL):
        result = chain.run(FilterContext(stage, "x"))
        assert result.action == Action.BLOCK
        assert from_chain_result(result).reason_code == ReasonCode.KILL_SWITCH


def test_a_file_created_during_a_run_stops_the_next_call(stop_file):
    scenario = load_scenario("concert_swap")
    runtime = Runtime(security_mode="closed")
    app = build_hierarchical_app(scenario["org"], ScriptedModelFactory(scenario["script"], runtime.events),
                                 runtime=runtime)
    record = runtime.events.add

    def stop_after_first_model_call(kind, **fields):
        added = record(kind, **fields)
        if kind == "llm":
            stop_file.touch()
        return added

    runtime.events.add = stop_after_first_model_call  # type: ignore[method-assign]
    result = OrchestraService(app).ask(scenario["request"])
    assert result["blocked"] and result["reason"] == "global_kill_switch"
    assert len(runtime.events.of("llm")) == 1
    assert not runtime.events.of("tool")
