"""SQLite-serialized, HMAC-authenticated hash chain with minimized audit payloads."""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from ..observability.masking import mask_any, mask_text


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class AuditLog:
    """One chain per DB; keep an external head checkpoint to detect tail truncation."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("audit key must contain at least 32 bytes")
        self.path, self._key = path, key
        with closing(self._db()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, previous TEXT, data TEXT, signature TEXT)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def _signature(self, seq: int, previous: str, data: str) -> str:
        return hmac.new(self._key, _json([seq, previous, data]).encode(), hashlib.sha256).hexdigest()

    def append(self, event: dict[str, Any]) -> str:
        """Preserve masked finite scalars; replace nested data with a keyed digest."""
        safe = {}
        for name, value in event.items():
            if not isinstance(name, str):
                raise ValueError("invalid audit key")
            if value is None or type(value) in {str, int, bool} or type(value) is float and math.isfinite(value):
                safe[mask_text(name)] = next(iter(mask_any({name: value}).values()))
            else:
                safe[mask_text(name)] = {"hmac_sha256": hmac.new(self._key, _json(value).encode(), hashlib.sha256).hexdigest()}
        data = _json(safe)
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            tail = db.execute("SELECT seq, signature FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, previous = (tail[0] + 1, tail[1]) if tail else (1, "0" * 64)
            signature = self._signature(seq, previous, data)
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (seq, previous, data, signature))
        return signature

    def verify(self, *, expected_head: str | None = None) -> bool:
        """Reject edits, holes, reordered rows, wrong keys and checkpointed truncation."""
        previous, seq = "0" * 64, 0
        with closing(self._db()) as db:
            for current, parent, data, signature in db.execute("SELECT * FROM audit ORDER BY seq"):
                if current != seq + 1 or parent != previous or not hmac.compare_digest(
                        signature, self._signature(current, parent, data)):
                    return False
                previous, seq = signature, current
        return expected_head is None or hmac.compare_digest(previous, expected_head)
