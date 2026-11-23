"""코어 내장 필터. 도메인 전용 필터는 domains/*/filters.py 에 둔다."""
from __future__ import annotations

from typing import Callable

from ..base import Filter
from .budget import budget
from .code_guard import code_guard
from .pii_mask import pii_mask
from .request_gate import request_gate
from .side_effect_policy import side_effect_policy

BUILTIN_FILTERS: dict[str, Callable[..., Filter]] = {
    "request_gate": request_gate,
    "pii_mask": pii_mask,
    "side_effect_policy": side_effect_policy,
    "budget": budget,
    "code_guard": code_guard,
}
