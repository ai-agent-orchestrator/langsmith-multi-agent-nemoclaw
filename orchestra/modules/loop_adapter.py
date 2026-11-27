"""벤더 `loop_control.run_loop` 를 그래프 노드 형태로 감싼다 (원본은 수정하지 않는다)."""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Optional

from third_party.multi_agent_module_lib.loop_control import run_loop

# 호출자가 큰 값을 넘겨도 폭주 루프가 되지 않게 하는 상한.
ITERATION_CEILING = 100


def loop_node(step: Callable[[str, int], str], is_done: Callable[[str], bool], *, max_iterations: int = 5,
              max_cost: Optional[float] = None, cost_per_step: float = 0.0) -> Callable[[Mapping[str, Any]], dict]:
    """`{"seed": str}` 를 받아 반복 결과(output, iterations, passed, stop_reason)를 돌려주는 노드를 만든다."""
    if type(max_iterations) is not int or not 1 <= max_iterations <= ITERATION_CEILING:
        raise ValueError(f"max_iterations must be an integer from 1 to {ITERATION_CEILING}")
    for name, value in (("max_cost", max_cost), ("cost_per_step", cost_per_step)):
        # NaN 은 모든 비교가 거짓이라 비용 상한을 조용히 무력화한다.
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{name} must be a finite non-negative number")

    def node(state: Mapping[str, Any]) -> dict:
        result = run_loop(step, is_done, str(state.get("seed", "")), max_iterations=max_iterations,
                          max_cost=max_cost, cost_per_step=cost_per_step)
        return {"output": result.output, "iterations": result.iterations, "passed": result.passed,
                "stop_reason": result.stop_reason}

    return node
