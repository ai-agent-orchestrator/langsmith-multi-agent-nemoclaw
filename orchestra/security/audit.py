"""해시 체인 감사 로그 (O3 / S8). 한 줄이라도 바뀌면 verify 가 실패한다.

레코드: {seq, ts, kind, data, prev, hash}; hash = SHA256(prev + canonical_json(seq, ts, kind, data)).
키(`key`)를 주면 HMAC-SHA256 으로 서명 → 로그 전체를 다시 계산해 위조하는 공격까지 막는다(키는 호스트에만; 샌드박스에 두지 않는다).
한계: 체인의 **마지막 해시(head)** 를 별도 위치(다른 시스템)에 보관하지 않으면 꼬리 잘라내기(truncation)는 탐지할 수 없다.
→ `verify(expected_head=...)` 로 외부에 저장한 head 와 비교한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Iterable, Optional

GENESIS = "0" * 64
AUDIT_KINDS = ("approval", "blocked", "sandbox", "filter", "handoff", "report")   # EventLog 에서 옮길 종류


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


class AuditLog:
    def __init__(self, key: Optional[bytes] = None):
        self.key = key
        self.records: list[dict[str, Any]] = []

    @property
    def head(self) -> str:
        return self.records[-1]["hash"] if self.records else GENESIS

    def _digest(self, prev: str, body: dict) -> str:
        msg = prev.encode() + _canon(body)
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest() if self.key else hashlib.sha256(msg).hexdigest()

    def append(self, kind: str, data: dict[str, Any], ts: float = 0.0) -> dict[str, Any]:
        body = {"seq": len(self.records), "ts": ts, "kind": kind, "data": data}
        rec = {**body, "prev": self.head, "hash": self._digest(self.head, body)}
        self.records.append(rec)
        return rec

    def verify(self, expected_head: Optional[str] = None) -> list[str]:
        """문제 목록(비어 있으면 정상)."""
        problems, prev = [], GENESIS
        for i, r in enumerate(self.records):
            body = {k: r[k] for k in ("seq", "ts", "kind", "data")}
            if r.get("seq") != i:
                problems.append(f"record {i}: seq mismatch")
            if r.get("prev") != prev:
                problems.append(f"record {i}: prev hash mismatch (reordered/removed/inserted)")
            if not hmac.compare_digest(str(r.get("hash")), self._digest(prev, body)):
                problems.append(f"record {i}: hash mismatch (content modified or wrong key)")
            prev = r.get("hash", "")
        if expected_head is not None and not hmac.compare_digest(self.head, expected_head):
            problems.append("head mismatch (log truncated or replaced)")
        return problems

    # ------------------------------------------------------------ 입출력
    def dump(self, path: str | Path) -> None:
        Path(path).write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in self.records) + "\n",
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, key: Optional[bytes] = None) -> "AuditLog":
        log = cls(key)
        log.records = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        return log


def from_events(events: Iterable[Any], key: Optional[bytes] = None, kinds: tuple[str, ...] = AUDIT_KINDS) -> AuditLog:
    """EventLog 이벤트 중 감사 대상만 체인에 옮긴다. 개인정보·식별자 최소화:
    짧은 스칼라(문자열 ≤120자/숫자/불리언)만 그대로 두고, 중첩 값(args, artifacts 등)은 `<키>_sha256` 해시로 대체한다.
    같은 입력이면 같은 해시이므로 '무엇이 승인됐는지' 사후 대조는 가능하되 원문은 로그에 남지 않는다."""
    log = AuditLog(key)
    for e in events:
        if e.kind not in kinds:
            continue
        data: dict[str, Any] = {}
        for k, v in e.data.items():
            if isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 120):
                data[k] = v
            else:
                data[f"{k}_sha256"] = hashlib.sha256(_canon(v)).hexdigest()
        log.append(e.kind, data, ts=float(getattr(e, "seq", 0)))
    return log
