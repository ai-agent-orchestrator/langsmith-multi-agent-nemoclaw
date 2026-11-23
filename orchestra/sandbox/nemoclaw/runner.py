"""NemoClawRunner (R1 / N1). CLI 래퍼 + 안전장치. CLI 문법은 NemoClaw 포크 docs/reference/commands.mdx 로 확정(R0).

설계:
  * 명령은 allowlist(템플릿)로만 실행한다. 임의 문자열 실행 금지, 인자는 검증 후 argv 리스트로 전달(shell=False).
  * 실행기(Executor)를 주입한다 → 테스트는 FakeExecutor, 운영은 SubprocessExecutor.
  * dry_run=True 이면 실행하지 않고 argv 만 기록한다.
  * 출력 크기·시간 상한. 초과하면 잘라내고/실패 처리.
  * exec(): 샌드박스 내부 워커 전송(N2)이 없으면 **실패로 닫는다** (sandbox_unavailable).
명령 템플릿은 `CommandTemplates` 로 분리해 CLI 버전이 달라도 코드 변경 없이 교체한다.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..base import ExecRequest, ExecResult

from ...specs import SANDBOX_NAME_RE            # noqa: E402  (1~19자, 소문자 시작, 단일 하이픈)

_PRESET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_CUSTOM_KEY_PREFIX = "nemoclaw_custom__"      # src/lib/policy/index.ts: customPolicyKey()
MAX_OUTPUT_BYTES = 256 * 1024


class CommandError(RuntimeError):
    pass


@dataclass
class CommandTemplates:
    """docs/reference/commands.mdx 기준 (R0 실측). `{sandbox}`·`{preset}`·`{file}` 만 치환한다.
    policy add 는 `--yes` 필수(비대화). 변경 명령은 항상 `policy add`(상위 문서를 병합해 전체 문서를 제출·검증)이며,
    OpenShell 의 `policy set`(전체 교체) 템플릿은 의도적으로 두지 않는다."""
    version: list[str] = field(default_factory=lambda: ["nemoclaw", "--version"])
    doctor: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "doctor", "--json"])
    status: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "status", "--json"])
    policy_get: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "policy", "get"])
    policy_list: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "policy", "list"])
    policy_add_file: list[str] = field(default_factory=lambda: [
        "nemoclaw", "{sandbox}", "policy", "add", "--from-file", "{file}", "--yes"])
    policy_add_dry_run: list[str] = field(default_factory=lambda: [
        "nemoclaw", "{sandbox}", "policy", "add", "--from-file", "{file}", "--dry-run"])
    policy_remove: list[str] = field(default_factory=lambda: [
        "nemoclaw", "{sandbox}", "policy", "remove", "{preset}", "--yes"])


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    dry_run: bool = False


class Executor(Protocol):
    def run(self, argv: list[str], *, timeout_s: float) -> CommandResult: ...


class SubprocessExecutor:
    def run(self, argv: list[str], *, timeout_s: float) -> CommandResult:
        try:
            p = subprocess.run(argv, capture_output=True, timeout=timeout_s, shell=False)
        except subprocess.TimeoutExpired:
            return CommandResult(argv, 124, stderr="timeout")
        except FileNotFoundError:
            return CommandResult(argv, 127, stderr=f"command not found: {argv[0]}")
        out, err = p.stdout[:MAX_OUTPUT_BYTES], p.stderr[:MAX_OUTPUT_BYTES]
        return CommandResult(argv, p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                             truncated=len(p.stdout) > MAX_OUTPUT_BYTES or len(p.stderr) > MAX_OUTPUT_BYTES)


class NemoClawRunner:
    def __init__(self, executor: Optional[Executor] = None, templates: Optional[CommandTemplates] = None, *,
                 dry_run: bool = False, cmd_timeout_s: float = 30.0,
                 transport: Optional[Callable[[ExecRequest], ExecResult]] = None,
                 parse_presets: Optional[Callable[[str], list[str]]] = None):
        self.executor = executor or SubprocessExecutor()
        self.t = templates or CommandTemplates()
        self.dry_run = dry_run
        self.cmd_timeout_s = cmd_timeout_s
        self.transport = transport                    # N2: 샌드박스 워커로 보내는 전송 계층
        self.parse_presets = parse_presets or parse_presets_from_policy
        self.history: list[CommandResult] = []

    # ------------------------------------------------------------ 안전한 명령 실행
    @staticmethod
    def _check(name: str, value: str) -> str:
        rx = {"sandbox": SANDBOX_NAME_RE, "preset": _PRESET_RE}.get(name)
        if name == "file":
            p = Path(value) if isinstance(value, str) else None
            if p is None or not p.is_absolute() or ".." in p.parts or p.suffix not in (".yaml", ".yml") \
                    or not re.match(r"^[A-Za-z0-9_./-]+$", value):
                raise CommandError(f"invalid file: {value!r} (absolute .yaml path without '..' required)")
            return value
        if rx is None or not isinstance(value, str) or not rx.match(value):
            raise CommandError(f"invalid {name}: {value!r}")
        return value

    def _argv(self, template: list[str], **kw: str) -> list[str]:
        for k, v in kw.items():
            self._check(k, v)
        return [part.format(**kw) for part in template]

    def _run(self, template: list[str], **kw: str) -> CommandResult:
        argv = self._argv(template, **kw)
        if self.dry_run:
            res = CommandResult(argv, 0, dry_run=True)
        else:
            res = self.executor.run(argv, timeout_s=self.cmd_timeout_s)
        self.history.append(res)
        return res

    # ------------------------------------------------------------ 조회/변경
    def version(self) -> Optional[str]:
        r = self._run(self.t.version)
        return r.stdout.strip() if r.returncode == 0 else None

    def doctor(self, sandbox: Optional[str] = None) -> dict[str, Any]:
        """설치·버전(+샌드박스 지정 시 `doctor --json` 판정). 실패는 예외가 아니라 이유와 함께 반환."""
        v = self._run(self.t.version)
        out: dict[str, Any] = {"ok": v.returncode == 0, "version": v.stdout.strip() or None if v.returncode == 0 else None,
                               "problem": None if v.returncode == 0 else (v.stderr.strip() or f"exit {v.returncode}"),
                               "dry_run": self.dry_run}
        if sandbox and out["ok"]:
            d = self._run(self.t.doctor, sandbox=sandbox)
            out["sandbox"] = sandbox
            out["ok"] = d.returncode == 0              # 문서: 실패한 점검은 비0 종료 → 준비 게이트로 사용 가능
            out["report"] = _parse_json(d.stdout)
            if d.returncode != 0:
                out["problem"] = d.stderr.strip() or f"doctor exit {d.returncode}"
        return out

    def policy_get(self, sandbox: str) -> str:
        r = self._run(self.t.policy_get, sandbox=sandbox)
        if r.returncode != 0:
            raise CommandError(f"policy get failed for {sandbox}: {r.stderr.strip() or r.returncode}")
        return r.stdout

    def applied_presets(self, sandbox: str) -> list[str]:
        """drift 점검용 fetch_applied 로 그대로 사용. 사용자 정의 preset 은 live 정책의 `nemoclaw_custom__<preset>__<key>` 로 복원."""
        return self.parse_presets(self.policy_get(sandbox))

    def policy_add_file(self, sandbox: str, file: str, *, dry_run: bool = False) -> CommandResult:
        tpl = self.t.policy_add_dry_run if dry_run else self.t.policy_add_file
        r = self._run(tpl, sandbox=sandbox, file=file)
        if r.returncode != 0:
            raise CommandError(f"policy add {file} failed on {sandbox}: {r.stderr.strip() or r.returncode}")
        return r

    def policy_remove(self, sandbox: str, preset: str) -> CommandResult:
        r = self._run(self.t.policy_remove, sandbox=sandbox, preset=preset)
        if r.returncode != 0:
            raise CommandError(f"policy remove {preset} failed on {sandbox}: {r.stderr.strip() or r.returncode}")
        return r

    # ------------------------------------------------------------ SandboxRunner 계약
    def status(self, sandbox: str) -> dict[str, Any]:
        r = self._run(self.t.status, sandbox=sandbox)
        return {"sandbox": sandbox, "runner": "nemoclaw", "ok": r.returncode == 0,
                "report": _parse_json(r.stdout), "error": r.stderr.strip() or None, "dry_run": r.dry_run}

    def exec(self, req: ExecRequest) -> ExecResult:
        if self.transport is None:                    # 워커 전송(N2) 미구현/미설정 → 닫는다
            return ExecResult(ok=False, error="sandbox_unavailable: worker transport not configured")
        t0 = time.perf_counter()
        try:
            res = self.transport(req)
        except Exception as exc:
            return ExecResult(ok=False, error=f"sandbox_unavailable: {type(exc).__name__}: {exc}")
        if not res.duration_ms:
            res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res


def _parse_json(text: str) -> Any:
    import json
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_presets_from_policy(text: str) -> list[str]:
    """`nemoclaw <sb> policy get` 출력(YAML)에서 적용된 사용자 정의 preset 이름을 복원한다.
    NemoClaw 는 `--from-file` 로 적용한 preset 의 정책 키를 `nemoclaw_custom__<preset>__<원래키>` 로 바꿔 live 정책에 넣는다
    (NemoClaw src/lib/policy/index.ts). 파싱 불가/비정상 문서는 빈 목록 → drift 점검이 'missing' 으로 기동을 막는다(fail-closed).
    `[VERIFY]` 설치 버전에서 `policy get` 실출력으로 한 번 대조."""
    import yaml
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    pols = doc.get("network_policies") if isinstance(doc, dict) else None
    if not isinstance(pols, dict):
        return []
    names = set()
    for key in pols:
        if isinstance(key, str) and key.startswith(_CUSTOM_KEY_PREFIX):
            rest = key[len(_CUSTOM_KEY_PREFIX):]
            sep = rest.find("__")
            if sep > 0:
                names.add(rest[:sep])
    return sorted(names)
