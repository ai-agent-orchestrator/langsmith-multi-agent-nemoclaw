"""OrchestraService: 게이트 → 그래프 실행 → interrupt/resume → 응답 필터 (DESIGN §14).

LangSmith 설정(run_config, tracing_scope, 마스킹 클라이언트)은 config_hook / scope 로 주입한다 (M7, Codex).
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Optional

from langchain_core.messages import BaseMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from .backends.base import ModelBlocked
from .filters.base import Action, FilterContext, Stage
from .patterns.hierarchical_graph import BuiltApp
from .messages import MessageContractBlocked
from .hitl import ApprovalBindingBlocked
from .taint import ToolTaintBlocked


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

    def _cfg(self, thread_id: str, user_id: str = "anonymous") -> dict:
        cfg = self.app.config(thread_id)
        cfg["configurable"]["request_user"] = user_id
        return self.config_hook(cfg, thread_id) if self.config_hook else cfg

    def _finish(self, result: dict, thread_id: str) -> dict:
        intr = result.get("__interrupt__")
        if intr:
            first = intr[0]
            if self.app.runtime.events.capture_trace:
                value = first.value if isinstance(first.value, dict) else {}
                self.app.runtime.events.add("approval_request", tool=value.get("tool"), ask=value.get("ask", ""))
            return {"answer": "", "blocked": False, "reason": "", "thread_id": thread_id,
                    "interrupt": {"id": getattr(first, "id", None), "payload": first.value}}
        answer = _text(result["messages"][-1]) if result.get("messages") else ""
        out = self.app.chain.run(FilterContext(Stage.PRE_RESPONSE, answer, org=self.app.org.name))
        if out.action == Action.BLOCK:
            return {"answer": f"차단: 응답 정책 위반 ({out.reason})", "blocked": True, "reason": out.reason,
                    "thread_id": thread_id, "interrupt": None}
        return {"answer": out.payload, "blocked": False, "reason": "", "thread_id": thread_id, "interrupt": None}

    def ask(self, question: str, thread_id: Optional[str] = None, *, user_id: str = "anonymous") -> dict:
        thread_id = thread_id or "default"
        gate = self.app.chain.run(FilterContext(Stage.PRE_REQUEST, question, org=self.app.org.name),
                                  dedupe="request")
        if gate.action == Action.BLOCK:
            self.app.runtime.events.add("blocked", stage="pre_request", reason=gate.reason, by=gate.blocked_by)
            return {"answer": f"차단: 보안 정책 위반 요청입니다 ({gate.reason})", "blocked": True,
                    "reason": gate.reason, "thread_id": thread_id, "interrupt": None}
        return self._invoke({"messages": [("user", question)]}, thread_id, user_id)

    def resume(self, thread_id: str, decision: bool | dict, *, user_id: str = "anonymous") -> dict:
        """interrupt 에 대한 사용자 응답으로 그래프를 재개한다."""
        stop = self.app.runtime.security_control.guard("")
        if stop is not None:  # 대기 중인 interrupt 는 건드리지 않아 해제 후 다시 재개할 수 있다.
            return self._stopped(stop, "security_control", "차단: 운영자 중단 상태입니다", thread_id)
        return self._invoke(Command(resume=decision), thread_id, user_id)

    def _stopped(self, reason: str, by: str, answer: str, thread_id: str) -> dict:
        self.app.runtime.events.add("blocked", by=by, reason=reason)
        return {"answer": answer, "blocked": True, "reason": reason, "thread_id": thread_id, "interrupt": None}

    def _invoke(self, payload: Any, thread_id: str, user_id: str = "anonymous") -> dict:
        try:
            with self.scope():
                result = self.app.graph.invoke(payload, self._cfg(thread_id, user_id))
        except ToolTaintBlocked:
            self.app.runtime.events.add("blocked", by="tool_taint", reason="tool_instruction_blocked")
            return {"answer": "차단: 툴 결과의 지시문 전파", "blocked": True, "reason": "tool_instruction_blocked",
                    "thread_id": thread_id, "interrupt": None}
        except MessageContractBlocked:
            self.app.runtime.events.add("blocked", by="message_contract", reason="message_contract_blocked")
            return {"answer": "차단: 메시지 계약 위반", "blocked": True, "reason": "message_contract_blocked",
                    "thread_id": thread_id, "interrupt": None}
        except GraphRecursionError:
            return self._stopped("recursion_limit", "recursion_limit", "차단: 에이전트 반복 한도를 넘었습니다", thread_id)
        except ModelBlocked:
            reason = self.app.runtime.security_control.guard("") or "model_blocked"
            return self._stopped(reason, "pre_model", "차단: 모델 호출 정책 위반", thread_id)
        except ApprovalBindingBlocked:
            return {"answer": "차단: 승인 바인딩이 유효하지 않습니다", "blocked": True, "reason": "approval_binding_blocked",
                    "thread_id": thread_id, "interrupt": None}
        return self._finish(result, thread_id)


def studio_graph(config: Optional[dict] = None) -> Any:
    """Build a fresh scripted/fake graph for local Studio, without keys or paid models."""
    from .models.factory import ScriptedModelFactory
    from .patterns.hierarchical_graph import build_hierarchical_app
    from .runtime import Runtime
    from .sandbox.fake import FakeSandboxRunner
    from .sandbox.nemoclaw import policy
    from .simulation import load_scenario

    scenario = load_scenario("jeju_trip_payment")
    services = {"today": scenario["context"]["today"], "now": 0.0}
    runtime = Runtime(security_mode="closed", sandbox_mode="fake", services=services)
    app = build_hierarchical_app(scenario["org"], ScriptedModelFactory(scenario["script"], runtime.events),
                                 runtime=runtime, services=services)
    specs = [spec for _, spec in app.resolved.tools.values()]
    presets = policy.build_presets(specs, security_mode="closed")
    sandboxes = sorted({spec.sandbox_name for spec in specs if spec.sandbox_name})
    runtime.sandbox = FakeSandboxRunner(policy.allowed_by_sandbox(presets, policy.applied_presets(sandboxes, presets)))
    return app.graph
