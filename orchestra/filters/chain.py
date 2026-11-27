"""FilterChain: 순서·단락·MODIFY 전파·승인 지연·fail-closed (DESIGN §11.2)."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, cast

from ..runtime import EventLog
from ..security.control import SecurityControl
from ..specs import FilterSpec
from .base import Action, Filter, FilterContext, FilterDecision, Stage

# 새 작업이 시작되는 단계만 막는다. 이미 실행된 작업의 결과 필터(post_*)는 계속 돌아야 한다.
_GUARDED_STAGES = frozenset({Stage.PRE_REQUEST, Stage.PRE_MODEL, Stage.PRE_TOOL})


@dataclass
class TrailEntry:
    filter: str
    action: Action
    reason: str = ""


@dataclass
class ChainResult:
    action: Action                     # ALLOW | BLOCK | REQUIRE_APPROVAL
    payload: Any
    reason: str = ""
    blocked_by: Optional[str] = None
    approval: Optional[dict] = None
    route_sandbox: bool = False
    modified: bool = False
    feedback: dict[str, float] = field(default_factory=dict)
    trail: list[TrailEntry] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action == Action.ALLOW


def _matches(spec: FilterSpec, ctx: FilterContext) -> bool:
    if ctx.stage.value not in spec.stages:
        return False
    targets = [t for t in (ctx.tool, ctx.agent) if t]
    if not targets:  # pre_request / pre_response 처럼 대상 없음
        return "*" in spec.applies_to
    return any(fnmatch.fnmatch(t, pat) for pat in spec.applies_to for t in targets)


class FilterChain:
    """(FilterSpec, Filter) 쌍을 order 오름차순으로 실행한다.

    - MODIFY: 다음 필터에 수정된 payload 전달
    - BLOCK: 즉시 단락
    - REQUIRE_APPROVAL: 나머지 필터를 끝까지 돌린 뒤 반영 (이후 BLOCK 될 요청을 사용자에게 묻지 않기 위해)
    - 필터가 예외를 던지면 fail-closed(BLOCK)
    - security_control 이 있으면 전역 kill switch·팀 차단기를 필터보다 먼저 확인한다
    """

    def __init__(self, entries: list[tuple[FilterSpec, Filter]], events: Optional[EventLog] = None,
                 security_control: Optional[SecurityControl] = None):
        self.entries = sorted(((s, f) for s, f in entries if s.enabled), key=lambda e: e[0].order)
        self.events = events
        self.security_control = security_control

    def names(self) -> list[str]:
        return [s.name for s, _ in self.entries]

    def with_events(self, events: EventLog) -> "FilterChain":
        return FilterChain(list(self.entries), events, self.security_control)

    def run(self, ctx: FilterContext, *, dedupe: Optional[str] = None) -> ChainResult:
        """단계에 해당하는 필터를 실행하고, 도구 결과를 팀 차단기에 반영한다."""
        control = self.security_control
        if control is not None and ctx.stage in _GUARDED_STAGES:
            stop = control.guard(ctx.agent or "")
            if stop is not None:
                denied = FilterDecision.block(stop)
                # dedupe 하지 않는다: 같은 툴이 전에 막힌 적이 있어도 이번 중단은 기록해야 한다.
                self._emit(ctx, "security_control", denied, None)
                return ChainResult(Action.BLOCK, ctx.payload, stop, blocked_by="security_control",
                                   trail=[TrailEntry("security_control", Action.BLOCK, stop)])
        result = self._run_filters(ctx, dedupe)
        if control is not None and ctx.agent and ctx.stage in (Stage.PRE_TOOL, Stage.POST_TOOL):
            # 승인 대기는 중립, pre_tool 통과만으로는 연속 차단 횟수를 지우지 않는다.
            if result.action == Action.BLOCK:
                control.record(ctx.agent, blocked=True)
            elif ctx.stage == Stage.POST_TOOL and result.action == Action.ALLOW:
                control.record(ctx.agent, blocked=False)
        return result

    def _run_filters(self, ctx: FilterContext, dedupe: Optional[str]) -> ChainResult:
        payload = ctx.payload
        res = ChainResult(Action.ALLOW, payload)
        approval: Optional[dict] = None
        approval_reason = ""
        for spec, flt in self.entries:
            if not _matches(spec, ctx):
                continue
            ctx.payload = payload
            try:
                d = flt(ctx)
            except Exception as e:  # noqa: BLE001 - fail closed
                d = FilterDecision.block(f"filter_error:{spec.name}:{type(e).__name__}")
            if d.feedback:
                res.feedback.update(d.feedback)
                if self.events:
                    self.events.add("feedback", **cast(dict[str, Any], d.feedback))
            res.trail.append(TrailEntry(spec.name, d.action, d.reason))
            self._emit(ctx, spec.name, d, dedupe)
            if d.action == Action.BLOCK:
                res.action, res.reason, res.blocked_by, res.payload = Action.BLOCK, d.reason, spec.name, payload
                return res
            if d.action == Action.MODIFY:
                payload = d.payload
                res.modified = True
            elif d.action == Action.REQUIRE_APPROVAL and approval is None:
                approval, approval_reason = d.approval or {}, d.reason
            elif d.action == Action.ROUTE_SANDBOX:
                res.route_sandbox = True
        res.payload = payload
        if approval is not None:
            res.action, res.approval, res.reason = Action.REQUIRE_APPROVAL, approval, approval_reason
        return res

    def _emit(self, ctx: FilterContext, name: str, d: FilterDecision, dedupe: Optional[str]) -> None:
        if not self.events or d.action == Action.ALLOW and not self.events.capture_trace:
            return
        key = f"filter:{dedupe}:{name}:{ctx.stage.value}:{d.action.value}" if dedupe else None
        self.events.add(
            "filter", dedupe_key=key, stage=ctx.stage.value, filter=name, action=d.action.value,
            reason=d.reason, tool=ctx.tool, agent=ctx.agent,
        )


def build_chain(specs: list[FilterSpec], impls: dict[str, Callable[..., Filter]],
                events: Optional[EventLog] = None) -> FilterChain:
    """FilterSpec 목록과 구현 팩토리(name -> factory(**params))로 체인 생성. 같은 name 이면 뒤쪽이 덮어쓴다."""
    by_name: dict[str, FilterSpec] = {}
    for s in specs:
        by_name[s.name] = s
    entries: list[tuple[FilterSpec, Filter]] = []
    for s in by_name.values():
        if s.name not in impls:
            raise KeyError(f"no filter implementation registered for {s.name!r}")
        entries.append((s, impls[s.name](**s.params)))
    return FilterChain(entries, events)
