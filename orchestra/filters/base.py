"""Filter 계약 (DESIGN §11.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from ..specs import ToolSpec


class Stage(str, Enum):
    PRE_REQUEST = "pre_request"
    PRE_MODEL = "pre_model"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_RESPONSE = "pre_response"


class Action(str, Enum):
    ALLOW = "allow"
    MODIFY = "modify"
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"
    ROUTE_SANDBOX = "route_sandbox"


@dataclass
class FilterContext:
    stage: Stage
    payload: Any
    org: str = ""
    domain: str = ""
    agent: Optional[str] = None
    tool: Optional[str] = None
    tool_spec: Optional[ToolSpec] = None
    state: dict = field(default_factory=dict)  # 읽기 전용 스냅샷: thread_id, approval_granted ...
    security_mode: str = "masked"
    sandbox_mode: str = "off"


@dataclass
class FilterDecision:
    action: Action = Action.ALLOW
    payload: Any = None                       # MODIFY 일 때 교체값
    reason: str = ""
    approval: Optional[dict] = None           # REQUIRE_APPROVAL 일 때 interrupt payload
    feedback: dict[str, float] = field(default_factory=dict)  # LangSmith feedback 키/점수

    @classmethod
    def allow(cls) -> "FilterDecision":
        return cls(Action.ALLOW)

    @classmethod
    def modify(cls, payload: Any, reason: str = "") -> "FilterDecision":
        return cls(Action.MODIFY, payload=payload, reason=reason)

    @classmethod
    def block(cls, reason: str, **feedback: float) -> "FilterDecision":
        return cls(Action.BLOCK, reason=reason, feedback=dict(feedback))

    @classmethod
    def require_approval(cls, reason: str, approval: Optional[dict] = None) -> "FilterDecision":
        return cls(Action.REQUIRE_APPROVAL, reason=reason, approval=approval or {})

    @classmethod
    def route_sandbox(cls, reason: str = "") -> "FilterDecision":
        return cls(Action.ROUTE_SANDBOX, reason=reason)


class Filter(Protocol):
    name: str

    def __call__(self, ctx: FilterContext) -> FilterDecision: ...
