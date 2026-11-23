"""NemoClawRunner (R1 / N1). CLI 래퍼 + 안전장치. 실제 CLI 문법은 `[VERIFY]` (R0 에서 확정).

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
from typing import Any, Callable, Optional, Protocol

from ..base import ExecRequest, ExecResult

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_OUTPUT_BYTES = 256 * 1024


class CommandError(RuntimeError):
    pass


@dataclass
class CommandTemplates:
    """`[VERIFY]` 실제 CLI 에 맞게 R0 에서 확정. 값은 argv 템플릿({sandbox},{preset},{file} 치환)."""
    policy_get: list[str] = field(default_factory=lambda: ["openshell", "policy", "get", "--full", "{sandbox}"])
    policy_add: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "policy-add", "{preset}"])
    status: list[str] = field(default_factory=lambda: ["nemoclaw", "{sandbox}", "status"])
    version: list[str] = field(default_factory=lambda: ["nemoclaw", "--version"])
    # 의도적으로 policy set(전체 교체) 템플릿은 두지 않는다 — 운영 변경은 policy-add 만.


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
        if not isinstance(value, str) or not _NAME_RE.match(value):
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
    def doctor(self) -> dict[str, Any]:
        """설치·버전·접근성 점검. 실패 항목을 이유와 함께 반환(예외 아님)."""
        v = self._run(self.t.version)
        ok = v.returncode == 0
        return {"ok": ok, "version": v.stdout.strip() if ok else None,
                "problem": None if ok else (v.stderr.strip() or f"exit {v.returncode}"), "dry_run": self.dry_run}

    def policy_get(self, sandbox: str) -> str:
        r = self._run(self.t.policy_get, sandbox=sandbox)
        if r.returncode != 0:
            raise CommandError(f"policy get failed for {sandbox}: {r.stderr.strip() or r.returncode}")
        return r.stdout

    def applied_presets(self, sandbox: str) -> list[str]:
        """drift 점검용 fetch_applied 로 그대로 사용."""
        return self.parse_presets(self.policy_get(sandbox))

    def policy_add(self, sandbox: str, preset: str) -> CommandResult:
        r = self._run(self.t.policy_add, sandbox=sandbox, preset=preset)
        if r.returncode != 0:
            raise CommandError(f"policy-add {preset} failed on {sandbox}: {r.stderr.strip() or r.returncode}")
        return r

    # ------------------------------------------------------------ SandboxRunner 계약
    def status(self, sandbox: str) -> dict[str, Any]:
        r = self._run(self.t.status, sandbox=sandbox)
        return {"sandbox": sandbox, "runner": "nemoclaw", "ok": r.returncode == 0,
                "raw": r.stdout.strip(), "error": r.stderr.strip() or None, "dry_run": r.dry_run}

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


def parse_presets_from_policy(text: str) -> list[str]:
    """`policy get --full` 출력에서 적용 preset 이름을 뽑는다. `[VERIFY]` 실제 출력 형식 확정 전 임시 규칙:
    `preset: <name>` 줄(YAML) 을 모두 수집한다."""
    return sorted(set(re.findall(r"(?m)^\s*preset:\s*([A-Za-z0-9_.-]+)\s*$", text)))
