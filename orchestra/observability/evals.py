"""Offline deterministic evaluation and an explicitly requested hosted SDK experiment."""
from typing import Any

from langsmith import evaluate

from ..simulation import list_scenarios, run_scenario
from .evaluators import payment_safety, require_payment_safety, trajectory_match, security_block, pass_rate
from .safe import safe_call
from .tracing import tracing_scope
from .client import security_mode


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


def _evaluate_locally(target: Any, data: Any, evaluators: list) -> list[dict]:
    if isinstance(data, (str, bytes)):
        raise ValueError("offline evaluation needs local examples, not a hosted dataset name")
    rows = []
    for example in data:
        inputs, reference = ((example["inputs"], example.get("outputs")) if isinstance(example, dict)
                             else (example.inputs, example.outputs))
        output = target(inputs)
        rows.append({"inputs": inputs, "outputs": output, "reference": reference,
                     "scores": [evaluator(output, reference) for evaluator in evaluators]})
    return rows


def sdk_evaluate(target: Any, data: Any, evaluators: list, *, offline: bool = True, client: Any = None) -> Any:
    """offline: 프로세스 안에서만 채점한다(SDK·클라이언트 없음). 업로드는 열린 클라이언트를 명시해야 한다.

    평가자는 `(outputs, reference_outputs)` 순서의 위치 인자로 호출된다.
    """
    if offline:
        return _evaluate_locally(target, data, evaluators)
    closed = security_mode() == "closed" or getattr(client, "mode", None) == "closed"
    if closed or getattr(client, "raw", None) is None:
        raise RuntimeError("hosted evaluation requires an explicit open client and is refused in closed mode")
    with tracing_scope("masked", client):
        return safe_call(evaluate, target, data=data, evaluators=evaluators,
                         client=client, experiment_prefix="orchestra-eval")


def summarize(rows: list[dict]) -> dict:
    """Summarize deterministic scenario scores."""
    return {key: pass_rate(row[key] for row in rows) for key in
            ("payment_safety", "trajectory_match", "security_block")}
