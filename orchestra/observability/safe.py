from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("orchestra.observability")


def safe_call(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """LangSmith 호출 실패는 경고만 남기고 default 반환 (DESIGN §9.1 실패 정책). 평가 '판정'에는 쓰지 않는다."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("langsmith call failed: %s: %s", type(exc).__name__, exc)
        return default
