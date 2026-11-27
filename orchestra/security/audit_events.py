"""Runtime 이벤트를 감사 로그에 넣기 전 최소화한다: 식별자 형태의 정해진 값만 남기고 나머지는 digest 로."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

_PLAIN_FIELDS = frozenset({
    "tool", "agent", "reason", "by", "stage", "ok", "approved", "error", "sandbox", "policy_revision", "filter",
    "action", "route", "mode", "team", "status", "source", "target", "role", "node_kind", "exhausted", "reason_code",
})
# 자유 문장(모델이 쓴 사유 등)은 허용 필드여도 원문으로 남기지 않는다.
_CODE = re.compile(r"[A-Za-z0-9_:./\-]{0,128}")


class AuditUnavailable(RuntimeError):
    """감사 기록에 실패하면 이벤트도 남기지 않고 중단한다. 원인 문자열은 담지 않는다."""


def _digestible(value: Any) -> list[str]:
    # AuditLog 는 중첩 값을 keyed digest 로 바꾸므로 list 로 감싼다.
    try:
        return [json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)]
    except (TypeError, ValueError):
        return [repr(value)]


def audit_record(kind: str, seq: int, data: Mapping[str, Any]) -> dict[str, Any]:
    """AuditLog.append 에 넘길 최소화된 레코드를 만든다."""
    record: dict[str, Any] = {}
    for name, value in data.items():
        plain = name in _PLAIN_FIELDS and (value is None or isinstance(value, (bool, int))
                                           or isinstance(value, str) and _CODE.fullmatch(value) is not None)
        record[str(name)] = value if plain else _digestible(value)
    record.update(kind=kind, seq=seq)
    return record
