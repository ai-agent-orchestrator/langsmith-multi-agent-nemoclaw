"""Masked ledger storage; SQLite transactions cover local mock payment and approval consumption."""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator, Protocol, ContextManager

from .hitl import ApprovalRecord

SCHEMA_VERSION = 1


def mask_any(value: Any) -> Any:
    """Use the existing pii_mask observation boundary after runtime imports finish."""
    from .observability.masking import mask_any as mask

    return mask(value)


class LedgerConfigurationError(RuntimeError):
    """Configuration/storage errors never include SQL parameters, paths, or secrets."""


class LedgerStore(Protocol):
    """Storage contract shared by approvals, event logs and mock payment state."""

    persistent: bool
    key_id: str

    def transaction(self) -> ContextManager[None]: ...
    def token(self, domain: str, value: str) -> str: ...
    def grant(self, record: ApprovalRecord, now: float) -> bool: ...
    def authorized(self, record: ApprovalRecord, now: float) -> bool: ...
    def claim(self, record: ApprovalRecord, now: float) -> bool: ...
    def revoke(self, record: ApprovalRecord) -> None: ...
    def cleanup_expired(self, now: float) -> int: ...
    def get_state(self, name: str) -> dict: ...
    def put_state(self, name: str, value: dict) -> None: ...
    def append_event(self, scope: str, kind: str, data: dict, dedupe_key: str | None = None) -> dict | None: ...
    def read_events(self, scope: str) -> list[dict]: ...


def _canonical(record: ApprovalRecord) -> str:
    # The live model has already validated the full C17 tuple. Never serialize it to storage.
    return json.dumps({"tool": record.tool, "args": json.loads(record.args_json), "user_id": record.user_id,
                       "amount": record.amount, "expires_at": record.expires_at, "request_id": record.request_id},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _display(record: ApprovalRecord) -> dict:
    return mask_any({"tool": record.tool, "args": "[REDACTED_ARGUMENTS]", "user": "[REDACTED_USER]",
                     "amount": record.amount, "request": "[REDACTED_REQUEST]"})


class MemoryLedgerStore:
    """Default isolated process memory, with reentrant transactions and rollback."""

    persistent = False
    key_id = "memory"

    def __init__(self, *, event_hook: Callable[[dict], None] | None = None):
        self._lock = threading.RLock()
        self._approvals: dict[str, dict] = {}
        self._states: dict[str, dict] = {}
        self._events: dict[str, list[dict]] = {}
        self.event_hook = event_hook or (lambda event: None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize memory writes and restore all ledger state if the operation fails."""
        with self._lock:
            snapshot = deepcopy((self._approvals, self._states, self._events))
            try:
                yield
            except BaseException:
                self._approvals, self._states, self._events = snapshot
                raise

    def token(self, domain: str, value: str) -> str:
        """Create a nonpersistent index; memory mode requires no key."""
        return hashlib.sha256((domain + "\0" + value).encode()).hexdigest()

    def _binding(self, record: ApprovalRecord) -> str:
        return self.token("approval", _canonical(record))

    def grant(self, record: ApprovalRecord, now: float) -> bool:
        """Insert an approval once; expired or previously consumed tuples cannot be renewed."""
        with self.transaction():
            key = self._binding(record)
            if record.expires_at <= now or key in self._approvals:
                return False
            self._approvals[key] = {"expires_at": record.expires_at, "consumed": False}
            return True

    def authorized(self, record: ApprovalRecord, now: float) -> bool:
        """Read-only expiry and consumption check."""
        with self._lock:
            row = self._approvals.get(self._binding(record))
            return bool(row and not row["consumed"] and row["expires_at"] > now)

    def claim(self, record: ApprovalRecord, now: float) -> bool:
        """Consume exactly one authorization under the store transaction."""
        with self.transaction():
            if not self.authorized(record, now):
                return False
            self._approvals[self._binding(record)]["consumed"] = True
            return True

    def revoke(self, record: ApprovalRecord) -> None:
        """Keep a tombstone so denial/consumption cannot be replayed."""
        with self.transaction():
            self._approvals[self._binding(record)] = {"expires_at": record.expires_at, "consumed": True}

    def cleanup_expired(self, now: float) -> int:
        """Delete expired metadata; callers must still reject expired live records."""
        with self.transaction():
            keys = [k for k, v in self._approvals.items() if v["expires_at"] <= now]
            for key in keys:
                del self._approvals[key]
            return len(keys)

    def get_state(self, name: str) -> dict:
        """Return an isolated copy of a named component's state."""
        with self._lock:
            return deepcopy(self._states.get(name, {}))

    def put_state(self, name: str, value: dict) -> None:
        """Save component state under the same transaction as approval consumption."""
        with self._lock:
            self._states[name] = deepcopy(value)

    def append_event(self, scope: str, kind: str, data: dict, dedupe_key: str | None = None) -> dict | None:
        """Append once, with one masked no-op-by-default signature/chain hook."""
        with self.transaction():
            events = self._events.setdefault(scope, [])
            if dedupe_key is not None and any(e["dedupe"] == dedupe_key for e in events):
                return None
            event = {"seq": len(events), "kind": kind, "data": mask_any(data), "dedupe": dedupe_key}
            self.event_hook(deepcopy(mask_any(event)))
            events.append(event)
            return deepcopy(event)

    def read_events(self, scope: str) -> list[dict]:
        """Reload masked events for a stable scope."""
        with self._lock:
            return deepcopy(self._events.get(scope, []))


class SQLiteLedgerStore(MemoryLedgerStore):
    """SQLite schema v1; all writes are masked before reaching SQL or the journal."""

    persistent = True

    def __init__(self, path: str | Path, *, event_hook: Callable[[dict], None] | None = None):
        secret = os.environ.get("ORCHESTRA_LEDGER_HMAC_KEY", "")
        if len(secret.encode()) < 32:
            raise LedgerConfigurationError("SQLite ledger requires a HMAC key of at least 32 bytes")
        self._key = secret.encode()
        self.key_id = os.environ.get("ORCHESTRA_LEDGER_KEY_ID") or self.token("key-id", "v1")[:16]
        if not self.key_id.isascii() or not self.key_id.replace("-", "").replace("_", "").isalnum() or len(self.key_id) > 64:
            raise LedgerConfigurationError("invalid ledger key ID")
        self.path = Path(path)
        self._local = threading.local()
        self.event_hook = event_hook or (lambda event: None)
        try:
            with closing(sqlite3.connect(self.path, timeout=30)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise LedgerConfigurationError("unsupported ledger schema version")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS approvals (
                        binding_hash TEXT PRIMARY KEY, user_hash TEXT NOT NULL,
                        expires_at REAL NOT NULL, consumed INTEGER NOT NULL,
                        display TEXT NOT NULL, schema_version INTEGER NOT NULL, key_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS component_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                        data TEXT NOT NULL, dedupe TEXT, key_id TEXT NOT NULL, UNIQUE(scope, dedupe));
                    PRAGMA user_version=1;
                """)
        except sqlite3.Error:
            raise LedgerConfigurationError("ledger initialization failed") from None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Join same-thread nested operations; serialize competing writers with BEGIN IMMEDIATE."""
        if getattr(self._local, "connection", None) is not None:
            yield
            return
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            self._local.connection = conn
            conn.execute("BEGIN IMMEDIATE")
            yield
            conn.commit()
        except sqlite3.Error:
            if conn:
                conn.rollback()
            raise LedgerConfigurationError("ledger transaction failed") from None
        except BaseException:
            if conn:
                conn.rollback()
            raise
        finally:
            self._local.connection = None
            if conn:
                conn.close()

    def token(self, domain: str, value: str) -> str:
        """Compute domain-separated full-length HMAC-SHA256; never expose key material."""
        return hmac.new(self._key, (domain + "\0" + value).encode(), hashlib.sha256).hexdigest()

    def grant(self, record: ApprovalRecord, now: float) -> bool:
        """Only validated live records can create keyed, masked grant metadata."""
        if record.expires_at <= now:
            return False
        with self.transaction():
            return self._local.connection.execute(
                "INSERT OR IGNORE INTO approvals VALUES (?,?,?,0,?,?,?)",
                (self._binding(record), self.token("user", record.user_id), record.expires_at,
                 json.dumps(_display(record), ensure_ascii=False), SCHEMA_VERSION, self.key_id)).rowcount == 1

    def authorized(self, record: ApprovalRecord, now: float) -> bool:
        """Recompute the full HMAC from transient raw arguments; no writes on approval lookup."""
        connection = getattr(self._local, "connection", None)
        own = connection is None
        try:
            connection = connection or sqlite3.connect(self.path, timeout=30)
            row = connection.execute("SELECT consumed, expires_at, user_hash, key_id FROM approvals WHERE binding_hash=?",
                                     (self._binding(record),)).fetchone()
            return bool(row and not row[0] and row[1] > now and row[3] == self.key_id
                        and hmac.compare_digest(row[2], self.token("user", record.user_id)))
        except sqlite3.Error:
            raise LedgerConfigurationError("ledger lookup failed") from None
        finally:
            if own and connection:
                connection.close()

    def claim(self, record: ApprovalRecord, now: float) -> bool:
        """One conditional UPDATE consumes once, within the caller's payment transaction."""
        with self.transaction():
            return self._local.connection.execute(
                "UPDATE approvals SET consumed=1 WHERE binding_hash=? AND consumed=0 AND expires_at>? AND key_id=? AND user_hash=?",
                (self._binding(record), now, self.key_id, self.token("user", record.user_id))).rowcount == 1

    def revoke(self, record: ApprovalRecord) -> None:
        """Record a denied/consumed tombstone without persisting live arguments."""
        with self.transaction():
            self.grant(record, float("-inf"))
            self._local.connection.execute("UPDATE approvals SET consumed=1 WHERE binding_hash=? AND key_id=?",
                                           (self._binding(record), self.key_id))

    def cleanup_expired(self, now: float) -> int:
        """Remove expired grants and consumed tombstones, including retired-key metadata."""
        with self.transaction():
            return self._local.connection.execute("DELETE FROM approvals WHERE expires_at<=?", (now,)).rowcount

    def get_state(self, name: str) -> dict:
        """Read named state through the same active transaction."""
        with self.transaction():
            row = self._local.connection.execute("SELECT value FROM component_state WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else {}

    def put_state(self, name: str, value: dict) -> None:
        """Persist only masked state; component names are fixed program identifiers."""
        if name not in {"payment"}:
            raise LedgerConfigurationError("unknown ledger component")
        with self.transaction():
            self._local.connection.execute("INSERT OR REPLACE INTO component_state VALUES (?,?)",
                                           (name, json.dumps(mask_any(value), ensure_ascii=False, allow_nan=False)))

    def append_event(self, scope: str, kind: str, data: dict, dedupe_key: str | None = None) -> dict | None:
        """Persist masked event data and keyed scope/dedupe indexes; one future chain hook."""
        with self.transaction():
            safe = self._event_data(data)
            if kind == "approval" and "key" in data:
                safe.pop("key", None)
                safe["approval_ref"] = self.token("approval-ref", str(data["key"]))
            event = {"kind": mask_any(kind), "data": safe}
            scope_id = self.token("event-scope", scope)
            dedupe = self.token("event-dedupe", dedupe_key) if dedupe_key is not None else None
            cursor = self._local.connection.execute(
                "INSERT OR IGNORE INTO events(scope,kind,data,dedupe,key_id) VALUES (?,?,?,?,?)",
                (scope_id, event["kind"], json.dumps(safe, ensure_ascii=False), dedupe, self.key_id))
            if not cursor.rowcount:
                return None
            event["seq"] = cursor.lastrowid - 1
            self.event_hook(deepcopy(event))
            return event

    def _event_data(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"args", "kwargs", "arguments"}:
                    result[key] = {}
                elif key in {"args_json", "binding", "account", "account_number", "bank_account", "card", "card_number", "ssn"}:
                    result[key] = "[REDACTED]"
                elif key in {"user_id", "request_id"}:
                    result[key] = self.token(key, str(item))
                else:
                    result[key] = self._event_data(item)
            return mask_any(result)
        if isinstance(value, (list, tuple)):
            return [self._event_data(item) for item in value]
        if isinstance(value, int) and not isinstance(value, bool) and len(str(abs(value))) in range(13, 20):
            return "[REDACTED_NUMBER]"
        return mask_any(value)

    def read_events(self, scope: str) -> list[dict]:
        """Reload current-key events for an explicit stable scope."""
        with self.transaction():
            return [{"seq": row[0] - 1, "kind": row[1], "data": json.loads(row[2])}
                    for row in self._local.connection.execute(
                        "SELECT id,kind,data FROM events WHERE scope=? ORDER BY id", (self.token("event-scope", scope),))]


def create_ledger_store() -> LedgerStore:
    """Memory by default; SQLite requires an explicit path and env-only HMAC key."""
    spec = os.environ.get("ORCHESTRA_LEDGER", "memory")
    if spec == "memory":
        return MemoryLedgerStore()
    if spec.startswith("sqlite:///") and spec[len("sqlite:///"):] and spec[len("sqlite:///"):] != ":memory:":
        return SQLiteLedgerStore(spec[len("sqlite:///"):])
    raise LedgerConfigurationError("ORCHESTRA_LEDGER must be memory or sqlite:///explicit-path")
