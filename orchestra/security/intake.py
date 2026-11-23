"""플러그인·스킬·MCP 정의 인입 게이트 (SECURITY_ROADMAP ③ SA-1 / 구 S3).

등록 순서 (하나라도 실패하면 deny, 이유는 reasonCode):
  1) 고정(pin): 커밋은 40자리 SHA 여야 한다. 브랜치·태그·짧은 SHA 는 움직일 수 있으므로 거부 → INTAKE_UNPINNED
  2) 해시: 디스크의 파일이 매니페스트의 sha256 과 같아야 하고, 매니페스트에 없는 파일이 있으면 안 된다 → INTAKE_HASH_MISMATCH
  3) 스캔: SecurityScanner(security-audit-skill/Claude Security 어댑터) 결과에 차단 대상이 없어야 한다 → INTAKE_SCAN_BLOCKED
     인입은 공급망 경계라 기본이 엄격 모드: needs_validation 도 막는다.
통과분만 `IntakeRegistry` 에 오르고, 툴/에이전트로 노출할 때 `assert_exposable` 이 다시 확인한다
(등록 뒤 파일이 바뀌면 매니페스트 다이제스트가 달라져 거부).

LOCK.json 형식은 벤더링 스크립트(`scripts/sync_third_party.py`, 브랜치 08)의
`{"schema_version": 1, "sources": {<src>: {"commit": sha, "files": {path: sha256}}}}` 를 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from .decision import Decision, ReasonCode, allow, deny
from .scanner import Finding, SecurityScanner, Severity, gate

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class IntakeManifest(BaseModel):
    name: str                                   # 예: "claude_plugins/security-guidance"
    source: str = ""                            # 저장소 식별자 (정보용)
    commit: str
    files: dict[str, str] = Field(default_factory=dict)   # 상대경로(posix) -> sha256

    def digest(self) -> str:
        """매니페스트 전체를 대표하는 해시 (커밋 + 파일 목록 + 각 해시)."""
        blob = json.dumps({"commit": self.commit, "files": self.files}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class IntakeVerdict(BaseModel):
    name: str
    decision: Decision
    problems: list[str] = Field(default_factory=list)
    blocking: list[Finding] = Field(default_factory=list)
    manifest_digest: str = ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_rel(rel: str) -> PurePosixPath | None:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p


def check_pin(m: IntakeManifest) -> list[str]:
    problems = []
    if not FULL_SHA.match(m.commit or ""):
        problems.append(f"commit {m.commit!r} is not a full 40-hex SHA")
    bad = [k for k, v in m.files.items() if not _HEX64.match(v or "")]
    if bad:
        problems.append(f"{len(bad)} file(s) without a sha256 entry")
    if not m.files:
        problems.append("manifest lists no files")
    return problems


def check_files(m: IntakeManifest, root: Path, exclude: tuple[str, ...] = ()) -> list[str]:
    """root 기준으로 매니페스트 파일을 대조한다. 심볼릭 링크·경로 탈출은 불일치로 본다.
    `exclude`: 목록 외 파일 검사에서 뺄 최상위 디렉터리 (다른 매니페스트가 맡는 단위 디렉터리)."""
    problems: list[str] = []
    root = root.resolve()
    for rel, want in sorted(m.files.items()):
        p = _safe_rel(rel)
        if p is None:
            problems.append(f"unsafe path {rel!r}")
            continue
        full = root / Path(*p.parts)
        if full.is_symlink() or not full.resolve().is_relative_to(root):
            problems.append(f"{rel}: symlink or path escape")
            continue
        if not full.is_file():
            problems.append(f"{rel}: missing")
            continue
        if sha256_file(full) != want:
            problems.append(f"{rel}: sha256 mismatch")
    listed = set(m.files)
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file() or f.is_symlink():
                rel = f.relative_to(root).as_posix()
                if rel.split("/", 1)[0] in exclude:
                    continue
                if rel not in listed:
                    problems.append(f"{rel}: not in manifest")
    return problems


class IntakeGate:
    """pin → hash → scan. 스캐너 예외도 deny (fail closed)."""

    def __init__(self, scanner: SecurityScanner, *, fail_on: Severity = Severity.HIGH, strict_unvalidated: bool = True):
        self.scanner = scanner
        self.fail_on = fail_on
        self.strict_unvalidated = strict_unvalidated

    def evaluate(self, m: IntakeManifest, root: Path, exclude: tuple[str, ...] = ()) -> IntakeVerdict:
        digest = m.digest()
        problems = check_pin(m)
        if problems:
            return IntakeVerdict(name=m.name, problems=problems, manifest_digest=digest,
                                 decision=deny(ReasonCode.INTAKE_UNPINNED, problems[0], by="intake_gate"))
        problems = check_files(m, root, exclude)
        if problems:
            return IntakeVerdict(name=m.name, problems=problems, manifest_digest=digest,
                                 decision=deny(ReasonCode.INTAKE_HASH_MISMATCH, f"{len(problems)} problem(s)",
                                               by="intake_gate"))
        try:
            report = self.scanner.scan(str(root))
        except Exception as exc:  # noqa: BLE001
            return IntakeVerdict(name=m.name, problems=[f"scanner error: {type(exc).__name__}"], manifest_digest=digest,
                                 decision=deny(ReasonCode.INTAKE_SCAN_BLOCKED, "scanner failed", by="intake_gate"))
        blocking = gate(report, fail_on=self.fail_on, block_on_needs_validation=self.strict_unvalidated)
        if blocking:
            return IntakeVerdict(name=m.name, blocking=blocking, manifest_digest=digest,
                                 problems=[f"{f.verdict.value} {f.id}" for f in blocking],
                                 decision=deny(ReasonCode.INTAKE_SCAN_BLOCKED, f"{len(blocking)} blocking finding(s)",
                                               by=f"intake_gate:{getattr(self.scanner, 'name', 'scanner')}"))
        return IntakeVerdict(name=m.name, manifest_digest=digest, decision=allow("intake_gate"))


class IntakeRegistry:
    """통과한 매니페스트만 기록. 노출 시점에 다시 대조한다."""

    def __init__(self) -> None:
        self._admitted: dict[str, str] = {}

    def admit(self, verdict: IntakeVerdict) -> bool:
        if not verdict.decision.allowed:
            return False
        self._admitted[verdict.name] = verdict.manifest_digest
        return True

    def revoke(self, name: str) -> None:
        self._admitted.pop(name, None)

    def admitted(self) -> list[str]:
        return sorted(self._admitted)

    def assert_exposable(self, m: IntakeManifest) -> Decision:
        """툴/에이전트로 노출해도 되는가. 미등록 또는 등록 이후 매니페스트 변경이면 deny."""
        got = self._admitted.get(m.name)
        if got is None:
            return deny(ReasonCode.INTAKE_NOT_ADMITTED, m.name, by="intake_registry")
        if got != m.digest():
            return deny(ReasonCode.INTAKE_HASH_MISMATCH, f"{m.name}: manifest changed since admission",
                        by="intake_registry")
        return allow("intake_registry")


def manifests_from_lock(lock: dict[str, Any] | str | Path, *, unit_dir: str = "plugins") -> list[IntakeManifest]:
    """LOCK.json → 매니페스트 목록. `<unit_dir>/<name>/...` 아래 파일은 단위(플러그인)별로 나누고,
    나머지 파일은 소스 단위 매니페스트 하나로 묶는다. 경로는 단위 디렉터리 기준 상대경로로 바뀐다."""
    if isinstance(lock, (str, Path)):
        lock = json.loads(Path(lock).read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or lock.get("schema_version") != 1 or not isinstance(lock.get("sources"), dict):
        raise ValueError("unsupported LOCK.json (need schema_version 1 with 'sources')")
    out: list[IntakeManifest] = []
    for src, entry in sorted(lock["sources"].items()):
        commit = str(entry.get("commit", ""))
        units: dict[str, dict[str, str]] = {}
        rest: dict[str, str] = {}
        for rel, sha in (entry.get("files") or {}).items():
            parts = PurePosixPath(rel).parts
            if len(parts) >= 3 and parts[0] == unit_dir:
                units.setdefault(parts[1], {})[PurePosixPath(*parts[2:]).as_posix()] = sha
            else:
                rest[rel] = sha
        for unit, files in sorted(units.items()):
            out.append(IntakeManifest(name=f"{src}/{unit}", source=src, commit=commit, files=files))
        if rest:
            out.append(IntakeManifest(name=f"{src}/_root", source=src, commit=commit, files=rest))
    return out


def unit_root(base: Path, m: IntakeManifest, *, unit_dir: str = "plugins") -> tuple[Path, tuple[str, ...]]:
    """`manifests_from_lock` 매니페스트의 디스크 위치와 제외 디렉터리.
    `<src>/_root` 는 소스 루트에서 단위 디렉터리를 뺀 나머지를 맡는다."""
    src, unit = m.name.split("/", 1)
    if unit == "_root":
        return base / src, (unit_dir,)
    return base / src / unit_dir / unit, ()
