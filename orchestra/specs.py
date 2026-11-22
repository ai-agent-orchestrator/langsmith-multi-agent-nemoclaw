"""선언형 스펙: 툴/에이전트/팀/조직/필터. 코어는 도메인을 모른다 (DESIGN §5)."""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

STAGES = ("pre_request", "pre_model", "pre_tool", "post_tool", "pre_response")
StageName = Literal["pre_request", "pre_model", "pre_tool", "post_tool", "pre_response"]


class SideEffect(str, Enum):
    READ = "read"        # 조회
    RESERVE = "reserve"  # 가역적 점유 (hold, TTL)
    COMMIT = "commit"    # 비가역/금전
    EXEC = "exec"        # 코드 실행·파일 쓰기·셸


class EgressRule(BaseModel):
    """NemoClaw preset 생성의 원천. 한 호스트에 대한 L7 허용 규칙."""

    host: str
    port: int = 443
    protocol: Literal["rest", "websocket", "json-rpc", "mcp"] = "rest"
    rules: list[dict[str, str]] = Field(default_factory=list)  # [{"method": "POST", "path": "/v1/x/**"}]

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}"


class ToolSpec(BaseModel):
    name: str
    side_effect: SideEffect = SideEffect.READ
    egress: list[EgressRule] = Field(default_factory=list)
    sensitivity: Literal["public", "internal", "pii", "pci"] = "public"
    sandbox: Literal["never", "preferred", "required"] = "never"
    sandbox_name: Optional[str] = None
    timeout_s: float = 30.0
    idempotent: bool = True  # False면 idempotency_key 를 필터가 주입

    @model_validator(mode="after")
    def _sandbox_needs_name(self) -> "ToolSpec":
        if self.sandbox != "never" and not self.sandbox_name:
            raise ValueError(f"tool {self.name}: sandbox={self.sandbox} requires sandbox_name")
        return self


class AgentSpec(BaseModel):
    id: str                       # 전역 유일: "travel/hotel_agent", "pr-review-toolkit/code-reviewer"
    name: str                     # 그래프 노드명 (식별자). 전역 유일
    prompt: str = ""              # 본문 | "file:<path>"
    tools: list[str] = Field(default_factory=list)
    model: str = "inherit"
    description: str = ""
    source: Optional[str] = None
    max_tools: int = 3
    max_tool_calls: int = 8
    handoff_to: list[str] = Field(default_factory=list)  # 같은 팀 형제 AgentSpec.id

    @field_validator("name")
    @classmethod
    def _name_ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"agent name must be an identifier, got {v!r}")
        return v

    @model_validator(mode="after")
    def _tool_cap(self) -> "AgentSpec":
        if len(self.tools) > self.max_tools:
            raise ValueError(f"agent {self.id}: {len(self.tools)} tools > max_tools={self.max_tools}")
        return self


class TeamSpec(BaseModel):
    name: str
    prompt: str = ""
    members: list[str]            # AgentSpec.id 또는 TeamSpec.name
    model: str = "inherit"
    output_mode: Literal["last_message", "full_history"] = "last_message"
    max_fanout: int = Field(default=5, ge=2, le=8)
    parallel: bool = False        # 구현은 M6(Codex): Send 팬아웃
    reducer: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"team name must be an identifier, got {v!r}")
        return v

    @field_validator("members")
    @classmethod
    def _members_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("team needs at least one member")
        if len(set(v)) != len(v):
            raise ValueError("duplicate members")
        return v


class FilterSpec(BaseModel):
    name: str
    stages: list[StageName]
    params: dict = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=lambda: ["*"])
    order: int = 100
    enabled: bool = True


class OrgSpec(BaseModel):
    name: str
    root: str
    teams: dict[str, TeamSpec]
    agents: dict[str, AgentSpec] = Field(default_factory=dict)  # resolve 단계에서 도메인 팩이 채움
    domains: list[str]
    filters: list[FilterSpec] = Field(default_factory=list)
    recursion_limit: Optional[int] = None
