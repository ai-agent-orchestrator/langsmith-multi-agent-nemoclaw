"""Bounded message contracts and role-attested data framing at model/handoff boundaries."""
from __future__ import annotations

import json
import math
from typing import Any, Literal, overload

from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Source = Literal["user", "tool", "agent", "system"]


class MessageContractBlocked(RuntimeError):
    """Reject invalid data without exposing its original contents in exceptions."""


def bounded_json(value: Any, depth: int = 0, counter: list[int] | None = None) -> Any:
    """Bound nested JSON depth, nodes, key/text sizes, and numeric finiteness."""
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > 8 or counter[0] > 256:
        raise ValueError("message structure limit")
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("message text limit")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("message key limit")
            bounded_json(item, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            bounded_json(item, depth + 1, counter)
    elif value is not None and (not isinstance(value, (bool, int, float)) or
                               isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("message requires JSON values")
    if depth == 0 and len(json.dumps(value, ensure_ascii=False)) > 8192:
        raise ValueError("message aggregate limit")
    return value


class Contract(BaseModel):
    """Source is mandatory; unknown fields and coercion are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    source: Source


class TaskBrief(Contract):
    """Bounded delegation data, never a system instruction."""

    goal: str = Field(min_length=1, max_length=4096)
    constraints: dict[str, Any] = Field(default_factory=dict)
    deliverable: str = Field(default="", max_length=2048)
    parent: str = Field(default="", max_length=128)

    @field_validator("constraints")
    @classmethod
    def bounded_constraints(cls, value: dict) -> dict:
        """Reject oversized nested constraint values."""
        return bounded_json(value)


class TeamReport(Contract):
    """Bounded team result with an explicit producer source."""

    team: str = Field(min_length=1, max_length=128)
    status: Literal["done", "partial", "blocked", "needs_approval"]
    summary: str = Field(max_length=4096)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("artifacts", "issues")
    @classmethod
    def bounded_fields(cls, value: Any) -> Any:
        """Bound nested artifacts and every issue separately."""
        bounded_json(value)
        if isinstance(value, list) and any(len(issue) > 512 for issue in value):
            raise ValueError("message issue limit")
        return value


@overload
def validate_contract(kind: Literal["brief"], value: Any) -> TaskBrief: ...


@overload
def validate_contract(kind: Literal["report"], value: Any) -> TeamReport: ...


def validate_contract(kind: str, value: Any) -> Any:
    """Fail closed on invalid contracts, without returning Pydantic's input details."""
    try:
        model: Any = {"brief": TaskBrief, "report": TeamReport}[kind]
        return model.model_validate(value)
    except (KeyError, ValidationError, ValueError, TypeError):
        raise MessageContractBlocked("message_contract_blocked") from None


def prepare_messages(messages: list) -> list[BaseMessage]:
    """Attest SDK roles, validate contract payloads, and frame agent/tool text as data copies."""
    prepared = []
    roles = {"human": "user", "ai": "agent", "tool": "tool", "system": "system"}
    for message in messages:
        if not isinstance(message, BaseMessage) or message.type not in roles:
            raise MessageContractBlocked("message_contract_blocked")
        source = roles[message.type]
        if isinstance(message, ToolMessage) and (message.name or "").startswith(("transfer_", "report_", "escalate")):
            source = "agent"
        if message.additional_kwargs.get("source", source) != source:
            raise MessageContractBlocked("message_contract_blocked")
        content = message.content
        framed = message.additional_kwargs.get("orchestra_data", False)
        if framed:
            try:
                if "source" not in message.additional_kwargs or not isinstance(content, str):
                    raise ValueError
                if not content.startswith('<orchestra-data>\n') or not content.endswith('\n</orchestra-data>'):
                    raise ValueError
                frame = json.loads(content[len('<orchestra-data>\n'):-len('\n</orchestra-data>')])
                if set(frame) != {"source", "content"} or frame["source"] != source:
                    raise ValueError
                content = frame["content"]
            except (ValueError, TypeError):
                raise MessageContractBlocked("message_contract_blocked") from None
        if len(json.dumps(message.content, ensure_ascii=False)) > 32768:
            raise MessageContractBlocked("message_contract_blocked")
        for call in getattr(message, "tool_calls", []):
            if call["name"].startswith("transfer_to_") and "brief" in call["args"]:
                validate_contract("brief", call["args"]["brief"])
        if isinstance(message, ToolMessage) and message.name in {"report_to_parent", "escalate"}:
            try:
                report = validate_contract("report", json.loads(content) if isinstance(content, str) else None)
                if report.source != source:
                    raise MessageContractBlocked("message_contract_blocked")
            except (ValueError, TypeError):
                raise MessageContractBlocked("message_contract_blocked") from None
        if source in {"agent", "tool"} and content:
            data = json.dumps({"source": source, "content": content}, ensure_ascii=False)
            content = '<orchestra-data>\n' + data.replace("<", "\\u003c").replace(">", "\\u003e") + '\n</orchestra-data>'
        prepared.append(message.model_copy(update={"content": content, "additional_kwargs": {
            **message.additional_kwargs, "source": source, "orchestra_data": bool(content) and source in {"agent", "tool"},
        }}))
    return prepared
