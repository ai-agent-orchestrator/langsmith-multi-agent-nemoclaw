"""시나리오 실행기 + 서사 렌더러 (DESIGN §12). 실제 컴파일된 그래프를 ScriptedChatModel 로 실행한다.

- run_scenario : YAML 시나리오 → 그래프 실행(interrupt 자동 응답) → SimResult
- check_expect : 시나리오의 expect 검증 (골든 테스트용)
- render       : 사람이 읽는 흐름 출력 (①②③ / [LLM #n] / · tool / [FILTER] / [HITL] / [SANDBOX])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models.factory import ScriptedModelFactory
from .org import REPO_ROOT, depth
from .patterns.hierarchical_graph import BuiltApp, build_hierarchical_app
from .runtime import EventLog, Runtime
from .sandbox.fake import FakeSandboxRunner
from .sandbox.nemoclaw import policy
from .service import OrchestraService

STEP_LABELS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


@dataclass
class SimResult:
    scenario: dict
    app: BuiltApp
    events: EventLog
    final: dict
    interrupts: list[dict] = field(default_factory=list)
    decisions: list[bool] = field(default_factory=list)
    leftovers: dict[str, int] = field(default_factory=dict)
    sandbox: Any = None


def load_scenario(id_or_path: str) -> dict:
    p = Path(id_or_path)
    if not p.exists():
        p = REPO_ROOT / "scenarios" / f"{id_or_path}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in (REPO_ROOT / "scenarios").glob("*.yaml"))


def run_scenario(sc: dict | str, *, sandbox: str = "fake", approve: Optional[list[bool]] = None,
                 security_mode: str = "closed") -> SimResult:
    sc = load_scenario(sc) if isinstance(sc, str) else sc
    ctx = sc.get("context", {})
    services: dict[str, Any] = {"today": ctx.get("today"), "now": 0.0}
    services["clock"] = lambda: services["now"]
    events = EventLog()
    rt = Runtime(security_mode=security_mode, sandbox_mode=sandbox, events=events, services=services,
                 clock=services["clock"])
    factory = ScriptedModelFactory(sc.get("script", {}), events)
    app = build_hierarchical_app(sc["org"], factory, services=services, runtime=rt)

    runner = None
    if sandbox == "fake":
        specs = [s for _, s in app.resolved.tools.values()]
        presets = policy.build_presets(specs, security_mode=security_mode)
        sandboxes = sorted({s.sandbox_name for s in specs if s.sandbox_name})
        allowed = policy.allowed_by_sandbox(presets, policy.applied_presets(sandboxes, presets))
        for hp in sc.get("deny_hosts", []):  # 시나리오가 정책에서 호스트를 빼서 egress 거부를 재현
            for v in allowed.values():
                v.discard(hp)
        runner = FakeSandboxRunner(allowed)
        rt.sandbox = runner

    svc = OrchestraService(app)
    tid = sc["id"]
    decisions = list(approve if approve is not None else sc.get("approve", [True]))
    out = svc.ask(sc["request"], thread_id=tid)
    interrupts, used = [], []
    while out.get("interrupt"):
        interrupts.append(out["interrupt"]["payload"])
        d = decisions.pop(0) if decisions else False  # 응답이 없으면 거절(fail closed)
        used.append(d)
        out = svc.resume(tid, d)
    return SimResult(sc, app, events, out, interrupts, used, factory.leftovers(), runner)


# ---------------------------------------------------------------- 검증
def _llm_roles(ev: EventLog) -> list[str]:
    seen: list[str] = []
    for e in ev.of("llm"):
        if e.data["role"] not in seen:
            seen.append(e.data["role"])
    return seen


def _is_subsequence(want: list, have: list) -> bool:
    it = iter(have)
    return all(any(w == h for h in it) for w in want)


def check_expect(res: SimResult) -> list[str]:
    """expect 항목과 실제 실행을 비교한 불일치 목록 (비어 있으면 통과)."""
    exp, ev, errs = res.scenario.get("expect", {}), res.events, []
    if "agents_in_order" in exp and not _is_subsequence(exp["agents_in_order"], _llm_roles(ev)):
        errs.append(f"agents_in_order: want {exp['agents_in_order']} ⊄ got {_llm_roles(ev)}")
    tools = [e.data["tool"] for e in ev.of("tool")]
    if "tools_in_order" in exp and tools != exp["tools_in_order"]:
        errs.append(f"tools_in_order: want {exp['tools_in_order']} got {tools}")
    if "handoffs" in exp:
        got = [[e.data["source"], e.data["target"]] for e in ev.of("handoff")]
        if got != exp["handoffs"]:
            errs.append(f"handoffs: want {exp['handoffs']} got {got}")
    if "interrupts" in exp and len(res.interrupts) != exp["interrupts"]:
        errs.append(f"interrupts: want {exp['interrupts']} got {len(res.interrupts)}")
    flags = exp.get("flags", {})
    if "blocked" in flags and bool(res.final.get("blocked")) != flags["blocked"]:
        errs.append(f"blocked: want {flags['blocked']} got {res.final.get('blocked')}")
    if "payment_captured" in flags:
        captured = any(e.data["tool"] == "capture_payment" for e in ev.of("tool"))
        if captured != flags["payment_captured"]:
            errs.append(f"payment_captured: want {flags['payment_captured']} got {captured}")
    if "blocked_by" in exp:
        blocked_by = [e.data.get("by") for e in ev.of("blocked")]
        if exp["blocked_by"] not in blocked_by:
            errs.append(f"blocked_by: want {exp['blocked_by']} in {blocked_by}")
    if "sandbox_denied" in exp:
        got = [h for e in ev.of("sandbox") for h in (e.data.get("egress_denied") or [])]
        if bool(got) != exp["sandbox_denied"]:
            errs.append(f"sandbox_denied: want {exp['sandbox_denied']} got {got}")
    if res.leftovers:
        errs.append(f"unconsumed script turns: {res.leftovers}")
    return errs


# ---------------------------------------------------------------- 렌더
def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "idempotency_key")


def render(res: SimResult) -> str:
    org = res.app.org
    sup_roles = set(org.teams)
    lines = [f"요청: {res.scenario['request']}", ""]
    step, llm_n, sup_n, last = 0, 0, 0, None
    for e in res.events.events:
        d = e.data
        if e.kind == "llm":
            role = d["role"]
            llm_n += 1
            sup_n += role in sup_roles
            if role != last:
                step += 1
                kind = "총괄/팀장" if role in sup_roles else "실무자"
                lines.append(f"{STEP_LABELS[min(step - 1, 19)]} {role} ({kind})")
                last = role
            calls = ", ".join(c["name"] + (f"({_fmt_args(c['args'])})" if c["args"] else "") for c in d["tool_calls"])
            note = (d["content"] or "").strip()
            lines.append(f"  [LLM #{llm_n}] {role}: " + (f"{note} → " if note and calls else note) + (f"호출 {calls}" if calls else ""))
        elif e.kind == "tool":
            lines.append(f"      · tool {d['tool']}({_fmt_args(d['args'])}) -> {d['result']}")
        elif e.kind == "handoff":
            lines.append(f"      ⇢ handoff {d['source']} → {d['target']} (팀장 경유 없음)")
        elif e.kind == "filter":
            lines.append(f"      [FILTER] {d['stage']} {d['filter']}({d['tool'] or d['agent'] or '-'}): {d['action']} — {d['reason']}")
        elif e.kind == "blocked":
            lines.append(f"      [BLOCKED] {d.get('tool') or 'request'} by {d.get('by')}: {d['reason']}")
        elif e.kind == "approval":
            lines.append(f"      [HITL] 사용자 승인: {'예' if d['approved'] else '아니오'}")
        elif e.kind == "sandbox":
            ok = "ok" if d["ok"] else f"FAILED {d.get('error')} {d.get('egress_denied') or ''}"
            lines.append(f"      [SANDBOX {d.get('sandbox')}] {d['tool']} {ok} (policy rev {d.get('policy_revision')})")
        elif e.kind == "report":
            lines.append(f"      [REPORT {d['team']}] {d['status']}: {d['summary']}")
    for i, p in enumerate(res.interrupts):
        lines.append(f"\n[interrupt #{i + 1}] {p.get('ask')}  → 응답: {'예' if res.decisions[i] else '아니오'}")
    lines += ["", "최종 답변:", f"  {res.final['answer']}", "",
              f"슈퍼바이저(총괄/팀장) LLM 호출 {sup_n}회, 전체 LLM 호출 {llm_n}회 (조직 깊이 {depth(org)})"]
    return "\n".join(lines)
