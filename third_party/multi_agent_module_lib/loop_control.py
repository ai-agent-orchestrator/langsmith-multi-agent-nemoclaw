"""Loop controller: iterate until quality passes, with hard iteration/cost caps.

Pattern inspired by `ralph-loop` (Stop Hook + --max-iterations).
Independent illustrative implementation. Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class LoopResult:
    output: str
    iterations: int
    passed: bool
    stop_reason: str
    history: list[str] = field(default_factory=list)


def run_loop(
    step: Callable[[str, int], str],
    is_done: Callable[[str], bool],
    seed: str,
    max_iterations: int = 5,
    max_cost: float | None = None,
    cost_per_step: float = 0.0,
) -> LoopResult:
    """step(prev_output, i) -> new output. Stops on success or any cap."""
    out, history, spent = seed, [], 0.0
    for i in range(1, max_iterations + 1):
        if max_cost is not None and spent + cost_per_step > max_cost:
            return LoopResult(out, i - 1, False, "max_cost", history)
        out = step(out, i)
        spent += cost_per_step
        history.append(out)
        if is_done(out):
            return LoopResult(out, i, True, "done", history)
    return LoopResult(out, max_iterations, False, "max_iterations", history)
