"""출력 마스킹(post_tool / pre_response). 구조(dict/list)는 유지하고 문자열만 치환한다.

travel_planner.observability.MASK_RULES + 결제(PCI) 규칙. idempotency_key / intent_id 같은 식별자는 건드리지 않는다.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Filter, FilterContext, FilterDecision

MASK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d{6}-[1-4]\d{6}"), "[REDACTED_ID]"),
    (re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"), "[REDACTED_CARD]"),
    (re.compile(r"(?i)\b(cvc|cvv|보안코드)\s*[:=]?\s*\d{3,4}\b"), "[REDACTED_CVC]"),
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED_EMAIL]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)((?:password|passwd|api[_-]?key|secret|비밀번호)\s*[:=]\s*)\S+"), r"\1[REDACTED_SECRET]"),
]


def mask_text(text: str) -> str:
    for pat, rep in MASK_RULES:
        text = pat.sub(rep, text)
    return text


def mask_any(data: Any) -> Any:
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {k: mask_any(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_any(v) for v in data]
    return data


def pii_mask() -> Filter:
    class _Mask:
        name = "pii_mask"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            masked = mask_any(ctx.payload)
            return FilterDecision.modify(masked, "masked") if masked != ctx.payload else FilterDecision.allow()

    return _Mask()
