"""스레드(thread_id)별 툴 호출 횟수 상한. 무한 루프·과금 폭주 방지 (module-lib loop_control 과 같은 취지).

주의: interrupt 재개 시 툴 노드가 재실행되어 카운트가 1~2 늘 수 있다 (상한은 보수적으로 잡을 것).
"""
from __future__ import annotations

from collections import defaultdict

from ..base import Filter, FilterContext, FilterDecision


def budget(max_tool_calls: int = 40) -> Filter:
    counts: dict[str, int] = defaultdict(int)

    class _Budget:
        name = "budget"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            tid = str(ctx.state.get("thread_id", "default"))
            counts[tid] += 1
            if counts[tid] > max_tool_calls:
                return FilterDecision.block(f"tool call budget exceeded ({max_tool_calls})", budget=0.0)
            return FilterDecision.allow()

    return _Budget()
