"""실모델 provider 생성 (DESIGN §6.2). 라우트·샌드박스 경계는 생성자를 부르기 전에 검증한다."""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from langchain_core.language_models.chat_models import BaseChatModel

Constructor = Callable[..., BaseChatModel]

PROVIDERS = ("anthropic", "openai_compatible")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SANDBOX_INFERENCE_HOST = "inference.local"
# 샌드박스 안에서는 자격증명이 게이트웨이에만 있어야 하므로 이 중 하나라도 보이면 경계가 깨진 것이다.
PROVIDER_KEY_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "NVIDIA_API_KEY")
# SDK 가 빈 키를 거부해서 넣는 자리값. 비밀이 아니다.
DUMMY_API_KEY = "unused"


def _openai_compatible(**kwargs: Any) -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("openai_compatible provider needs the optional dependency: pip install -e '.[models]'") from exc
    if "http_socket_options" in ChatOpenAI.model_fields:
        # 기본 소켓 옵션은 httpx 의 프록시 자동 감지를 끈다. 샌드박스의 inference.local 은 프록시를 거쳐야 한다.
        kwargs["http_socket_options"] = ()
    if urlsplit(kwargs["base_url"]).hostname in LOOPBACK_HOSTS:
        import httpx

        # 프록시 환경변수가 loopback 요청(민감 데이터)을 밖으로 돌리지 못하게 한다.
        kwargs.update(http_client=httpx.Client(trust_env=False),
                      http_async_client=httpx.AsyncClient(trust_env=False), openai_proxy=None)
    return ChatOpenAI(use_responses_api=False, **kwargs)


def _anthropic(**kwargs: Any) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(**kwargs)


def _checked_url(env: Mapping[str, str], name: str, hosts: frozenset[str]) -> str:
    """env[name] 이 허용 호스트를 가리키는 평범한 http(s) URL 인지 확인한다. 오류에 URL 원문을 넣지 않는다."""
    url = env.get(name, "").strip()
    if not url:
        raise ValueError(f"set {name} to use this model route")
    try:
        parts = urlsplit(url)
        _ = parts.port  # 범위를 벗어난 포트는 여기서 ValueError
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid URL") from exc
    plain = parts.username is None and parts.password is None and not parts.query and not parts.fragment
    if parts.scheme not in ("http", "https") or not plain or "\\" in url or parts.hostname not in hosts:
        raise ValueError(f"{name} must be a plain http(s) URL on one of: {', '.join(sorted(hosts))}")
    return url


class ProviderBuilder:
    """`AliasModelFactory.builder` 구현: (model_id, route) -> BaseChatModel.

    - `ORCHESTRA_SANDBOX=nemoclaw*`: openai_compatible 로 `inference.local` 만 쓰고, provider 키가 있으면 거부한다.
    - route `local`: openai_compatible 로 loopback 인 `ORCHESTRA_LOCAL_BASE_URL` 만 쓴다.
    - 그 외: `ORCHESTRA_MODEL_PROVIDER`(기본 anthropic)를 따른다.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None,
                 constructors: Optional[Mapping[str, Constructor]] = None):
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.constructors: dict[str, Constructor] = {
            "anthropic": _anthropic, "openai_compatible": _openai_compatible, **(constructors or {})}

    def __call__(self, model_id: str, route: str) -> BaseChatModel:
        """경계를 검증한 뒤 모델을 만든다. 위반이면 생성자를 부르지 않고 ValueError."""
        if not model_id:
            raise ValueError("model id is required")
        if route not in ("local", "frontier"):
            raise ValueError("unknown model route")
        provider = self.env.get("ORCHESTRA_MODEL_PROVIDER", "").strip() or "anthropic"
        sandboxed = self.env.get("ORCHESTRA_SANDBOX", "").strip().lower().startswith("nemoclaw")
        metadata = {"model_route": route}
        if sandboxed:
            if any(self.env.get(name) for name in PROVIDER_KEY_ENVS):
                raise ValueError("provider API keys must not be present in nemoclaw sandbox mode")
            base_url = _checked_url(self.env, "NEMOCLAW_INFERENCE_BASE_URL", frozenset({SANDBOX_INFERENCE_HOST}))
        elif route == "local" or provider == "openai_compatible":
            base_url = _checked_url(self.env, "ORCHESTRA_LOCAL_BASE_URL", LOOPBACK_HOSTS)
        elif provider == "anthropic":
            key = self.env.get("ANTHROPIC_API_KEY", "")
            extra = {"api_key": key} if key else {}
            return self.constructors["anthropic"](model=model_id, metadata=metadata, **extra)
        else:
            raise ValueError(f"ORCHESTRA_MODEL_PROVIDER must be one of: {', '.join(PROVIDERS)} "
                             "(scripted models come from ScriptedModelFactory)")
        return self.constructors["openai_compatible"](model=model_id, base_url=base_url, api_key=DUMMY_API_KEY,
                                                      metadata=metadata)
