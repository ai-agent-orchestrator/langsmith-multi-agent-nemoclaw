"""Sandbox-side allowlist, independent filters and durable execution journal."""
from __future__ import annotations

import hashlib
from contextlib import closing, contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, cast, Iterator

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from ...filters.base import Action, FilterContext, Stage
from ...filters.chain import FilterChain
from ...specs import SideEffect, ToolSpec
from ...taint import instruction_in
from ...security.capability import CapabilityAuthority
from ...security.control import SecurityControl
from ...observability.masking import mask_any
from ...observability.distributed import parent_scope
from ...observability.tracing import tracing_scope
from ..base import ExecResult
from .cli import SANDBOX_NAME_RE

MAX_BYTES = 1_048_576


class WorkerRequest(BaseModel):
    """No callable, approval flag, command, policy or client is accepted over the wire."""

    model_config = ConfigDict(extra="forbid", strict=True)
    sandbox: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    egress: list[str] = Field(default_factory=list)
    trace_headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    idempotency_key: str | None = Field(default=None, max_length=256)
    agent: str | None = None
    capability_token: str | None = Field(default=None, repr=False)


class Worker:
    """Execute only local registered tools; remote COMMIT remains fail-closed."""

    def __init__(self, sandbox: str, tools: dict[str, tuple[BaseTool, ToolSpec]], chain: FilterChain, *,
                 journal: Path, grants: dict[str, set[str]], security_mode: str = "closed",
                 authority: CapabilityAuthority | None = None, observation_client: Any = None) -> None:
        if SANDBOX_NAME_RE.fullmatch(sandbox) is None:
            raise ValueError("invalid sandbox name")
        self.sandbox, self.tools, self.chain = sandbox, tools, chain
        self.grants, self.security_mode = grants, security_mode
        self.journal = journal
        self.authority = authority
        self.observation_client = observation_client
        self.chain.security_control = SecurityControl()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS executions (key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(str(self.journal), timeout=5)) as db, db:
            yield db

    def handle(self, payload: str) -> str:
        """Return one bounded JSON result; never include exception text or raw args."""
        try:
            if len(payload.encode()) > MAX_BYTES:
                raise ValueError
            req = WorkerRequest.model_validate_json(payload)
            result = self._execute(req)
            encoded = result.model_dump_json()
            if len(encoded.encode()) > MAX_BYTES:
                return ExecResult(ok=False, error="worker_output_too_large").model_dump_json()
            return encoded
        except Exception:
            return ExecResult(ok=False, error="worker_request_failed").model_dump_json()

    def _execute(self, req: WorkerRequest) -> ExecResult:
        if req.sandbox != self.sandbox or req.tool not in self.tools:
            return ExecResult(ok=False, error="worker_tool_forbidden")
        if req.agent is None or req.tool not in self.grants.get(req.agent, set()):
            return ExecResult(ok=False, error="worker_capability_denied")
        if self.authority is None or not self.authority.verify(req.model_dump(), req.capability_token):
            return ExecResult(ok=False, error="worker_capability_invalid")
        tool, spec = self.tools[req.tool]
        if spec.sandbox_name != self.sandbox or spec.name != req.tool:
            return ExecResult(ok=False, error="worker_sandbox_mismatch")
        # A host assertion is not an approval. Distributed payment requires an
        # independently verifiable authorization and provider-side atomic ledger.
        if spec.side_effect == SideEffect.COMMIT:
            return ExecResult(ok=False, error="worker_commit_authorization_unavailable")
        if not math.isfinite(spec.timeout_s) or spec.timeout_s <= 0:
            return ExecResult(ok=False, error="worker_invalid_timeout")
        schema = cast(Any, tool.get_input_schema())
        args = schema.model_validate(req.args).model_dump()
        pre = self.chain.run(FilterContext(Stage.PRE_TOOL, args, agent=req.agent, tool=spec.name,
                                          tool_spec=spec, security_mode=self.security_mode, sandbox_mode="nemoclaw"))
        if pre.action != Action.ALLOW or not isinstance(pre.payload, dict):
            return ExecResult(ok=False, error="worker_pre_tool_blocked")
        if instruction_in(pre.payload):
            return ExecResult(ok=False, error="worker_taint_blocked")
        args = schema.model_validate(pre.payload).model_dump()
        if spec.side_effect != SideEffect.READ and not req.idempotency_key:
            return ExecResult(ok=False, error="worker_idempotency_required")
        digest = hashlib.sha256(json.dumps({"sandbox": req.sandbox, "tool": req.tool, "agent": req.agent,
                                           "args": args}, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key = hashlib.sha256((req.sandbox + "\0" + (req.idempotency_key or "")).encode()).hexdigest()
        if req.idempotency_key:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT digest, result FROM executions WHERE key=?", (key,)).fetchone()
                if row:
                    if row[0] != digest:
                        return ExecResult(ok=False, error="worker_idempotency_conflict")
                    return ExecResult.model_validate_json(row[1]) if row[1] else ExecResult(
                        ok=False, error="worker_execution_indeterminate")
                db.execute("INSERT INTO executions VALUES (?, ?, NULL)", (key, digest))
        start = time.monotonic()
        try:
            scope = parent_scope(req.trace_headers, mode=self.security_mode, client=self.observation_client) if (
                self.observation_client is not None) else tracing_scope("closed", None, enabled=False)
            with scope:
                output = tool.invoke(args)
            if instruction_in(output):
                result = ExecResult(ok=False, error="worker_taint_blocked")
            else:
                post = self.chain.run(FilterContext(Stage.POST_TOOL, output, agent=req.agent, tool=spec.name,
                                                   tool_spec=spec, security_mode=self.security_mode,
                                                   sandbox_mode="nemoclaw"))
                if post.action != Action.ALLOW or instruction_in(post.payload):
                    result = ExecResult(ok=False, error="worker_post_tool_blocked")
                else:
                    result = ExecResult(ok=True, output=mask_any(post.payload))
            if time.monotonic() - start > min(req.timeout_s, spec.timeout_s):
                result = ExecResult(ok=False, error="worker_timeout")
            encoded = result.model_dump_json()
            if len(encoded.encode()) > MAX_BYTES:
                result = ExecResult(ok=False, error="worker_output_too_large")
        except Exception:
            result = ExecResult(ok=False, error="worker_execution_failed")
        if req.idempotency_key:
            with self._db() as db:
                db.execute("UPDATE executions SET result=? WHERE key=?", (result.model_dump_json(), key))
        return result


def main() -> int:
    """Load the sandbox's own org and filters; read exactly one JSON request."""
    from ...filters.chain import build_chain
    from ...org import load_org
    from ...registry import build_packs, resolve

    try:
        sandbox = os.environ["ORCHESTRA_WORKER_SANDBOX"]
        org = load_org(os.environ["ORCHESTRA_WORKER_ORG"])
        resolved = resolve(org, build_packs(org.domains, {}))
        grants = {agent.name: set(agent.tools) for agent in resolved.org.agents.values()}
        authority = CapabilityAuthority(Path(os.environ["ORCHESTRA_WORKER_CAPABILITY_KEY_FILE"]).read_bytes(), grants)
        worker = Worker(sandbox, resolved.tools, build_chain(resolved.filter_specs, resolved.filter_impls),
                        journal=Path(os.environ["ORCHESTRA_WORKER_JOURNAL"]),
                        grants=grants, authority=authority)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        sys.stdout.write(worker.handle(raw.decode("utf-8")))
        return 0
    except Exception:
        sys.stdout.write(ExecResult(ok=False, error="worker_unavailable").model_dump_json())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
