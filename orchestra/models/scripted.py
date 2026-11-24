"""ScriptedChatModel: LLM 없이 실제 그래프를 실행하기 위한 결정적 모델 (DESIGN §12.2).

역할(role)별 턴 큐를 가진다. turn 형식:
    {"tool_calls": [{"name": "...", "args": {...}}], "content": "선택"}   # 툴 호출
    {"content": "최종 답"}                                                # 응답
바인딩되지 않은 툴을 호출하는 스크립트는 즉시 오류 (라이브러리 버전이 handoff 툴명을 바꾸면 바로 드러남).
"""
from __future__ import annotations

import itertools
from collections import deque
from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from ..runtime import EventLog

EXHAUSTED = "(script exhausted)"


class ScriptedChatModel(BaseChatModel):
    role: str = "agent"
    bound_tools: list[str] = []
    strict_tools: bool = True
    _queue: deque = PrivateAttr(default_factory=deque)
    _counter: Any = PrivateAttr(default_factory=lambda: itertools.count(1))
    _events: Optional[EventLog] = PrivateAttr(default=None)
    _kind: str = PrivateAttr(default="agent")

    def __init__(self, role: str, turns: list[dict], events: Optional[EventLog] = None,
                 kind: str = "agent", **kw: Any):
        super().__init__(role=role, **kw)
        self._queue = deque(turns)
        self._events = events
        self._kind = kind

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        names = []
        for t in tools:
            names.append(t if isinstance(t, str) else getattr(t, "name", None) or t.get("name") or t["function"]["name"])
        clone = self.model_copy(update={"bound_tools": names})  # _queue/_counter 는 같은 객체를 공유한다
        return clone

    def remaining(self) -> int:
        return len(self._queue)

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        prompt_chars = sum(len(str(m.content)) for m in messages)
        if not self._queue:
            msg = AIMessage(content=EXHAUSTED)
            self._log(msg, exhausted=True)
            return ChatResult(generations=[ChatGeneration(message=msg)])
        turn = self._queue.popleft()
        calls = []
        for c in turn.get("tool_calls", []):
            if self.strict_tools and c["name"] not in self.bound_tools:
                raise ValueError(
                    f"[{self.role}] scripted call to unbound tool {c['name']!r}; bound={self.bound_tools}"
                )
            calls.append({"name": c["name"], "args": c.get("args", {}), "id": f"call_{self.role}_{next(self._counter)}",
                          "type": "tool_call"})
        content = turn.get("content", "")
        out_tokens = max(1, len(content) // 4) + 12 * len(calls)
        in_tokens = max(1, prompt_chars // 4)
        msg = AIMessage(content=content, tool_calls=calls,
                        usage_metadata={"input_tokens": in_tokens, "output_tokens": out_tokens,
                                        "total_tokens": in_tokens + out_tokens})
        self._log(msg)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _log(self, msg: AIMessage, exhausted: bool = False) -> None:
        if self._events is not None:
            self._events.add("llm", role=self.role, node_kind=self._kind, content=msg.content,
                             tool_calls=[{"name": c["name"], "args": c["args"]} for c in msg.tool_calls],
                             exhausted=exhausted)
