"""계층 운영용 툴 팩토리 (DESIGN §6.5). 도메인 툴은 domains/*/tools.py 에 있다.

- make_handoff : 같은 팀 형제 에이전트로 제어권 이양 (Command(goto=..., graph=PARENT))
- make_report  : 팀장 → 상위 보고 (TeamReport 검증)
- make_escalate: 해결 불가 사유 보고 (status=blocked)
- make_request_approval: 필터와 별개로 에이전트가 명시적으로 사용자 확인을 요청
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from ..messages import TaskBrief as TaskBrief, TeamReport as TeamReport, MessageContractBlocked, validate_contract, prepare_messages

from .. import hitl
from ..runtime import Runtime
from ..taint import ToolTaintBlocked, current_thread, reject_labelled_instructions


def handoff_tool_name(target: str) -> str:
    """langgraph_supervisor 기본 접두사와 같은 규칙 (스크립트가 툴명을 하드코딩하지 않도록)."""
    return f"transfer_to_{target}"


def make_handoff(target: str, rt: Optional[Runtime] = None, source: str = "") -> BaseTool:
    @tool(handoff_tool_name(target))
    def _transfer(
        state: Annotated[dict, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
        brief: Optional[dict[str, Any]] = None,
    ):
        """같은 팀의 다른 전문 에이전트에게 대화 제어권을 직접 넘긴다 (팀장을 거치지 않는다)."""
        reject_labelled_instructions(brief)
        if rt is not None and current_thread() in rt.quarantined_threads:
            raise ToolTaintBlocked("tool_instruction_blocked")
        try:
            contract = validate_contract("brief", brief if brief is not None else {
                "goal": f"handoff to {target}", "parent": source, "source": "agent",
            })
            if contract.source != "agent":
                raise MessageContractBlocked("message_contract_blocked")
            messages = prepare_messages(state["messages"])
        except MessageContractBlocked:
            if rt is not None:
                rt.events.add("blocked", by="message_contract", stage="handoff", reason="message_contract_blocked")
            return {"error": "blocked", "reason": "message_contract_blocked"}
        ack = ToolMessage(content=contract.model_dump_json(), name=handoff_tool_name(target), tool_call_id=tool_call_id)
        if rt is not None:
            rt.events.add("handoff", source=source, target=target)
        return Command(goto=target, graph=Command.PARENT,
                       update={"messages": messages + [ack]})

    return _transfer


def make_report(team: str, rt: Optional[Runtime] = None) -> BaseTool:
    @tool("report_to_parent")
    def _report(status: Literal["done", "partial", "blocked", "needs_approval"], summary: str,
                artifacts: Optional[dict[str, Any]] = None, issues: Optional[list[str]] = None) -> str:
        """팀 작업 결과를 상위(총괄)에게 보고한다. 최종 답변 전에 호출."""
        rep = validate_contract("report", {"team": team, "status": status, "summary": summary,
                                           "artifacts": artifacts or {}, "issues": issues or [], "source": "agent"})
        if rt is not None:
            rt.events.add("report", team=team, status=status, summary=summary, artifacts=rep.artifacts,
                          issues=rep.issues)
        return rep.model_dump_json()

    return _report


def make_escalate(team: str, rt: Optional[Runtime] = None) -> BaseTool:
    @tool("escalate")
    def _escalate(reason: str) -> str:
        """팀 안에서 해결할 수 없을 때 사유를 상위에 올린다 (status=blocked 보고)."""
        rep = validate_contract("report", {"team": team, "status": "blocked", "summary": reason,
                                           "issues": [reason], "source": "agent"})
        if rt is not None:
            rt.events.add("report", team=team, status="blocked", summary=reason, artifacts={}, issues=[reason])
        return rep.model_dump_json()

    return _escalate


def make_request_approval(kind: str) -> BaseTool:
    @tool("request_user_approval")
    def _ask(question: str, details: Optional[dict[str, Any]] = None) -> dict:
        """사용자에게 명시적 확인을 요청한다 (interrupt). 승인 여부를 돌려준다."""
        approved = hitl.request({"kind": kind, "ask": question, "details": details or {}})
        return {"approved": approved}

    return _ask
