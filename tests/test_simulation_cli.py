"""C13 CLI contracts: isolated, offline, masked trace exports, and existing expectations."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "examples/simulate_hierarchical.py"


def cli(*args):
    return subprocess.run([sys.executable, "-I", str(CLI), *map(str, args)], cwd=ROOT,
                          capture_output=True, encoding="utf-8", shell=False, timeout=60)


@pytest.mark.parametrize("pattern", ["graph", "tools"])
def test_pattern_and_org_combination_meets_expectations(tmp_path, pattern):
    output = tmp_path / "simulation.json"
    result = cli("--org", ROOT / "orgs/travel_payment.yaml", "--pattern", pattern,
                 "--trace", "--mode", "graph", "--json", output)
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["errors"] == [] and data["trace"]
    assert "[TRACE]" in result.stdout and "PASS" in result.stdout
    assert [x["seq"] for x in data["trace"]] == sorted(x["seq"] for x in data["trace"])
    assert {"approval_request", "approval", "sandbox_route", "sandbox", "filter"} <= {x["kind"] for x in data["trace"]}
    assert {"stage", "filter", "action", "reason"} <= next(x for x in data["trace"] if x["kind"] == "filter").keys()
    kinds = [x["kind"] for x in data["trace"]]
    assert kinds.index("approval_request") < kinds.index("approval")


def test_missing_org_is_one_line_error_without_graph_execution(tmp_path):
    path = tmp_path / "missing.yaml"
    result = cli("--org", path)
    assert result.returncode == 2
    assert result.stdout == "" and len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr and "org" in result.stderr.lower()


@pytest.mark.parametrize("scenario", ["jeju_trip_payment", "jeju_payment_rejected", "payment_over_limit",
                                      "pii_bulk_blocked", "sandbox_egress_denied"])
def test_five_original_scenarios_pass_with_trace(scenario):
    result = cli("--scenario", scenario, "--trace")
    assert result.returncode == 0 and "PASS" in result.stdout, result.stderr + result.stdout


def test_trace_masks_entire_console_and_json_even_in_off_mode(tmp_path):
    import yaml
    from orchestra.simulation import load_scenario

    pii = "900101-1234567"
    pci = "4111-1111-1111-1111"
    scenario = load_scenario("jeju_trip_payment")
    scenario["request"] += f" 연락처 {pii} {pci}"
    scenario["script"]["travel_director"][0]["content"] = f"연락처 {pii} {pci}"
    source = tmp_path / "pii.yaml"
    source.write_text(yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8")
    output = tmp_path / "pii.json"
    result = cli("--scenario", source, "--trace", "--security-mode", "off", "--json", output)
    assert result.returncode == 0, result.stderr
    payload = result.stdout + result.stderr + output.read_text(encoding="utf-8")
    assert pii not in payload and pci not in payload
    assert "REDACTED" in payload


def test_default_output_and_json_do_not_opt_into_trace(tmp_path):
    output = tmp_path / "default.json"
    result = cli("--json", output)
    assert result.returncode == 0 and "[TRACE]" not in result.stdout
    assert "trace" not in json.loads(output.read_text(encoding="utf-8"))

@pytest.mark.parametrize("scenario", ["jeju_trip_payment", "jeju_payment_rejected", "payment_over_limit",
                                      "pii_bulk_blocked", "sandbox_egress_denied"])
def test_graph_and_tools_share_all_observable_safety_behavior(scenario):
    from orchestra.simulation import run_scenario, check_expect
    from orchestra.simulation_trace import trace_records

    graph = run_scenario(scenario, pattern="graph", trace=True)
    tools = run_scenario(scenario, pattern="tools", trace=True)
    assert check_expect(graph) == check_expect(tools) == []
    assert [e.data["tool"] for e in graph.events.of("tool")] == [e.data["tool"] for e in tools.events.of("tool")]
    assert graph.final == tools.final
    assert trace_records(graph.events) == trace_records(tools.events)
    assert len(graph.interrupts) == len(tools.interrupts)
    assert graph.decisions == tools.decisions
    for result in (graph, tools):
        assert sum(e.data["tool"] == "capture_payment" for e in result.events.of("tool")) <= 1
    assert len(graph.events.of("approval_request")) == len(tools.events.of("approval_request"))


def test_tools_selector_uses_the_requested_facade(monkeypatch):
    from orchestra.patterns import hierarchical_tools
    from orchestra.simulation import run_scenario

    called = []
    original = hierarchical_tools.build_tools_app

    def build(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(hierarchical_tools, "build_tools_app", build)
    run_scenario("pii_bulk_blocked", pattern="tools")
    assert called == [True]


def test_unknown_pattern_is_rejected_by_argparse():
    result = cli("--pattern", "unsupported")
    assert result.returncode == 2 and "invalid choice" in result.stderr
    assert "Traceback" not in result.stderr


def test_trace_has_escaped_single_line_records_and_no_raw_sensitive_values():
    from orchestra.runtime import EventLog
    from orchestra.simulation_trace import trace_records, render_trace

    events = EventLog(capture_trace=True)
    events.add("filter", stage="pre_tool", filter="example", action="BLOCK",
               reason="900101-1234567\n4111111111111111", tool="read", agent="worker")
    events.add("approval_request", tool="capture", ask="카드 4111 1111 1111 1111\n승인?")
    records = trace_records(events)
    output = render_trace(records)
    assert len(output.splitlines()) == 2
    assert "900101-1234567" not in output and "4111111111111111" not in output
    assert "4111 1111 1111 1111" not in output
    assert "REDACTED" in output


def test_org_override_is_used_without_mutating_original_scenario():
    from orchestra.simulation import load_scenario, run_scenario, check_expect

    scenario = load_scenario("jeju_trip_payment")
    scenario["org"] = "does-not-exist.yaml"
    result = run_scenario(scenario, org=ROOT / "orgs/travel_payment.yaml", trace=True)
    assert check_expect(result) == []
    assert scenario["org"] == "does-not-exist.yaml"
    assert result.app.org.name == "travel_payment"


def test_explicit_graph_keeps_default_console_behavior():
    default, graph = cli(), cli("--pattern", "graph")
    assert default.returncode == graph.returncode == 0
    assert default.stdout == graph.stdout


def test_directory_is_not_accepted_as_org(tmp_path):
    result = cli("--org", tmp_path)
    assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
