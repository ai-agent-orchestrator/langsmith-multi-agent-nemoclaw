"""툴을 FilterChain / HITL / 샌드박스로 감싼다 (prebuilt 백엔드용, DESIGN §6.1).

흐름: pre_tool → (승인 필요 시 interrupt → 재평가) → 실행(샌드박스 또는 직접) → post_tool
- BLOCK 은 예외가 아니라 {"error": "blocked", ...} 를 돌려줘 에이전트가 계획을 바꿀 수 있게 한다.
- COMMIT 툴은 체인 구성과 무관하게 '승인 장부'에 승인이 없으면 실행하지 않는다 (마지막 방어선).
"""
from __future__ import annotations

from typing import Any, Optional
import os
import math
from contextlib import nullcontext

from langchain_core.tools import BaseTool, StructuredTool

from .. import hitl
from ..runtime import Runtime
from ..sandbox.base import ExecRequest
from ..sandbox.fake import FakeSandboxRunner
from ..security.egress_isolation import IsolatingSandboxRunner
from ..specs import SideEffect, ToolSpec
from ..taint import ToolTaintBlocked, current_thread, instruction_in, reject_labelled_instructions
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
        if by == "approval_binding":
            raise hitl.ApprovalBindingBlocked("approval_binding_blocked")
        return {"error": "blocked", "reason": reason, "by": by}

    def run(**kwargs: Any) -> Any:
        args: dict[str, Any] = dict(kwargs)
        if spec.side_effect == SideEffect.COMMIT:
            reject_labelled_instructions(args)
            if current_thread() in rt.quarantined_threads:
                raise ToolTaintBlocked("tool_instruction_blocked")

        pre = chain.run(_ctx(Stage.PRE_TOOL, args), dedupe=tool.name)
        if pre.action == Action.BLOCK:
            return _blocked(pre.reason, pre.blocked_by, "pre_tool")
        args = pre.payload
        route_sandbox = pre.route_sandbox

        from langgraph.config import get_config

        try:
            user_id = str(get_config().get("configurable", {}).get("request_user", "anonymous"))
        except RuntimeError:
            user_id = "anonymous"

        def binding(arguments: dict, expires_at: float) -> hitl.ApprovalRecord:
            amount_fn = rt.services.get("approval_amount")
            amount = amount_fn(tool.name, arguments) if callable(amount_fn) else (pre.approval or {}).get("amount")
            return hitl.make_approval_record(tool.name, arguments, user_id=user_id, amount=amount,
                                            expires_at=expires_at, request_id=_thread_id(rt))

        try:
            ttl = float(os.environ.get("ORCHESTRA_APPROVAL_TTL_SECONDS", "300"))
        except ValueError:
            return _blocked("invalid_approval_ttl", "approval_binding", "pre_tool")
        if not math.isfinite(ttl) or ttl <= 0:
            return _blocked("invalid_approval_ttl", "approval_binding", "pre_tool")
        record = binding(args, rt.clock() + ttl)
        # Read the original interrupt payload; no ledger mutation or new expiry is permitted on resume.
        if rt.graph is not None and rt.graph.checkpointer is not None:
            try:
                snapshot = rt.graph.get_state({"configurable": {"thread_id": _thread_id(rt)}}, subgraphs=True)
            except Exception:
                return _blocked("approval_checkpoint_unavailable", "approval_binding", "pre_tool")
            pending = [i.value for i in snapshot.interrupts if isinstance(i.value, dict) and i.value.get("tool") == tool.name]
            if pending:
                try:
                    prior = hitl.ApprovalRecord.model_validate(pending[0]["binding"])
                    record = binding(args, prior.expires_at)
                    if record.key != prior.key or rt.clock() >= prior.expires_at:
                        return _blocked("approval_binding_invalid_or_expired", "approval_binding", "pre_tool")
                except (ValueError, KeyError, TypeError):
                    return _blocked("approval_binding_invalid", "approval_binding", "pre_tool")
        key = record.key
        if pre.action == Action.REQUIRE_APPROVAL:
            if not rt.approvals.is_granted(key, binding=record, now=rt.clock()):
                payload = {"ask": f"{tool.name} 실행을 승인할까요?", "tool": tool.name, "args": args,
                           **(pre.approval or {}), "key": key, "binding": record.model_dump()}
                approved = hitl.request(payload)          # ← interrupt. 재개되면 이 노드가 재실행된다
                rt.events.add("approval", dedupe_key=f"approval:{key}", tool=tool.name, key=key, approved=approved,
                              binding={"user_id": record.user_id, "amount": record.amount, "expires_at": record.expires_at,
                                       "request_id": record.request_id})
                if not approved:
                    rt.approvals.deny(key, binding=record)
                    return {"error": "approval_denied", "tool": tool.name}
                if not rt.approvals.grant(record, now=rt.clock()):
                    return _blocked("approval_expired_or_replayed", "approval_binding", "pre_tool")
            again = chain.run(_ctx(Stage.PRE_TOOL, args, granted=True), dedupe=f"{tool.name}:granted")
            if again.action != Action.ALLOW:
                rt.approvals.consume(key)
                return _blocked(again.reason or "approval_not_sufficient", again.blocked_by, "pre_tool")
            args = again.payload
            if binding(args, record.expires_at).key != key:
                rt.approvals.consume(key)
                return _blocked("approval_arguments_changed", "approval_binding", "pre_tool")
            route_sandbox = route_sandbox or again.route_sandbox

        # 마지막 방어선: COMMIT 은 승인 없이 실행 금지 (필터 구성 실수 방지)
        if spec.side_effect == SideEffect.COMMIT and not rt.approvals.is_granted(key, binding=record, now=rt.clock()):
            return _blocked("commit_without_approval", "tool_wrapper", "pre_tool")

        store = rt.approvals.store
        if store.persistent and spec.side_effect == SideEffect.COMMIT:
            if tool.name not in rt.services.get("ledger_atomic_tools", set()) or (rt.sandbox is not None and not isinstance(rt.sandbox, FakeSandboxRunner)):
                return _blocked("persistent_payment_transport_not_transactional", "approval_binding", "pre_tool")
        with store.transaction() if store.persistent else nullcontext():
            if (pre.action == Action.REQUIRE_APPROVAL or spec.side_effect == SideEffect.COMMIT) and not rt.approvals.claim(record, now=rt.clock()):
                return _blocked("approval_expired_or_consumed", "approval_binding", "pre_tool")
            output = _execute(args, route_sandbox)
        if isinstance(output, dict) and output.get("error") in ("sandbox_failed", "sandbox_unavailable"):
            return output

        tainted = instruction_in(output)
        if tainted:
            rt.quarantined_threads.add(current_thread())
            rt.events.add("blocked", by="tool_taint", stage="post_tool", reason="tool_instruction_blocked", tool=tool.name)
            output = {"error": "tool_instruction_blocked"}
        post = chain.run(_ctx(Stage.POST_TOOL, output), dedupe=f"{tool.name}:post")
        approval_ref = {"approval_ref": store.token("approval-ref", key)} if store.persistent and spec.side_effect == SideEffect.COMMIT else {}
        if not tainted and instruction_in(post.payload):
            tainted = True
            rt.quarantined_threads.add(current_thread())
            rt.events.add("blocked", by="tool_taint", stage="post_tool", reason="tool_instruction_blocked", tool=tool.name)
        if tainted:
            labelled = {"source": "tool", "data": {"error": "tool_instruction_blocked"}}
            rt.events.add("tool", tool=tool.name, agent=agent, args=args, result=labelled, **approval_ref)
            return labelled
        if post.action == Action.BLOCK:
            return _blocked(post.reason, post.blocked_by, "post_tool")
        labelled = {"source": "tool", "data": post.payload}
        rt.events.add("tool", tool=tool.name, agent=agent, args=args, result=labelled, **approval_ref)
        return labelled

    def _execute(args: dict[str, Any], route_sandbox: bool) -> Any:
        wants_box = route_sandbox or spec.sandbox == "required"
        if rt.events.capture_trace:
            route = "sandbox" if wants_box and rt.sandbox is not None else "direct"
            if spec.side_effect == SideEffect.EXEC and rt.sandbox is None:
                route = "dry_run"
            elif wants_box and rt.sandbox is None and rt.sandbox_mode.startswith("nemoclaw"):
                route = "unavailable"
            rt.events.add("sandbox_route", tool=tool.name, agent=agent, sandbox=spec.sandbox_name,
                          route=route, mode=rt.sandbox_mode)
        if spec.side_effect == SideEffect.EXEC and rt.sandbox is None:
            return {"dry_run": True, "tool": tool.name, "args": args, "note": "sandbox off: not executed"}
        if wants_box and rt.sandbox is None and rt.sandbox_mode.startswith("nemoclaw"):
            rt.events.add("sandbox", tool=tool.name, ok=False, error="sandbox_unavailable")
            return {"error": "sandbox_unavailable", "tool": tool.name}   # fail closed
        if wants_box and rt.sandbox is not None:
            req = ExecRequest(
                sandbox=spec.sandbox_name or "", tool=tool.name, args=args, timeout_s=spec.timeout_s,
                egress=[e.hostport for e in spec.egress], idempotency_key=args.get("idempotency_key") or None,
                local_fn=lambda **kw: tool.invoke(kw), agent=agent,
            )
            # Every sandbox call passes the isolation check, whichever runner is configured.
            runner = rt.sandbox
            if not isinstance(runner, IsolatingSandboxRunner):
                runner = IsolatingSandboxRunner(runner, rt.egress_guard)
            res = runner.exec(req)
            rt.events.add("sandbox", tool=tool.name, sandbox=req.sandbox, ok=res.ok, error=res.error,
                          egress_denied=res.egress_denied, policy_revision=res.policy_revision)
            if not res.ok:
                return {"error": "sandbox_failed", "reason": res.error, "egress_denied": res.egress_denied}
            return res.output
        return tool.invoke(args)

    return StructuredTool.from_function(
        func=run, name=tool.name, description=tool.description or tool.name, args_schema=tool.args_schema,
    )
