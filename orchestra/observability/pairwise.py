"""Pairwise comparisons support injected judges and installed SDK capabilities."""
from typing import Any, Callable

from langsmith import evaluation

from .masking import mask_any
from .safe import safe_call
from .evaluators import judge_prompt


def compare(a: Any, b: Any, judge: Callable | None = None) -> dict:
    """Compare outputs using an explicit comparator, never an implicit paid model."""
    if judge is None:
        return {"status": "unavailable", "reason": "inject comparator model", "equal": a == b}
    return safe_call(judge, judge_prompt("comparator"), mask_any({"a": a, "b": b}), default={"status": "unavailable"})


def compare_targets(inputs: Any, target_a: Callable, target_b: Callable, judge: Callable | None = None) -> dict:
    """Run both supplied pattern targets on identical inputs before comparing outputs."""
    return compare(target_a(inputs), target_b(inputs), judge)


def sdk_compare(experiments: tuple, evaluators: list, client: Any) -> Any:
    """Use the verified evaluation-module comparative API when available."""
    method = getattr(evaluation, "evaluate_comparative", None)
    if method is None:
        return {"status": "unavailable", "guide": "docs/langsmith-ui-setup.md"}
    return safe_call(method, experiments, evaluators=evaluators, client=client)
