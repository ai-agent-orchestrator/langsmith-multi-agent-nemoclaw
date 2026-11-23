"""Opt-in hosted pytest reporting; default test selection excludes this marker."""
import os

import pytest

from orchestra.observability.evals import evaluate_scenarios, summarize
from orchestra.observability.safe import safe_call


@pytest.mark.langsmith
def test_scenario_scores_in_langsmith():
    """Use installed pytest logging functions only when explicitly configured."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        pytest.skip("explicit hosted test requires LANGSMITH_API_KEY")
    from langsmith.testing import log_inputs, log_outputs, log_feedback

    scores = summarize(evaluate_scenarios())
    safe_call(log_inputs, {"suite": "orchestra-scenarios"})
    safe_call(log_outputs, scores)
    for key, score in scores.items():
        safe_call(log_feedback, key=key, score=score)
    assert scores["payment_safety"] == 1
