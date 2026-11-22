"""NemoClaw egress preset 생성·검증·drift 검사 (DESIGN §10.4~10.5). 순수 함수 — NemoClaw 바이너리 불필요.

원천은 ToolSpec.egress / sandbox_name. 같은 선언에서 정책 파일이 나오므로 도구와 정책이 어긋나지 않는다.
금지 규칙(검증 대상):
  * 추론 제공자 호스트(inference.local 등)를 egress 에 넣지 말 것 (관리형 자격증명 경계 우회)
  * binaries 누락/비어 있음, access: full, allowed_ips(SSRF 경계), 와일드카드 호스트
  * 같은 host:port 가 서로 다른 샌드박스에 열리는 것 (신뢰 경계 분리; LangSmith 호스트 제외)
  * placeholder 호스트(*.invalid) — 실제 값(PAYMENT_PROVIDER_HOST) 미설정
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ...specs import EgressRule, ToolSpec

DEFAULT_BINARY = "/opt/orchestra/venv/bin/python3*"
LANGSMITH_HOST = "api.smith.langchain.com"
LANGSMITH_PRESET = "orchestra-langsmith"
# [VERIFY] 실제 SDK 요청 경로는 로컬 수신 서버로 캡처해 확인 (tests/test_langsmith_offline 의 수신 서버 재사용)
LANGSMITH_RULES = [{"method": "POST", "path": "/runs/**"}, {"method": "PATCH", "path": "/runs/**"},
                   {"method": "GET", "path": "/info"}]
DEFAULT_FORBIDDEN_HOSTS = frozenset({
    "inference.local", "api.anthropic.com", "api.openai.com", "integrate.api.nvidia.com",
    "inference-api.nvidia.com", "generativelanguage.googleapis.com",
})
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$|^[a-z0-9-]+$", re.I)


def preset_name(sandbox: str) -> str:
    return f"{sandbox}-egress"


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def collect(tool_specs: Iterable[ToolSpec]) -> dict[str, dict[str, EgressRule]]:
    """sandbox -> {host:port -> 병합된 EgressRule}. 같은 호스트의 규칙은 합친다."""
    out: dict[str, dict[str, EgressRule]] = {}
    for s in tool_specs:
        if not s.sandbox_name or not s.egress:
            continue
        slot = out.setdefault(s.sandbox_name, {})
        for e in s.egress:
            cur = slot.get(e.hostport)
            if cur is None:
                slot[e.hostport] = e.model_copy(deep=True)
            else:
                for r in e.rules:
                    if r not in cur.rules:
                        cur.rules.append(r)
    return out


def _doc(name: str, description: str, policies: dict[str, dict], binary: str) -> dict[str, Any]:
    return {
        "preset": {"name": name, "description": description},
        "network_policies": {
            k: {
                "name": k,
                "endpoints": [
                    {"host": e.host, "port": e.port, "protocol": e.protocol, "enforcement": "enforce",
                     "rules": [{"allow": dict(r)} for r in e.rules]}
                    for e in eps
                ],
                "binaries": [{"path": binary}],
            }
            for k, eps in policies.items()
        },
    }


def build_presets(tool_specs: Iterable[ToolSpec], *, security_mode: str = "masked",
                  langsmith_host: str = LANGSMITH_HOST, binary: str = DEFAULT_BINARY) -> dict[str, dict]:
    """preset_name -> 문서. closed 모드에서는 LangSmith preset 을 만들지 않는다 (물리적 전송 차단, 이중 방어)."""
    if security_mode not in ("off", "masked", "closed", "self_hosted"):
        raise ValueError(f"unknown security_mode {security_mode!r}")
    presets: dict[str, dict] = {}
    for sandbox, hosts in sorted(collect(tool_specs).items()):
        policies = {_key(f"{sandbox}_{hp}"): [rule] for hp, rule in sorted(hosts.items())}
        presets[preset_name(sandbox)] = _doc(preset_name(sandbox), f"Egress for {sandbox} (generated)", policies, binary)
    if security_mode != "closed":
        ls = EgressRule(host=langsmith_host, rules=list(LANGSMITH_RULES))
        presets[LANGSMITH_PRESET] = _doc(LANGSMITH_PRESET, "LangSmith trace ingest (generated)",
                                         {"orchestra_langsmith": [ls]}, binary)
    return presets


def applied_presets(sandboxes: Iterable[str], presets: dict[str, dict]) -> dict[str, list[str]]:
    """각 샌드박스에 적용돼야 하는 preset 이름 (자기 egress preset + 존재하면 langsmith)."""
    out = {}
    for s in sandboxes:
        names = [n for n in (preset_name(s), LANGSMITH_PRESET) if n in presets]
        out[s] = names
    return out


def allowed_hostports(doc: dict) -> set[str]:
    return {f"{e['host']}:{e['port']}" for p in doc.get("network_policies", {}).values() for e in p.get("endpoints", [])}


def allowed_by_sandbox(presets: dict[str, dict], mapping: dict[str, list[str]]) -> dict[str, set[str]]:
    """FakeSandboxRunner 에 넣을 샌드박스별 허용 host:port."""
    return {sb: set().union(*(allowed_hostports(presets[n]) for n in names)) if names else set()
            for sb, names in mapping.items()}


# ---------------------------------------------------------------- 검증
def validate_preset(doc: dict, *, forbidden_hosts: Iterable[str] = DEFAULT_FORBIDDEN_HOSTS,
                    allow_placeholder: bool = False) -> list[str]:
    errs: list[str] = []
    name = (doc.get("preset") or {}).get("name", "?")
    pols = doc.get("network_policies")
    if not pols:
        return [f"{name}: network_policies is empty"]
    forbidden = {h.lower() for h in forbidden_hosts}
    for key, p in pols.items():
        where = f"{name}.{key}"
        if not p.get("binaries") or not all(b.get("path") for b in p["binaries"]):
            errs.append(f"{where}: binaries missing (omitting allows any executable)")
        if p.get("access") == "full" or any(e.get("access") == "full" for e in p.get("endpoints", [])):
            errs.append(f"{where}: access: full is forbidden")
        if not p.get("endpoints"):
            errs.append(f"{where}: no endpoints")
        for e in p.get("endpoints", []):
            host = str(e.get("host", "")).lower()
            if not host or "*" in host or not _HOST_RE.match(host):
                errs.append(f"{where}: invalid or wildcard host {host!r}")
            if host in forbidden:
                errs.append(f"{where}: inference/credential host {host!r} must not be in egress policy")
            if host.endswith(".invalid") and not allow_placeholder:
                errs.append(f"{where}: placeholder host {host!r} (set the real host, e.g. PAYMENT_PROVIDER_HOST)")
            if not isinstance(e.get("port"), int) or not (0 < e["port"] < 65536):
                errs.append(f"{where}: bad port for {host}")
            if e.get("enforcement") != "enforce":
                errs.append(f"{where}: {host} enforcement must be 'enforce'")
            if not e.get("rules"):
                errs.append(f"{where}: {host} has no L7 rules (L4-only is not allowed here)")
            if e.get("allowed_ips"):
                errs.append(f"{where}: allowed_ips changes the SSRF boundary; opt-in required")
    return errs


def validate_presets(presets: dict[str, dict], *, tool_specs: Optional[Iterable[ToolSpec]] = None,
                     forbidden_hosts: Iterable[str] = DEFAULT_FORBIDDEN_HOSTS,
                     allow_placeholder: bool = False) -> list[str]:
    errs: list[str] = []
    for doc in presets.values():
        errs += validate_preset(doc, forbidden_hosts=forbidden_hosts, allow_placeholder=allow_placeholder)
    if tool_specs is not None:  # 신뢰 경계: 같은 host:port 가 두 샌드박스에 열리면 안 된다
        seen: dict[str, str] = {}
        for sandbox, hosts in collect(tool_specs).items():
            for hp in hosts:
                if hp in seen and seen[hp] != sandbox:
                    errs.append(f"{hp} is opened in both {seen[hp]} and {sandbox} (split trust boundaries)")
                seen[hp] = sandbox
    return errs


def check_applied(expected: dict[str, list[str]], actual: dict[str, list[str]]) -> list[str]:
    """기동 시 fail-closed 점검: 실제 적용 preset 이 기대와 다르면 오류 목록(비어 있지 않으면 기동 실패)."""
    errs = []
    for sb, want in expected.items():
        have = set(actual.get(sb, []))
        if set(want) - have:
            errs.append(f"{sb}: missing presets {sorted(set(want) - have)}")
        if LANGSMITH_PRESET in have and LANGSMITH_PRESET not in want:
            errs.append(f"{sb}: {LANGSMITH_PRESET} applied but security mode forbids trace egress")
    return errs


# ---------------------------------------------------------------- 렌더/드리프트
def render(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def write_all(presets: dict[str, dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, doc in presets.items():
        p = out_dir / f"{name}.yaml"
        p.write_text(render(doc), encoding="utf-8")
        paths.append(p)
    return paths


def diff_dir(presets: dict[str, dict], directory: Path) -> list[str]:
    """커밋된 preset 과 생성 결과의 차이 (CI drift 검사). 파일명 = preset.name."""
    problems = []
    for name, doc in presets.items():
        f = directory / f"{name}.yaml"
        if not f.exists():
            problems.append(f"missing: {f}")
        elif f.read_text(encoding="utf-8") != render(doc):
            problems.append(f"drift: {f}")
    for f in sorted(directory.glob("*.yaml")) if directory.exists() else []:
        if f.stem not in presets:
            problems.append(f"stale: {f}")
    return problems
