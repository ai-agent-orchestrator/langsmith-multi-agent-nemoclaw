"""L0 요청 게이트: 대량 개인정보 조회 / 프롬프트 인젝션 / 비밀 유출 요청 차단.

규칙은 travel_planner.observability.BLOCK_RULES 와 동일하다 (코어는 travel_planner 를 import 하지 않는다).
"""
from __future__ import annotations

import re

from ..base import Filter, FilterContext, FilterDecision

_PII = r"(주민\s?(?:등록)?번호|계좌\s?번호|카드\s?번호|비밀번호|신용카드)"
_BULK = r"(전체|모든|전부|덤프|테이블|dump|select\s+\*)"
BLOCK_RULES = [
    ("bulk_pii_request", re.compile(rf"(?s)(?:{_BULK}.*{_PII}|{_PII}.*{_BULK})", re.I)),
    (
        "prompt_injection",
        re.compile(
            r"(이전|위의?)\s*(지시|명령|프롬프트).{0,10}(무시|잊)|ignore\s+(all\s+)?(previous|prior)\s+instructions|system\s*prompt",
            re.I,
        ),
    ),
    ("secret_exfiltration", re.compile(r"(api\s*key|api\s*키|토큰|secret).{0,10}(알려|출력|보여|print|show)", re.I)),
]


def check_text(text: str) -> str | None:
    for name, pat in BLOCK_RULES:
        if pat.search(text):
            return name
    return None


def request_gate() -> Filter:
    class _Gate:
        name = "request_gate"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            hit = check_text(str(ctx.payload))
            return FilterDecision.block(hit, security_gate=0.0) if hit else FilterDecision.allow()

    return _Gate()
