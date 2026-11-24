"""C14 reuses C13 exports and masks untrusted imported diagnostics before UI delivery."""
import importlib
import importlib.util
import json
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def playground():
    spec = importlib.util.spec_from_file_location("playground_c14", ROOT / "examples/playground.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(playground):
    with ThreadingHTTPServer(("127.0.0.1", 0), playground.Handler) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{http.server_port}"
        http.shutdown()
        thread.join(timeout=5)


def post(server, path, body):
    request = Request(server + path, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def test_simulation_api_uses_c13_trace_export(server):
    result = post(server, "/api/simulation", {"scenario": "jeju_trip_payment"})
    assert result["scenario"] == "jeju_trip_payment" and result["errors"] == []
    assert {"filter", "approval_request", "approval", "sandbox_route"} <= {r["kind"] for r in result["trace"]}
    assert len([r for r in result["trace"] if r["kind"] == "approval_request"]) == 1


def test_import_masks_raw_pii_and_drops_non_trace_payload(server):
    raw = {"scenario": "900101-1234567", "trace": [
        {"seq": 1, "kind": "filter", "stage": "pre_tool", "filter": "mask", "action": "modify",
         "reason": "4111-1111-1111-1111", "tool": "<img src=x onerror=alert(1)>"}],
           "events": [{"args": {"secret": "do-not-show"}}]}
    result = post(server, "/api/trace/import", raw)
    assert "900101-1234567" not in str(result) and "4111-1111-1111-1111" not in str(result)
    assert "events" not in result and "do-not-show" not in str(result)
    assert result["trace"][0]["tool"] == "<img src=x onerror=alert(1)>"


@pytest.mark.parametrize("bad", [{}, {"trace": "bad"}, {"trace": [{"seq": -1, "kind": "filter"}]},
                                {"trace": [{"seq": 1, "kind": "unknown"}]},
                                {"trace": [{"seq": 4111111111111111, "kind": "blocked"}]},
                                {"trace": [{"seq": 1, "kind": "filter", "reason": 4111111111111111}]},
                                {"trace": [{"seq": 2, "kind": "blocked"}, {"seq": 1, "kind": "blocked"}]}])
def test_bad_trace_import_is_rejected_without_reflecting_payload(server, bad):
    with pytest.raises(HTTPError) as error:
        post(server, "/api/trace/import", bad)
    assert error.value.code == 400
    assert json.load(error.value) == {"error": "invalid_trace_export"}


def test_scenario_endpoint_rejects_paths(server):
    with pytest.raises(HTTPError) as error:
        post(server, "/api/simulation", {"scenario": "../../.env"})
    assert error.value.code == 400
    assert json.load(error.value) == {"error": "invalid_scenario"}


def test_shared_export_preserves_existing_cli_contract():
    from orchestra.simulation import run_scenario
    from orchestra.simulation_trace import export_simulation, trace_records

    result = run_scenario("pii_bulk_blocked", trace=True)
    exported = export_simulation(result, trace=True)
    assert exported["trace"] == trace_records(result.events)
    assert exported["errors"] == [] and exported["final"]["blocked"]
    assert "trace" not in export_simulation(result)


def test_langgraph_entry_is_importable_and_builds_without_external_services(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LEDGER", "memory")
    config = json.loads((ROOT / "langgraph.json").read_text())
    module, symbol = config["graphs"]["orchestra"].split(":")
    factory = getattr(importlib.import_module(module), symbol)
    graph = factory()
    assert graph.get_graph().nodes
