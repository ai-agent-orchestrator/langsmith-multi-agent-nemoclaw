"""LangSmith 연동: 보안 모드(마스킹/폐쇄망) + 트레이싱 설정 + 입력 보안 게이트.

기존 4개 구조(supervisor / handoff / hybrid / hierarchical)를 수정하지 않고
컴파일된 그래프를 바깥에서 감싸는 방식으로 연결한다.

보안 모드 (환경변수 TRAVEL_SECURITY_MODE, 기본 masked)
- off     : 마스킹 없이 LangSmith 로 트레이스 전송 (개발용)
- masked  : 전송 직전 로컬에서 민감정보를 치환한 뒤 전송 (anonymizer)
- closed  : 트레이싱 완전 차단. 외부 LangSmith 로 아무것도 전송하지 않음 (폐쇄망)
"""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from langsmith import Client, traceable, tracing_context
from langsmith.anonymizer import create_anonymizer

from .safe import safe_call
from .client import SafeClient
from orchestra.filters.builtin.pii_mask import mask_any

from orchestra.filters.builtin.pii_mask import LEGACY_MASK_RULES as MASK_RULES
from orchestra.filters.builtin.request_gate import BLOCK_RULES as BLOCK_RULES, check_text

MODES = ("off", "masked", "closed")

# 구조별 재귀 한도: 계층형은 호출 단계가 깊으므로 더 크게 둔다.
RECURSION_LIMITS = {"supervisor": 25, "handoff": 25, "hybrid": 30, "hierarchical": 40}

# ---------------------------------------------------------------- 마스킹
# 중첩 dict/list 구조를 유지한 채 문자열 노드만 치환한다.
# (str(data) 전체를 {"masked_payload": ...} 로 바꾸면 트레이스 구조가 망가진다.)
_anonymize = safe_call(create_anonymizer, MASK_RULES, default=mask_any)


def _listify(data: Any) -> Any:
    """튜플은 anonymizer 가 순회하지 않으므로 리스트로 바꿔 둔다 (예: messages=[("user", "...")])."""
    if isinstance(data, (list, tuple)):
        return [_listify(x) for x in data]
    if isinstance(data, dict):
        return {k: _listify(v) for k, v in data.items()}
    return data


def masker(data: Any) -> Any:
    return _anonymize(_listify(data))


def mask_text(text: str) -> str:
    for rule in MASK_RULES:
        text = rule["pattern"].sub(rule["replace"], text)
    return text


def _mask_io(data: Any) -> Any:
    return masker(data)


# ---------------------------------------------------------------- 모드/클라이언트
def security_mode(mode: Optional[str] = None) -> str:
    mode = (mode or os.getenv("TRAVEL_SECURITY_MODE") or "masked").lower()
    if mode not in MODES:
        raise ValueError(f"TRAVEL_SECURITY_MODE must be one of {MODES}, got {mode!r}")
    return mode


def tracing_requested() -> bool:
    return os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")).lower() in ("true", "1")


def make_client(mode: Optional[str] = None, **client_kwargs: Any) -> Any:
    """모드에 맞는 LangSmith Client. closed 모드는 None (클라이언트 자체를 만들지 않음)."""
    mode = security_mode(mode)
    if mode == "closed":
        return None
    if mode == "masked":
        return SafeClient(safe_call(Client, anonymizer=masker, **client_kwargs), mode)
    return SafeClient(safe_call(Client, **client_kwargs), mode)


def enforce_closed_network() -> None:
    """closed 모드용 방어선: 트레이싱 env 를 끄고, LangSmith 엔드포인트를 로컬 불통 주소로 돌린다.

    evaluate() 등이 내부에서 만드는 기본 Client 가 외부 api.smith.langchain.com 으로
    /info 조회조차 시도하지 못하게 한다. (사내 self-hosted 를 쓰려면 closed 대신 masked 사용)
    """
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_ENDPOINT"] = "http://127.0.0.1:9"
    os.environ["LANGCHAIN_ENDPOINT"] = "http://127.0.0.1:9"


@contextmanager
def tracing_scope(mode: Optional[str] = None, client: Optional[Client] = None) -> Iterator[None]:
    """closed 면 이 블록 안의 모든 트레이싱을 끄고, 아니면 모드별 client 를 사용한다."""
    mode = security_mode(mode)
    if mode == "closed" or not tracing_requested():
        with safe_call(tracing_context, enabled=False, default=nullcontext()):
            yield
    else:
        with safe_call(tracing_context, enabled=True, client=client or make_client(mode), default=nullcontext()):
            yield


# ---------------------------------------------------------------- 실행 설정
def run_config(
    pattern: str,
    thread_id: Optional[str] = None,
    *,
    mode: Optional[str] = None,
    client: Optional[Client] = None,
    extra_tags: tuple[str, ...] = (),
    extra_metadata: Optional[dict] = None,
) -> dict:
    """그래프 invoke 에 넘길 config: run_name/tags/metadata + recursion_limit + (필요 시) 마스킹 트레이서.

    LangSmith 에서 `pattern:*` 태그나 metadata.pattern 으로 구조별 비용·지연을 비교할 수 있다.
    """
    if pattern not in RECURSION_LIMITS:
        raise ValueError(f"unknown pattern {pattern!r}; choose from {sorted(RECURSION_LIMITS)}")
    mode = security_mode(mode)
    cfg: dict[str, Any] = {
        "run_name": f"travel-{pattern}",
        "tags": [f"pattern:{pattern}", f"security:{mode}", *extra_tags],
        "metadata": {"pattern": pattern, "thread_id": thread_id, "service": "travel-planner", **(extra_metadata or {})},
        "recursion_limit": RECURSION_LIMITS[pattern],
    }
    if thread_id:
        cfg["configurable"] = {"thread_id": thread_id}
    if mode != "closed" and tracing_requested():
        from langchain_core.tracers.langchain import LangChainTracer

        cfg["callbacks"] = [
            safe_call(LangChainTracer, client=client or make_client(mode), project_name=os.getenv("LANGSMITH_PROJECT"))
        ]
    return cfg


def apply_guards(app: Any, *, step_timeout: Optional[float] = 60.0) -> Any:
    """노드 1 step 당 타임아웃 (무한 대기/재시도 폭주 방지). 지원하지 않는 그래프면 그대로 반환."""
    if step_timeout is not None and hasattr(app, "step_timeout"):
        app.step_timeout = step_timeout
    return app


# ---------------------------------------------------------------- 입력 보안 게이트
@dataclass(frozen=True)
class GateResult:
    blocked: bool
    reason: str = ""


def security_gate(question: str) -> GateResult:
    """에이전트에 도달하기 전에 대량 개인정보 조회/프롬프트 인젝션/비밀 유출 요청을 차단한다."""
    reason = check_text(question)
    return GateResult(bool(reason), reason or "")


def _last_text(result: Any) -> str:
    msgs = result.get("messages") if isinstance(result, dict) else None
    if not msgs:
        return str(result)
    last = msgs[-1]
    return getattr(last, "content", None) or (last.get("content", "") if isinstance(last, dict) else str(last))


@safe_call(traceable, name="travel_request", run_type="chain", process_inputs=_mask_io, process_outputs=_mask_io,
           default=lambda fn: fn)
def _handle(question: str, app: Any, pattern: str, thread_id: Optional[str], mode: str) -> dict:
    gate = security_gate(question)
    if gate.blocked:
        return {"answer": f"차단: 보안 정책 위반 요청입니다 ({gate.reason})", "blocked": True, "reason": gate.reason}
    cfg = run_config(pattern, thread_id, mode=mode)
    result = app.invoke({"messages": [("user", question)]}, cfg)
    return {"answer": _last_text(result), "blocked": False, "reason": ""}


def guarded_invoke(
    app: Any,
    question: str,
    *,
    pattern: str,
    thread_id: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    """게이트 → (통과 시) 트레이싱 설정이 붙은 그래프 실행. 어떤 구조의 app 이든 동일하게 쓴다."""
    mode = security_mode(mode)
    with tracing_scope(mode):
        return _handle(question, app, pattern, thread_id, mode)
