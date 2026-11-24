"""4개 구조를 같은 인터페이스로 쓰는 서비스 진입점 (보안 게이트 + 트레이싱 + 가드 적용)."""
from __future__ import annotations

from typing import Any, Optional

from .observability import RECURSION_LIMITS, apply_guards, enforce_closed_network, guarded_invoke, security_mode


def _builders() -> dict[str, Any]:
    from .handoff_graph import build_handoff_app
    from .hierarchical_graph import build_hierarchical_app
    from .hybrid_graph import build_hybrid_app
    from .supervisor_graph import build_supervisor_app

    return {
        "supervisor": build_supervisor_app,
        "handoff": build_handoff_app,
        "hybrid": build_hybrid_app,
        "hierarchical": build_hierarchical_app,
    }


class TravelService:
    """TravelService("hierarchical", model).ask("이번 주말 제주도 맛집 투어 ...", thread_id="u1")"""

    def __init__(self, pattern: str, model: Any = None, *, app: Any = None, mode: Optional[str] = None,
                 step_timeout: Optional[float] = 60.0):
        if pattern not in RECURSION_LIMITS:
            raise ValueError(f"unknown pattern {pattern!r}; choose from {sorted(RECURSION_LIMITS)}")
        self.pattern = pattern
        self.mode = security_mode(mode)
        if self.mode == "closed":
            enforce_closed_network()
        # app 을 직접 주면(테스트/스텁) 그대로 사용, 아니면 구조별 빌더로 생성
        self.app = apply_guards(app or _builders()[pattern](model), step_timeout=step_timeout)

    def ask(self, question: str, thread_id: Optional[str] = None) -> dict:
        return guarded_invoke(self.app, question, pattern=self.pattern, thread_id=thread_id, mode=self.mode)
