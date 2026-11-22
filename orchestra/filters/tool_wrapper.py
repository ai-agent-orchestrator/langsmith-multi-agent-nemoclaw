"""툴을 FilterChain / HITL / 샌드박스로 감싼다 (prebuilt 백엔드용, DESIGN §6.1).

흐름: pre_tool → (승인 필요 시 interrupt → 재평가) → 실행(샌드박스 또는 직접) → post_tool
- BLOCK 은 예외가 아니라 {"error": "blocked", ...} 를 돌려줘 에이전트가 계획을 바꿀 수 있게 한다.
- COMMIT 툴은 체인 구성과 무관하게 '승인 장부'에 승인이 없으면 실행하지 않는다 (마지막 방어선).
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool

from .. import hitl
from ..runtime import Runtime
from ..sandbox.base import ExecRequest
from ..specs import SideEffect, ToolSpec
from .base import Action, FilterContext, Stage
from .chain import FilterChain


def _thread_id(rt: Runtime) -> str:
    """현재 실행 중인 LangGraph config 의 thread_id (런타임 전역 변수에 의존하지 않는다: 동시 요청 안전)."""
    try:
        from langgraph.config import get_config

        return str(get_config().get("configurable", {}).get("thread_id", rt.thread_id))
    except RuntimeError:
        return rt.thread_id


def wrap_tool(tool: BaseTool, spec: ToolSpec, chain: FilterChain, rt: Runtime, *,
              agent: Optional[str] = None, domain: str = "") -> BaseTool:
    def _ctx(stage: Stage, payload: Any, granted: bool = False) -> FilterContext:
        return FilterContext(
            stage=stage, payload=payload, org=rt.org, domain=domain, agent=agent, tool=tool.name,
            tool_spec=spec, security_mode=rt.security_mode, sandbox_mode=rt.sandbox_mode,
            state={"thread_id": _thread_id(rt), "approval_granted": granted, "today": rt.services.get("today")},
        )

    def _blocked(reason: str, by: Optional[str], stage: str) -> dict:
        rt.events.add("blocked", tool=tool.name, agent=agent, reason=reason, by=by, stage=stage)
        return {"error": "blocked", "reason": reason, "by": by}

    def run(**kwargs: Any) -> Any:
        args: dict[str, Any] = dict(kwargs)

        pre = chain.run(_ctx(Stage.PRE_TOOL, args), dedupe=tool.name)
        if pre.action == Action.BLOCK:
            return _blocked(pre.reason, pre.blocked_by, "pre_tool")
        args = pre.payload
        route_sandbox = pre.route_sandbox

        key = hitl.approval_key(tool.name, args)
        if pre.action == Action.REQUIRE_APPROVAL:
            if not rt.approvals.is_granted(key):
                payload = {"ask": f"{tool.name} 실행을 승인할까요?", "tool": tool.name, "args": args,
                           "key": key, **(pre.approval or {})}
                approved = hitl.request(payload)          # ← interrupt. 재개되면 이 노드가 재실행된다
                rt.events.add("approval", dedupe_key=f"approval:{key}", tool=tool.name, key=key, approved=approved)
                if not approved:
                    rt.approvals.deny(key)
                    return {"error": "approval_denied", "tool": tool.name}
                rt.approvals.grant(key)
            again = chain.run(_ctx(Stage.PRE_TOOL, args, granted=True), dedupe=f"{tool.name}:granted")
            if again.action != Action.ALLOW:
                rt.approvals.consume(key)
                return _blocked(again.reason or "approval_not_sufficient", again.blocked_by, "pre_tool")
            args = again.payload
            route_sandbox = route_sandbox or again.route_sandbox

        # 마지막 방어선: COMMIT 은 승인 없이 실행 금지 (필터 구성 실수 방지)
        if spec.side_effect == SideEffect.COMMIT and not rt.approvals.is_granted(key):
            return _blocked("commit_without_approval", "tool_wrapper", "pre_tool")

        output = _execute(args, route_sandbox)
        rt.approvals.consume(key)
        if isinstance(output, dict) and output.get("error") in ("sandbox_failed", "sandbox_unavailable"):
            return output

        post = chain.run(_ctx(Stage.POST_TOOL, output), dedupe=f"{tool.name}:post")
        if post.action == Action.BLOCK:
            return _blocked(post.reason, post.blocked_by, "post_tool")
        rt.events.add("tool", tool=tool.name, agent=agent, args=args, result=post.payload)
        return post.payload

    def _execute(args: dict[str, Any], route_sandbox: bool) -> Any:
        wants_box = route_sandbox or spec.sandbox == "required"
        if spec.side_effect == SideEffect.EXEC and rt.sandbox is None:
            return {"dry_run": True, "tool": tool.name, "args": args, "note": "sandbox off: not executed"}
        if wants_box and rt.sandbox is None and rt.sandbox_mode.startswith("nemoclaw"):
            rt.events.add("sandbox", tool=tool.name, ok=False, error="sandbox_unavailable")
            return {"error": "sandbox_unavailable", "tool": tool.name}   # fail closed
        if wants_box and rt.sandbox is not None:
            req = ExecRequest(
                sandbox=spec.sandbox_name or "", tool=tool.name, args=args, timeout_s=spec.timeout_s,
                egress=[e.hostport for e in spec.egress], idempotency_key=args.get("idempotency_key") or None,
                local_fn=lambda **kw: tool.invoke(kw),
            )
            res = rt.sandbox.exec(req)
            rt.events.add("sandbox", tool=tool.name, sandbox=req.sandbox, ok=res.ok, error=res.error,
                          egress_denied=res.egress_denied, policy_revision=res.policy_revision)
            if not res.ok:
                return {"error": "sandbox_failed", "reason": res.error, "egress_denied": res.egress_denied}
            return res.output
        return tool.invoke(args)

    return StructuredTool.from_function(
        func=run, name=tool.name, description=tool.description or tool.name, args_schema=tool.args_schema,
    )
