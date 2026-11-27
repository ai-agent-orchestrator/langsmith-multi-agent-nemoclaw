"""Tracing contexts and an opt-in OrchestraService observation adapter."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Iterator

from langsmith import trace, tracing_context, get_current_run_tree

from ..security.decision import Decision, DecisionAction, ReasonCode
from .masking import mask_any
from .client import SafeClient, make_client, security_mode
from .safe import safe_call

_CLIENT: ContextVar[Any] = ContextVar("orchestra_trace_client", default=None)
_SOURCE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@contextmanager
def tracing_scope(mode: str, client: SafeClient | None = None, *, enabled: bool = True,
                  parent: Any = None) -> Iterator[None]:
    """Suppress all nested tracing when closed or explicitly disabled."""
    active = security_mode(mode) != "closed" and enabled
    context = safe_call(tracing_context, enabled=active, client=client if active else None,
                        parent=parent if active else None, default=nullcontext())
    token = _CLIENT.set(client)
    try:
        with context:
            yield
    finally:
        _CLIENT.reset(token)


@contextmanager
def span(name: str, *, inputs: Any = None, metadata: dict | None = None,
         client: SafeClient | None = None) -> Iterator[Any]:
    """Record a sanitized child span; SDK failures never rerun the observed operation."""
    parent = safe_call(get_current_run_tree)
    if (parent is None and client is None) or (client is not None and client.mode == "closed"):
        yield None
        return
    context = safe_call(trace, name, inputs={"payload": mask_any(inputs or {})}, metadata=mask_any(metadata or {}),
                        parent=parent, client=client or _CLIENT.get(), default=None)
    tree = safe_call(context.__enter__) if context is not None else None
    try:
        yield tree
    except Exception as error:
        if tree is not None:
            safe_call(tree.end, error=type(error).__name__)
        raise
    finally:
        if context is not None and tree is not None:
            safe_call(context.__exit__, None, None, None)


class TracedFilter:
    """Wrap a filter without changing its decision or its failure semantics."""

    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.name = wrapped.name

    def __call__(self, context: Any) -> Any:
        with span(f"filter:{self.name}", inputs=context.payload, metadata={"stage": context.stage.value}) as tree:
            result = self.wrapped(context)
            if tree is not None:
                safe_call(tree.end, outputs=mask_any({"action": result.action.value, "reason": result.reason}))
            return result


class TracedSandbox:
    """Observe an existing runner without changing its interface or executing a CLI."""

    def __init__(self, wrapped: Any, mode: str) -> None:
        self.wrapped, self.mode = wrapped, mode

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)

    def exec(self, request: Any) -> Any:
        """Propagate masked correlation headers and record the actual runner result."""
        from .distributed import trace_headers

        copied = request.model_copy(update={"trace_headers": trace_headers(self.mode)})
        with span(f"sandbox:{request.sandbox}", inputs=request.args,
                  metadata={"sandbox_name": request.sandbox, "tool": request.tool}) as tree:
            result = self.wrapped.exec(copied)
            if tree is not None:
                safe_call(tree.add_metadata, {"policy_revision": result.policy_revision})
                safe_call(tree.end, outputs=mask_any(result.model_dump()))
            return result


class Observation:
    """Supply config_hook/scope and optional filter instrumentation to OrchestraService."""

    def __init__(self, *, org: str, domains: list[str] | None = None, mode: str | None = None,
                 pattern: str = "hierarchical", client: SafeClient | None = None,
                 enabled: bool | None = None, metadata: dict | None = None) -> None:
        self.mode = security_mode(mode)
        self.org, self.domains, self.pattern = org, domains or [], pattern
        self.enabled = (os.environ.get("LANGSMITH_TRACING", "false").lower() in {"true", "1"}) \
            if enabled is None else enabled
        self.client = SafeClient(None, "closed") if self.mode == "closed" else (
            client or (make_client(self.mode) if self.enabled else SafeClient(None, self.mode)))
        self.metadata = mask_any(metadata or {})

    def config_hook(self, config: dict, thread_id: str) -> dict:
        """Merge tags/session metadata without changing the core recursion limit."""
        result = dict(config)
        result["run_name"] = f"orchestra:{self.org}:{self.pattern}"
        result["tags"] = [*config.get("tags", []), f"org:{self.org}", f"pattern:{self.pattern}",
                          f"security:{self.mode}", f"backend:{self.metadata.get('backend', 'prebuilt')}",
                          f"sandbox:{self.metadata.get('sandbox', 'off')}", "exec:ported",
                          *([f"scenario:{self.metadata['scenario_id']}"] if self.metadata.get("scenario_id") else []),
                          *[f"domain:{d}" for d in self.domains]]
        result["metadata"] = {**config.get("metadata", {}), **self.metadata, "org": self.org,
                              "thread_id": thread_id, "session_id": thread_id, "domains": self.domains}
        if self.mode == "closed" or not self.enabled:
            result["callbacks"] = []
        else:
            from langchain_core.tracers.langchain import LangChainTracer

            tracer = safe_call(LangChainTracer, client=self.client,
                               project_name=os.environ.get("LANGSMITH_PROJECT", "orchestra-dev"))
            result["callbacks"] = [*config.get("callbacks", []), *([tracer] if tracer else [])]
        return result

    def report_decision(self, kind: str, data: dict) -> None:
        """Record a refusal as a run whose metadata is its reason code; no reason text or arguments."""
        code = data.get("reason_code")
        if not code or not self.enabled or self.mode == "closed":
            return
        by = "sandbox" if kind == "sandbox" else data.get("by")
        decision = Decision(action=DecisionAction.DENY, reason_code=ReasonCode(code),
                            by=by if isinstance(by, str) and _SOURCE.fullmatch(by) else None)
        metadata = decision.trace_metadata()
        # A request refused before the graph starts has no parent run and no tracing scope yet.
        context = nullcontext() if safe_call(get_current_run_tree) is not None else self.scope()
        with context, span("security:decision", metadata=metadata, client=self.client) as tree:
            if tree is not None:
                safe_call(tree.end, outputs=metadata)

    def scope(self) -> Any:
        """Return an isolated per-invocation tracing context."""
        return tracing_scope(self.mode, self.client, enabled=self.enabled)

    def service(self, app: Any) -> Any:
        """Bind observation to the existing service hooks; no domain dependencies."""
        from ..service import OrchestraService

        self.metadata.setdefault("sandbox", app.runtime.sandbox_mode)
        self.metadata.setdefault("backend", app.backend)
        self.metadata.setdefault("agent_source", {a.name: a.source for a in app.org.agents.values()})
        self.metadata.setdefault("git_sha", os.environ.get("ORCHESTRA_GIT_SHA", "unavailable"))
        if app.runtime.sandbox is not None and not isinstance(app.runtime.sandbox, TracedSandbox):
            app.runtime.sandbox = TracedSandbox(app.runtime.sandbox, self.mode)
        app.chain.entries[:] = [(spec, flt if isinstance(flt, TracedFilter) else TracedFilter(flt))
                                for spec, flt in app.chain.entries]
        listeners = app.runtime.events.listeners
        if not any(getattr(item, "__func__", None) is Observation.report_decision for item in listeners):
            listeners.append(self.report_decision)
        return OrchestraService(app, config_hook=self.config_hook, scope=self.scope)
