"""OrchestraService: 게이트 → 그래프 실행 → interrupt/resume → 응답 필터 (DESIGN §14).

LangSmith 설정(run_config, tracing_scope, 마스킹 클라이언트)은 config_hook / scope 로 주입한다 (M7, Codex).
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Optional

from langchain_core.messages import BaseMessage
from langgraph.types import Command

from .filters.base import Action, FilterContext, Stage
from .patterns.hierarchical_graph import BuiltApp


def _text(msg: Any) -> str:
    if isinstance(msg, BaseMessage):
        return msg.content if isinstance(msg.content, str) else str(msg.content)
    if isinstance(msg, dict):
        return str(msg.get("content", ""))
    return str(msg)


class OrchestraService:
    def __init__(self, app: BuiltApp, *, config_hook: Optional[Callable[[dict, str], dict]] = None,
                 scope: Optional[Callable[[], ContextManager]] = None):
        self.app = app
        self.config_hook = config_hook      # (config, thread_id) -> config  (태그/메타데이터/콜백 추가)
        self.scope = scope or nullcontext   # 예: tracing_scope(mode)

    def _cfg(self, thread_id: str) -> dict:
        cfg = self.app.config(thread_id)
        return self.config_hook(cfg, thread_id) if self.config_hook else cfg

    def _finish(self, result: dict, thread_id: str) -> dict:
        intr = result.get("__interrupt__")
        if intr:
            first = intr[0]
            return {"answer": "", "blocked": False, "reason": "", "thread_id": thread_id,
                    "interrupt": {"id": getattr(first, "id", None), "payload": first.value}}
        answer = _text(result["messages"][-1]) if result.get("messages") else ""
        out = self.app.chain.run(FilterContext(Stage.PRE_RESPONSE, answer, org=self.app.org.name))
        if out.action == Action.BLOCK:
            return {"answer": f"차단: 응답 정책 위반 ({out.reason})", "blocked": True, "reason": out.reason,
                    "thread_id": thread_id, "interrupt": None}
        return {"answer": out.payload, "blocked": False, "reason": "", "thread_id": thread_id, "interrupt": None}

    def ask(self, question: str, thread_id: Optional[str] = None) -> dict:
        thread_id = thread_id or "default"
        gate = self.app.chain.run(FilterContext(Stage.PRE_REQUEST, question, org=self.app.org.name),
                                  dedupe="request")
        if gate.action == Action.BLOCK:
            self.app.runtime.events.add("blocked", stage="pre_request", reason=gate.reason, by=gate.blocked_by)
            return {"answer": f"차단: 보안 정책 위반 요청입니다 ({gate.reason})", "blocked": True,
                    "reason": gate.reason, "thread_id": thread_id, "interrupt": None}
        res = self._invoke({"messages": [("user", question)]}, thread_id)
        return res if self._aborted(res) else self._finish(res, thread_id)

    def resume(self, thread_id: str, decision: bool | dict) -> dict:
        """interrupt 에 대한 사용자 응답으로 그래프를 재개한다."""
        res = self._invoke(Command(resume=decision), thread_id)
        return res if self._aborted(res) else self._finish(res, thread_id)

    # ------------------------------------------------------------ 폭주 방어: 재귀 한도는 예외가 아니라 '중단 결과'로 닫는다
    def _invoke(self, payload: Any, thread_id: str) -> Any:
        from langgraph.errors import GraphRecursionError
        try:
            with self.scope():
                return self.app.graph.invoke(payload, self._cfg(thread_id))
        except GraphRecursionError:
            self.app.runtime.events.add("blocked", stage="runtime", reason="recursion_limit", by="recursion_limit")
            return {"answer": "처리를 중단했습니다: 에이전트 간 호출이 한도를 넘었습니다(무한 루프 방지).",
                    "blocked": True, "reason": "recursion_limit", "thread_id": thread_id, "interrupt": None,
                    "_aborted": True}

    @staticmethod
    def _aborted(res: Any) -> bool:
        return isinstance(res, dict) and res.get("_aborted") is True
