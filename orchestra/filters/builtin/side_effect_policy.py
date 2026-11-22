"""부작용 등급 기반 강제: COMMIT 은 승인 없이는 실행 불가, sandbox 요구 툴은 샌드박스로 라우팅."""
from __future__ import annotations

from ...specs import SideEffect
from ..base import Filter, FilterContext, FilterDecision


def side_effect_policy(commit_requires_approval: bool = True) -> Filter:
    class _Policy:
        name = "side_effect_policy"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            spec = ctx.tool_spec
            if spec is None:
                return FilterDecision.allow()
            if commit_requires_approval and spec.side_effect == SideEffect.COMMIT:
                if not ctx.state.get("approval_granted"):
                    return FilterDecision.require_approval(
                        f"commit tool {spec.name} needs user approval",
                        {"kind": "commit", "tool": spec.name, "args": ctx.payload},
                    )
            if spec.sandbox == "required":
                return FilterDecision.route_sandbox(f"{spec.name} runs in {spec.sandbox_name}")
            return FilterDecision.allow()

    return _Policy()
