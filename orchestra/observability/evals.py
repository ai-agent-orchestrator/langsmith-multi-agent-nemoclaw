"""Offline deterministic evaluation and an optional guarded SDK experiment."""
from typing import Any

from langsmith import evaluate

from ..simulation import list_scenarios, run_scenario
from .evaluators import payment_safety, require_payment_safety, trajectory_match, security_block, pass_rate
from .safe import safe_call
from .tracing import tracing_scope
from .client import make_client
from .offline import capture_server


def evaluate_scenarios() -> list[dict]:
    """Run every real scripted graph and enforce the payment gate outside safe_call."""
    rows = []
    for name in list_scenarios():
        result = run_scenario(name)
        require_payment_safety(result.events.events)
        expected = result.scenario.get("expect", {})
        rows.append({"scenario_id": name, "payment_safety": payment_safety(result.events.events),
                     "trajectory_match": trajectory_match([e.data["role"] for e in result.events.of("llm")],
                                                          expected.get("agents_in_order", [])),
                     "security_block": security_block(bool(result.final.get("blocked")),
                                                       expected.get("flags", {}).get("blocked", False))})
    return rows


def sdk_evaluate(target: Any, data: Any, evaluators: list, *, offline: bool = True, client: Any = None) -> Any:
    """Use the installed SDK with upload_results=False for local experiments."""
    if offline:
        # The SDK still constructs a client in local evaluation. Supply loopback explicitly.
        with capture_server() as (endpoint, _):
            local = make_client("masked", api_url=endpoint, api_key="local-test", auto_batch_tracing=False)
            with tracing_scope("closed", local, enabled=False):
                return safe_call(evaluate, target, data=data, evaluators=evaluators, upload_results=False,
                                 disable_evaluator_tracing=True, client=local, experiment_prefix="orchestra-eval")
    with tracing_scope("masked", client):
        return safe_call(evaluate, target, data=data, evaluators=evaluators,
                         client=client, experiment_prefix="orchestra-eval")


def summarize(rows: list[dict]) -> dict:
    """Summarize deterministic scenario scores."""
    return {key: pass_rate(row[key] for row in rows) for key in
            ("payment_safety", "trajectory_match", "security_block")}
