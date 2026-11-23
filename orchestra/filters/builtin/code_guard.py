"""EXEC 툴에 넘어가는 코드/명령의 위험 패턴 차단 (O1). pre_tool 단계, `side_effect=EXEC` 툴 대상.

아이디어 출처: Claude `security-guidance` 플러그인(Apache-2.0)의 patterns.py 가 편집 시점에 경고하는 위험 API 목록.
여기서는 **런타임 차단용으로 Python/셸 코드 규칙을 새로 작성**했다(문구·정규식은 복사하지 않음; THIRD_PARTY.md 참고).
샌드박스가 1차 격리이고, 이 필터는 "격리 안에서도 실행하면 안 되는 것"을 미리 걸러 내는 2차 방어다.

규칙 심각도: block(기본) / warn(허용하되 feedback 점수 0.5 로 기록). 검사 대상 인자: code, script, command, cmd.
"""
from __future__ import annotations

import re
from typing import Any

from ...specs import SideEffect
from ..base import Filter, FilterContext, FilterDecision

CODE_ARGS = ("code", "script", "command", "cmd")

# (규칙명, 정규식, 심각도)
RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("dynamic_eval", re.compile(r"(?<![\w.])(eval|exec)\s*\("), "block"),
    ("dynamic_import", re.compile(r"__import__\s*\(|importlib\.import_module\s*\("), "block"),
    ("shell_true", re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "block"),
    ("os_system", re.compile(r"\bos\.(system|popen|exec[lv]p?e?|spawn\w*)\s*\("), "block"),
    ("unsafe_deserialization", re.compile(
        r"\b(pickle|cPickle|cloudpickle|dill|marshal|shelve)\.(loads?|Unpickler|open)\b"), "block"),
    ("unsafe_yaml", re.compile(r"\byaml\.(unsafe_load|load)\s*\((?![^)\n]*Safe)"), "block"),
    ("tls_verification_off", re.compile(r"\bverify\s*=\s*False\b|CERT_NONE|_create_unverified_context"), "block"),
    ("raw_socket", re.compile(r"\bsocket\.socket\s*\("), "block"),
    ("curl_pipe_shell", re.compile(r"(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "block"),
    ("destructive_rm", re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|\*)"), "block"),
    ("reads_secret_files", re.compile(r"(/etc/shadow|\.ssh/|\.aws/credentials|\.env\b|id_rsa)"), "block"),
    ("env_dump", re.compile(r"\bos\.environ\b(?!\s*\.get\(|\s*\[)|\bprintenv\b|(?<![\w])env\s*$"), "warn"),
    ("xml_unsafe_parse", re.compile(r"\b(ElementTree|ET|minidom)\.(parse|fromstring|parseString)\s*\("), "warn"),
]


def scan_code(text: str) -> list[tuple[str, str]]:
    return [(name, sev) for name, rx, sev in RULES if rx.search(text)]


def _texts(args: Any) -> list[str]:
    if isinstance(args, str):
        return [args]
    if isinstance(args, dict):
        return [v for k, v in args.items() if k in CODE_ARGS and isinstance(v, str)]
    return []


def code_guard(block_on_warn: bool = False) -> Filter:
    class _Guard:
        name = "code_guard"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            spec = ctx.tool_spec
            if spec is None or spec.side_effect != SideEffect.EXEC:
                return FilterDecision.allow()
            hits = [h for t in _texts(ctx.payload) for h in scan_code(t)]
            hard = sorted({n for n, s in hits if s == "block" or block_on_warn})
            if hard:
                return FilterDecision.block(f"dangerous code pattern: {', '.join(hard)}", code_guard=0.0)
            soft = sorted({n for n, s in hits if s == "warn"})
            if soft:
                d = FilterDecision.allow()
                d.reason = f"warn: {', '.join(soft)}"
                d.feedback = {"code_guard": 0.5}
                return d
            return FilterDecision.allow()

    return _Guard()
