"""OrgSpec 로드/검증/팬아웃 정규화 — 전부 순수 함수 (그래프·SDK 의존 없음)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from .specs import AgentSpec, OrgSpec, TeamSpec

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_org(path_or_dict: str | Path | dict[str, Any]) -> OrgSpec:
    """YAML 파일(또는 dict)에서 OrgSpec 생성. agents 는 비워 두고 registry.resolve 가 채운다."""
    if isinstance(path_or_dict, dict):
        data = path_or_dict
    else:
        p = Path(path_or_dict)
        if not p.is_absolute() and not p.exists():
            p = REPO_ROOT / p
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    teams = {name: {"name": name, **cfg} for name, cfg in (data.get("teams") or {}).items()}
    return OrgSpec(**{**data, "teams": teams})


def read_prompt(prompt: str, base: Path = REPO_ROOT) -> str:
    """'file:<상대경로>' 이면 파일 내용, 아니면 그대로."""
    if prompt.startswith("file:"):
        return (base / prompt[5:]).read_text(encoding="utf-8").strip()
    return prompt


# ---------------------------------------------------------------- 검증
def validate_org(org: OrgSpec) -> list[str]:
    """구조 오류 목록 (빈 리스트면 통과). agents 가 채워진 뒤 호출."""
    errs: list[str] = []
    if org.root not in org.teams:
        errs.append(f"root {org.root!r} is not a team")

    names: dict[str, str] = {}
    for team_name in org.teams:
        names[team_name] = f"team:{team_name}"
    for aid, a in org.agents.items():
        if aid != a.id:
            errs.append(f"agent key {aid!r} != id {a.id!r}")
        if a.name in names:
            errs.append(f"node name collision: {a.name!r} ({names[a.name]} vs agent:{aid})")
        names[a.name] = f"agent:{aid}"

    parent: dict[str, str] = {}
    for t in org.teams.values():
        for m in t.members:
            if m not in org.teams and m not in org.agents:
                errs.append(f"team {t.name}: unknown member {m!r}")
                continue
            if m in parent:
                errs.append(f"{m!r} has two parents ({parent[m]}, {t.name})")
            parent[m] = t.name
    if org.root in parent:
        errs.append(f"root {org.root!r} is used as a member of {parent[org.root]!r}")

    # 사이클 / 도달 불가
    seen: set[str] = set()

    def walk(team: str, stack: tuple[str, ...]) -> None:
        if team in stack:
            errs.append(f"cycle: {' -> '.join(stack + (team,))}")
            return
        seen.add(team)
        for m in org.teams[team].members:
            if m in org.teams:
                walk(m, stack + (team,))

    if org.root in org.teams:
        walk(org.root, ())
        for team_name in org.teams:
            if team_name not in seen:
                errs.append(f"team {team_name!r} unreachable from root")

    # handoff 는 같은 팀 형제끼리만 (승인·감사 경계 유지)
    for aid, a in org.agents.items():
        for tgt in a.handoff_to:
            if parent.get(aid) is None or parent.get(tgt) != parent.get(aid):
                errs.append(f"agent {aid}: handoff to {tgt!r} crosses team boundary")
    return errs


def assert_valid(org: OrgSpec) -> OrgSpec:
    errs = validate_org(org)
    if errs:
        raise ValueError("invalid org %r:\n  - %s" % (org.name, "\n  - ".join(errs)))
    return org


# ---------------------------------------------------------------- 팬아웃
def normalize_fanout(org: OrgSpec) -> OrgSpec:
    """members > max_fanout 인 팀에 sub-lead 를 삽입한다 (재귀). 입력은 변경하지 않는다."""
    teams = {k: v.model_copy(deep=True) for k, v in org.teams.items()}
    changed = True
    while changed:
        changed = False
        for name in list(teams):
            t = teams[name]
            if len(t.members) <= t.max_fanout:
                continue
            groups = math.ceil(len(t.members) / t.max_fanout)
            size = math.ceil(len(t.members) / groups)
            chunks = [t.members[i : i + size] for i in range(0, len(t.members), size)]
            new_members: list[str] = []
            for i, chunk in enumerate(chunks):
                if len(chunk) == 1:  # 1명짜리 묶음은 sub-lead 없이 직접 매달기
                    new_members.append(chunk[0])
                    continue
                n = i
                while f"{name}_sub{n}" in teams:  # 재귀 분할에서도 이름이 겹치지 않게
                    n += 1
                sub = f"{name}_sub{n}"
                teams[sub] = TeamSpec(
                    name=sub,
                    prompt=f"{t.prompt}\n(너는 {name} 의 하위 그룹 {i} 담당이다. 맡은 팀원만 호출하고 결과를 요약 보고한다.)",
                    members=chunk,
                    model=t.model,
                    output_mode=t.output_mode,
                    max_fanout=t.max_fanout,
                    parallel=t.parallel,
                    reducer=t.reducer,
                )
                new_members.append(sub)
            t.members = new_members
            changed = True
    return org.model_copy(update={"teams": teams})


def team_of(org: OrgSpec) -> dict[str, str]:
    """멤버 id/이름 -> 부모 팀 이름."""
    return {m: t.name for t in org.teams.values() for m in t.members}


def depth(org: OrgSpec) -> int:
    """root 팀=1. 가장 깊은 팀 체인 길이."""
    def d(team: str) -> int:
        subs = [m for m in org.teams[team].members if m in org.teams]
        return 1 + max((d(s) for s in subs), default=0)

    return d(org.root)


def recursion_limit(org: OrgSpec) -> int:
    """org.recursion_limit 이 없으면 10 + 8*depth + 2*agents (상한 120) — DESIGN §6.4(7)."""
    if org.recursion_limit:
        return org.recursion_limit
    leaves = sum(1 for t in org.teams.values() for m in t.members if m not in org.teams)
    return min(120, 10 + 8 * depth(org) + 2 * leaves)


def fanout_report(org: OrgSpec) -> dict[str, int]:
    """팀별 슈퍼바이저가 보는 선택지 수 (README 의 '3~5개 이하' 원칙 점검용)."""
    return {t.name: len(t.members) for t in org.teams.values()}


def leaf_agents(org: OrgSpec) -> list[AgentSpec]:
    return [org.agents[m] for t in org.teams.values() for m in t.members if m in org.agents]
