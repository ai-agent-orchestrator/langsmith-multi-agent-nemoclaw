"""Human-in-the-loop: 승인 요청(interrupt) / 승인 키.

주의(LangGraph 규칙): interrupt() 가 재개되면 해당 노드가 처음부터 다시 실행된다.
따라서 interrupt 호출 앞쪽 코드는 부작용이 없어야 한다 (필터 체인·키 계산만 둔다).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def approval_key(tool: str, args: dict[str, Any]) -> str:
    """같은 툴 + 같은 인자 = 같은 승인 키 (인자 순서 무관)."""
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
    return "apv_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def request(payload: dict[str, Any]) -> bool:
    """사용자 승인을 요청한다. 재개 값: True/False 또는 {"approved": bool, ...}."""
    from langgraph.types import interrupt

    answer = interrupt(payload)
    if isinstance(answer, dict):
        return bool(answer.get("approved"))
    return bool(answer)
