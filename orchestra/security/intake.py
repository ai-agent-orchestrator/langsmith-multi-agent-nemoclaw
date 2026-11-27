"""플러그인·스킬·MCP 정의 도입 게이트: 고정 → 해시 → 스캔 → 등록.

하나라도 실패하면 deny 이고 이유는 reason_code 로 남는다:
  1) 고정: 커밋은 40자리 SHA 여야 한다. 브랜치·태그·짧은 SHA 는 움직일 수 있다 → INTAKE_UNPINNED
  2) 해시: 파일이 매니페스트의 sha256 과 같아야 하고 목록에 없는 파일이 없어야 한다 → INTAKE_HASH_MISMATCH
  3) 스캔: 방금 해시한 바로 그 바이트에 대한 보고서에 차단 항목이 없어야 한다 → INTAKE_SCAN_BLOCKED
통과분만 `IntakeRegistry` 에 오르고, 노출할 때 `assert_exposable` 이 다시 확인한다.

스캐너 계약은 `scanner.py`(`scan(files) -> ScanReport`)를 그대로 쓴다. LOCK.json 형식은
`scripts/sync_third_party.py` 의 `{"schema_version": 1, "sources": {<src>: {"commit", "files",
"generated_files"}}}` 이다. `__pycache__` 도 목록 외 파일로 본다: 검증하지 않은 바이트코드는 검증한
소스 대신 실행될 수 있다. 깨끗한 체크아웃에서 돌린다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from .decision import Decision, ReasonCode, allow, deny
from .scanner import Finding, SecurityScanner, Severity, StaticScanner, files_digest

MAX_ASSET_BYTES = 2_000_000
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UNIT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class IntakeManifest(BaseModel):
    name: str                                   # 예: "claude_plugins/security-guidance"
    source: str = ""
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


def check_pin(manifest: IntakeManifest) -> list[str]:
    """커밋 고정과 해시 항목 형식을 확인한다."""
    problems = []
    if _FULL_SHA.fullmatch(manifest.commit) is None:
        problems.append("commit is not a full 40-hex SHA")
    bad = [name for name, digest in manifest.files.items() if _HEX64.fullmatch(digest) is None]
    if bad:
        problems.append(f"{len(bad)} file(s) without a sha256 entry")
    if not manifest.files:
        problems.append("manifest lists no files")
    return problems


def _entries(root: Path) -> list[Path]:
    """디렉터리가 아닌 모든 항목. 심볼릭 링크는 디렉터리를 가리켜도 항목으로 본다."""
    return sorted(path for path in root.rglob("*") if path.is_symlink() or not path.is_dir())


def read_snapshot(manifest: IntakeManifest, root: Path, exclude: tuple[str, ...] = (), *,
                  ignore_bytecode: bool = False) -> tuple[list[str], dict[str, bytes]]:
    """파일을 한 번만 읽어 대조하고, 대조한 바이트를 돌려준다 (스캔과 해시가 어긋나지 않게).

    심볼릭 링크·경로 탈출은 불일치다. `exclude` 는 목록 외 파일 검사에서 뺄 최상위 디렉터리이며
    다른 매니페스트가 맡는 곳이다 (`check_coverage` 가 빈틈을 확인한다). `ignore_bytecode` 는 파일을
    데이터로만 읽고 실행하지 않는 호출자만 켠다.
    """
    if root.is_symlink():
        return ["unit root is a symlink"], {}
    problems: list[str] = []
    files: dict[str, bytes] = {}
    resolved = root.resolve()
    for name, expected in sorted(manifest.files.items()):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            problems.append(f"unsafe path {name!r}")
            continue
        path = resolved.joinpath(*relative.parts)
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(resolved):
                problems.append(f"{name}: symlink or path escape")
            elif not path.is_file():
                problems.append(f"{name}: missing")
            else:
                with path.open("rb") as handle:
                    data = handle.read(MAX_ASSET_BYTES + 1)
                if len(data) > MAX_ASSET_BYTES:
                    problems.append(f"{name}: too large")
                elif hashlib.sha256(data).hexdigest() != expected:
                    problems.append(f"{name}: sha256 mismatch")
                else:
                    files[name] = data
        except (OSError, ValueError):
            problems.append(f"{name}: unreadable")
    if resolved.is_dir():
        for path in _entries(resolved):
            name = path.relative_to(resolved).as_posix()
            if "/" in name and name.split("/", 1)[0] in exclude:
                continue
            if ignore_bytecode and "__pycache__" in PurePosixPath(name).parts[:-1] and not path.is_symlink():
                continue
            if name not in manifest.files:
                problems.append(f"{name}: not in manifest")
    return problems, files


class IntakeGate:
    """고정 → 해시 → 스캔. 스캐너 예외와 바이트에 묶이지 않은 보고서는 deny."""

    def __init__(self, scanner: SecurityScanner, *, fail_on: Severity = "high") -> None:
        if _RANK.get(fail_on, _RANK["critical"]) > _RANK["high"]:
            raise ValueError("the intake gate cannot be weaker than blocking high findings")
        self.scanner = scanner
        self.fail_on = fail_on

    def evaluate(self, manifest: IntakeManifest, root: Path, exclude: tuple[str, ...] = (), *,
                 ignore_bytecode: bool = False) -> IntakeVerdict:
        """단위 하나를 판정한다."""
        digest = manifest.digest()

        def refuse(code: ReasonCode, problems: list[str], blocking: list[Finding] | None = None) -> IntakeVerdict:
            return IntakeVerdict(name=manifest.name, manifest_digest=digest, problems=problems,
                                 blocking=blocking or [], decision=deny(code, problems[0], by="intake_gate"))

        problems = check_pin(manifest)
        if problems:
            return refuse(ReasonCode.INTAKE_UNPINNED, problems)
        problems, files = read_snapshot(manifest, root, exclude, ignore_bytecode=ignore_bytecode)
        if problems:
            return refuse(ReasonCode.INTAKE_HASH_MISMATCH, problems)
        try:
            report = self.scanner.scan(files)
            # require_scan 과 같은 규칙: 테스트용 스캐너와 다른 바이트의 보고서는 통과 근거가 아니다.
            if report.scanner == "fake" or report.digest != files_digest(files):
                return refuse(ReasonCode.INTAKE_SCAN_BLOCKED, ["scan report is not valid for these bytes"])
        except Exception as exc:  # noqa: BLE001 - fail closed
            return refuse(ReasonCode.INTAKE_SCAN_BLOCKED, [f"scanner error: {type(exc).__name__}"])
        blocking = [finding for finding in report.findings if _RANK[finding.severity] >= _RANK[self.fail_on]]
        if blocking:
            return refuse(ReasonCode.INTAKE_SCAN_BLOCKED,
                          [f"{f.severity} {f.rule} {f.path}:{f.line}" for f in blocking], blocking)
        return IntakeVerdict(name=manifest.name, manifest_digest=digest, decision=allow("intake_gate"))


class IntakeRegistry:
    """통과한 매니페스트만 기록하고, 노출 시점에 다시 대조한다. 상태는 프로세스 메모리에만 있다."""

    def __init__(self) -> None:
        self._admitted: dict[str, str] = {}

    def admit(self, verdict: IntakeVerdict) -> bool:
        """허용 판정만 등록한다."""
        if not verdict.decision.allowed:
            return False
        self._admitted[verdict.name] = verdict.manifest_digest
        return True

    def revoke(self, name: str) -> None:
        """등록을 취소한다."""
        self._admitted.pop(name, None)

    def admitted(self) -> list[str]:
        """등록된 단위 이름."""
        return sorted(self._admitted)

    def assert_exposable(self, manifest: IntakeManifest) -> Decision:
        """툴·에이전트로 노출해도 되는지. 미등록이거나 등록 뒤 매니페스트가 바뀌었으면 deny."""
        admitted = self._admitted.get(manifest.name)
        if admitted is None:
            return deny(ReasonCode.INTAKE_NOT_ADMITTED, manifest.name, by="intake_registry")
        if admitted != manifest.digest():
            return deny(ReasonCode.INTAKE_HASH_MISMATCH, f"{manifest.name}: manifest changed since admission",
                        by="intake_registry")
        return allow("intake_registry")


def manifests_from_lock(lock: dict[str, Any] | str | Path, *, unit_dir: str = "plugins") -> list[IntakeManifest]:
    """LOCK.json → 매니페스트 목록.

    `<unit_dir>/<name>/...` 파일은 단위별로 나눠 단위 디렉터리 기준 상대경로로 바꾸고,
    나머지는 소스마다 `<source>/_root` 하나로 묶는다.
    """
    if isinstance(lock, (str, Path)):
        lock = json.loads(Path(lock).read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or lock.get("schema_version") != 1 or not isinstance(lock.get("sources"), dict):
        raise ValueError("unsupported LOCK.json (need schema_version 1 with 'sources')")
    manifests: list[IntakeManifest] = []
    for source, entry in sorted(lock["sources"].items()):
        if _UNIT_NAME.fullmatch(source) is None or not isinstance(entry, dict) \
                or not isinstance(entry.get("files"), dict) \
                or not isinstance(entry.get("generated_files", {}), dict):
            raise ValueError("invalid LOCK.json source")
        generated = entry.get("generated_files", {})
        if generated.keys() & entry["files"].keys():
            raise ValueError("LOCK.json lists a file twice")
        commit = str(entry.get("commit", ""))
        units: dict[str, dict[str, str]] = {}
        rest: dict[str, str] = {}
        for name, digest in {**entry["files"], **generated}.items():
            parts = PurePosixPath(name).parts
            if len(parts) >= 3 and parts[0] == unit_dir:
                if _UNIT_NAME.fullmatch(parts[1]) is None or parts[1] == "_root":
                    raise ValueError("invalid LOCK.json unit name")
                units.setdefault(parts[1], {})[PurePosixPath(*parts[2:]).as_posix()] = str(digest)
            else:
                rest[name] = str(digest)
        for unit, files in sorted(units.items()):
            manifests.append(IntakeManifest(name=f"{source}/{unit}", source=source, commit=commit, files=files))
        if rest:
            manifests.append(IntakeManifest(name=f"{source}/_root", source=source, commit=commit, files=rest))
    return manifests


def unit_root(base: Path, manifest: IntakeManifest, *, unit_dir: str = "plugins") -> tuple[Path, tuple[str, ...]]:
    """`manifests_from_lock` 매니페스트의 디스크 위치와 제외 디렉터리."""
    source, unit = manifest.name.split("/", 1)
    if unit == "_root":
        return base / source, (unit_dir,)
    return base / source / unit_dir / unit, ()


def check_coverage(base: Path, manifests: list[IntakeManifest], *, unit_dir: str = "plugins") -> list[str]:
    """어느 매니페스트의 검사 범위에도 들지 않는 항목: 잠금에 없는 소스·단위 디렉터리와 그 파일.

    단위별 판정만으로는 새로 끼워 넣은 플러그인 디렉터리가 보이지 않는다.
    """
    units: dict[str, set[str]] = {}
    for manifest in manifests:
        source, unit = manifest.name.split("/", 1)
        units.setdefault(source, set()).add(unit)
    problems = [f"{path.name}: source not in lock" for path in sorted(base.iterdir())
                if (path.is_dir() or path.is_symlink()) and path.name not in units]
    for source, names in sorted(units.items()):
        root = base / source
        if root.is_symlink() or (root / unit_dir).is_symlink():
            problems.append(f"{source}: symlinked source or unit directory")
            continue
        for path in _entries(root) if root.is_dir() else []:
            parts = path.relative_to(root).parts
            if len(parts) > 1 and parts[0] == unit_dir:
                covered = len(parts) > 2 and parts[1] in names - {"_root"}
            else:
                covered = "_root" in names
            if not covered:
                problems.append(f"{path.relative_to(base).as_posix()}: not in lock")
    return problems


def refused_plugins(plugin_root: Path, names: list[str], lock_path: Path) -> dict[str, str]:
    """카탈로그가 읽으려는 플러그인 중 도입 게이트를 통과하지 못한 것: 이름 -> reason_code.

    `plugin_root` 는 `<base>/<source>/<unit_dir>` 이다. 잠금을 읽지 못하면 전부 거부한다. 카탈로그는
    플러그인 코드를 실행하지 않으므로 `__pycache__` 는 무시한다.
    """
    source, unit_dir = plugin_root.parent.name, plugin_root.name
    try:
        manifests = {m.name: m for m in manifests_from_lock(lock_path, unit_dir=unit_dir)}
    except (OSError, ValueError):
        return dict.fromkeys(names, ReasonCode.INTAKE_NOT_ADMITTED.value)
    gate = IntakeGate(StaticScanner())
    refused: dict[str, str] = {}
    for name in names:
        manifest = manifests.get(f"{source}/{name}")
        if manifest is None or name == "_root":
            refused[name] = ReasonCode.INTAKE_NOT_ADMITTED.value
            continue
        verdict = gate.evaluate(manifest, plugin_root / name, ignore_bytecode=True)
        if not verdict.decision.allowed:
            refused[name] = verdict.decision.reason_code.value
    return refused
