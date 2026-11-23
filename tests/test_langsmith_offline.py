"""Real SDK wire checks use only a loopback receiver, never hosted LangSmith."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from orchestra.observability.client import make_client
from orchestra.observability.evaluators import payment_safety, trajectory_match, require_payment_safety
from orchestra.observability.safe import safe_call


@pytest.fixture
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, b""))
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_PATCH = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_masked_sdk_wire_has_no_raw_pii_pci(receiver):
    endpoint, requests = receiver
    client = make_client("masked", api_url=endpoint, api_key="local-test", auto_batch_tracing=False)
    client.create_run(name="test", inputs={"text": "900101-1234567 private@example.com 1234-5678-1234-5678 cvc=123 4111111111111111 **** **** **** 4242"},
                      run_type="chain", id=uuid4(), extra={"metadata": {"thread_id": "t"}})
    sent = b"".join(body for _, body in requests)
    assert sent and b"REDACTED" in sent
    for raw in (b"900101-1234567", b"private@example.com", b"1234-5678-1234-5678", b"cvc=123", b"4111111111111111", b"**** **** **** 4242"):
        assert raw not in sent


def test_closed_sdk_calls_send_zero_bytes_even_with_client_requested(receiver):
    endpoint, requests = receiver
    client = make_client("closed", api_url=endpoint, api_key="local-test")
    client.create_run(name="no-send", inputs={"text": "private@example.com"}, run_type="chain")
    client.create_feedback(uuid4(), "score", comment="private@example.com")
    assert requests == []


def test_closed_graph_ignores_ambient_enabled_tracing(receiver, monkeypatch):
    from orchestra.observability.showcase import trace_demo

    endpoint, requests = receiver
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", endpoint)
    result = trace_demo("closed", endpoint)
    assert result["headers"] == {} and result["answer"]["interrupt"] is None
    assert requests == []


def test_safe_call_never_logs_exception_contents(caplog):
    def broken():
        raise RuntimeError("private@example.com secret=raw-token")

    assert safe_call(broken, default="fallback") == "fallback"
    assert "private@example.com" not in caplog.text and "raw-token" not in caplog.text


def test_payment_safety_requires_matching_one_shot_approval():
    tool = {"kind": "tool", "data": {"tool": "capture_payment", "args": {"intent_id": "one"}}}
    approval = {"kind": "approval", "data": {"tool": "capture_payment", "approved": True,
                                             "args": {"intent_id": "one"}}}
    assert payment_safety([tool]) == 0
    assert payment_safety([approval, tool]) == 1
    assert payment_safety([approval, tool, tool]) == 0
    with pytest.raises(AssertionError):
        require_payment_safety([tool])


def test_all_current_scenarios_pass_payment_safety_and_trajectory():
    from orchestra.simulation import list_scenarios, run_scenario

    for name in list_scenarios():
        result = run_scenario(name)
        require_payment_safety(result.events.events)
        expected = result.scenario.get("expect", {}).get("agents_in_order", [])
        roles = [e.data["role"] for e in result.events.of("llm")]
        assert trajectory_match(roles, expected) == 1


def test_showcase_offline_has_24_features_and_writes_masked_artifacts(tmp_path, monkeypatch):
    import socket

    from orchestra.observability.showcase import main

    attempted = []
    original = socket.socket.connect

    def connect(sock, address):
        if address[0] not in {"127.0.0.1", "::1"}:
            attempted.append(address)
            raise OSError("offline mode forbids external sockets")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    assert main(["all", "--offline", "--output", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["features"]) == 24
    assert (tmp_path / "report.html").exists()
    assert attempted == []
    creates = [json.loads(item["body"]) for item in manifest["results"]["wire"]
               if item["method"] == "POST" and item["path"] == "/runs"]
    names = {item["name"] for item in creates}
    assert "distributed-worker" in names and any(name.startswith("filter:") for name in names)
    worker = next(item for item in creates if item["name"] == "distributed-worker")
    assert worker["parent_run_id"] in {item["id"] for item in creates}


def test_sdk_failure_and_share_policy_do_not_expose_or_execute(caplog):
    from orchestra.observability.client import SafeClient

    class Broken:
        shared = False

        def create_feedback(self, *args, **kwargs):
            raise RuntimeError("private@example.com")

        def share_run(self, *args):
            self.shared = True

        def list_runs(self):
            yield "partial"
            raise RuntimeError("private@example.com")

    raw = Broken()
    client = SafeClient(raw, "masked")
    assert client.create_feedback("id", "key") is None
    assert client.list_runs() == []
    assert client.share_run("id") is None and not raw.shared
    assert "private@example.com" not in caplog.text
    assert SafeClient(raw, "closed").share_run("id") is None and not raw.shared


def test_prompt_hub_uses_local_fallback_and_caches_successes():
    from orchestra.observability.client import SafeClient
    from orchestra.observability.prompts import PromptHub

    class Remote:
        calls = 0

        def pull_prompt(self, name):
            self.calls += 1
            if name == "missing":
                raise RuntimeError("unavailable")
            return type("Template", (), {"format_prompt": lambda self: self, "to_string": lambda self: "remote"})()

    raw = Remote()
    hub = PromptHub(SafeClient(raw, "masked"))
    resolver = hub.resolver({"missing": "local", "present": "fallback"})
    assert resolver("hub:missing") == "local"
    assert resolver("hub:present") == resolver("hub:present") == "remote"
    assert raw.calls == 2


def test_all_mutation_payloads_are_masked_and_unsupported_apis_degrade():
    from orchestra.observability.client import SafeClient
    from orchestra.observability.offline import OfflineClient
    from orchestra.observability.feedback import feedback
    from orchestra.observability.datasets import push_dataset
    from orchestra.observability.online import setup

    raw = OfflineClient()
    client = SafeClient(raw, "masked")
    feedback(client, str(uuid4()), "quality", 1, "private@example.com")
    push_dataset(client, [{"inputs": {"text": "private@example.com"}, "outputs": {}}])
    assert "private@example.com" not in json.dumps(raw.calls, default=str)
    assert setup(client, "project")["status"] == "unavailable"


def test_price_estimation_requires_explicit_model_prices(monkeypatch):
    from orchestra.observability.report import estimate_cost

    monkeypatch.setenv("ORCHESTRA_PRICE_TABLE", '{"configured": {"in": 2, "out": 4}}')
    assert estimate_cost("missing", 100, 100) is None
    assert estimate_cost("configured", 1_000_000, 1_000_000) == 6


def test_builder_hub_resolution_uses_explicit_injection():
    from orchestra.models.factory import ScriptedModelFactory
    from orchestra.org import load_org
    from orchestra.patterns.hierarchical_graph import build_hierarchical_app

    org = load_org("orgs/travel_legacy.yaml")
    org.teams[org.root].prompt = "hub:director"
    with pytest.raises(ValueError, match="prompt_resolver"):
        build_hierarchical_app(org, ScriptedModelFactory({}))
    seen = []

    def resolve(declaration):
        seen.append(declaration)
        return "Local fallback director prompt"

    app = build_hierarchical_app(org, ScriptedModelFactory({}), services={"prompt_resolver": resolve})
    assert app.graph and seen == ["hub:director"]


def test_judge_adapters_use_original_prompts_without_implicit_models():
    from orchestra.observability.evaluators import llm_judge
    from orchestra.observability.pairwise import compare_targets

    assert llm_judge({}, {})["status"] == "unavailable"
    captured = []

    def judge(system, data):
        captured.append((system, data))
        return {"score": 1}

    assert llm_judge({}, {"text": "private@example.com"}, judge) == {"score": 1}
    assert "# Grader Agent" in captured[-1][0]
    assert "private@example.com" not in str(captured[-1][1])
    assert compare_targets("input", lambda value: value, lambda value: value, judge) == {"score": 1}
    assert "Comparator" in captured[-1][0]


def test_structured_secret_fields_are_redacted_but_idempotency_is_preserved():
    from orchestra.observability.masking import masker

    output = masker({"token": "opaque-value", "api_key": "opaque-key", "idempotency_key": "idem_123", "total_tokens": 5})
    assert output == {"token": "[REDACTED_SECRET]", "api_key": "[REDACTED_SECRET]", "idempotency_key": "idem_123", "total_tokens": 5}
