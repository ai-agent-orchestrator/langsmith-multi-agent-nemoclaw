"""ModelFactory: 역할/별칭/민감도에 따라 모델을 만든다 (DESIGN §6.2). 모델 ID 는 코드에 두지 않는다."""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional, Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from ..runtime import EventLog
from .scripted import ScriptedChatModel


class ModelFactory(Protocol):
    def __call__(self, *, role: str, alias: str = "inherit", sensitivity: str = "public",
                 kind: str = "agent") -> BaseChatModel: ...


class ConstantModelFactory:
    """기존 build_*_app(model) 호환: 모든 역할에 같은 모델."""

    def __init__(self, model: BaseChatModel):
        self.model = model

    def __call__(self, *, role: str, alias: str = "inherit", sensitivity: str = "public",
                 kind: str = "agent") -> BaseChatModel:
        return self.model


class ScriptedModelFactory:
    """role -> ScriptedChatModel (같은 role 은 같은 인스턴스, 큐 공유)."""

    def __init__(self, scripts: dict[str, list[dict]], events: Optional[EventLog] = None, strict_tools: bool = True):
        self.scripts = scripts
        self.events = events
        self.strict_tools = strict_tools
        self._cache: dict[str, ScriptedChatModel] = {}

    def __call__(self, *, role: str, alias: str = "inherit", sensitivity: str = "public",
                 kind: str = "agent") -> BaseChatModel:
        if role not in self._cache:
            self._cache[role] = ScriptedChatModel(role, list(self.scripts.get(role, [])), self.events, kind,
                                                  strict_tools=self.strict_tools)
        return self._cache[role]

    def leftovers(self) -> dict[str, int]:
        """소비되지 않은 스크립트 턴 (시나리오가 실제 흐름과 어긋났는지 점검)."""
        return {r: m.remaining() for r, m in self._cache.items() if m.remaining()}


ALIAS_ENV = {"haiku": "ORCHESTRA_MODEL_HAIKU", "sonnet": "ORCHESTRA_MODEL_SONNET",
             "opus": "ORCHESTRA_MODEL_OPUS", "local": "ORCHESTRA_MODEL_LOCAL"}


def resolve_alias(alias: str, *, default: str = "sonnet", env: Optional[Mapping[str, str]] = None) -> str:
    """별칭 -> 모델 ID(환경변수). inherit 은 default 별칭으로. 설정이 없으면 명확한 오류."""
    env = env if env is not None else os.environ
    a = default if alias in ("", "inherit") else alias
    if a in ALIAS_ENV:
        model_id = env.get(ALIAS_ENV[a], "")
        if not model_id:
            raise RuntimeError(f"set {ALIAS_ENV[a]} to use model alias {a!r}")
        return model_id
    return a  # 원본 모델 ID 를 직접 쓴 경우


def route_for_sensitivity(sensitivity: str, *, policy: Optional[str] = None) -> str:
    """민감도 라우팅(앱 레벨 이중 방어). 'pii'/'pci' 이고 local_first 면 'local' 로 강제."""
    policy = policy if policy is not None else os.environ.get("ORCHESTRA_PRIVACY_ROUTE", "local_first")
    if policy == "local_first" and sensitivity in ("pii", "pci"):
        return "local"
    return "frontier"


class AliasModelFactory:
    """실제 모델 팩토리 골격. provider 별 생성은 Codex(M2)가 build() 안에 채운다."""

    def __init__(self, default_alias: str = "sonnet", builder: Any = None):
        self.default_alias = default_alias
        self.builder = builder  # Callable[[str model_id, str route], BaseChatModel]

    def __call__(self, *, role: str, alias: str = "inherit", sensitivity: str = "public",
                 kind: str = "agent") -> BaseChatModel:
        route = route_for_sensitivity(sensitivity)
        eff = "local" if route == "local" else alias
        model_id = resolve_alias(eff, default=self.default_alias)
        if self.builder is None:
            raise NotImplementedError("AliasModelFactory.builder 를 주입하세요 (provider 생성은 M2 작업)")
        return self.builder(model_id, route)
