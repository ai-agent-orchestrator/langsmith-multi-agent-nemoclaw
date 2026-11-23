"""Guardrail module: regex pre-filter + optional LLM verification.

Pattern inspired by the `security-guidance` plugin (regex + LLM hybrid).
This is an independent illustrative implementation, NOT the upstream
security_reminder_hook.py. Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# (rule id, compiled pattern, message)
RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("eval-exec", re.compile(r"\b(eval|exec)\s*\("), "Dynamic code execution"),
    ("shell-true", re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "shell=True command injection risk"),
    ("sql-concat", re.compile(r"(SELECT|INSERT|UPDATE|DELETE)[^\n]*(\+|%s|\{)\s*\w*", re.I), "Possible SQL string building"),
    ("hardcoded-secret", re.compile(r"(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]", re.I), "Hardcoded secret"),
    ("rm-rf", re.compile(r"\brm\s+-rf\s+/(?!\w)"), "Destructive shell command"),
]


@dataclass
class Finding:
    rule_id: str
    message: str
    line: int


@dataclass
class Verdict:
    allowed: bool
    findings: list[Finding]


# An LLM verifier receives (text, findings) and returns the findings it confirms.
Verifier = Callable[[str, list[Finding]], list[Finding]]


def scan(text: str) -> list[Finding]:
    """Stage 1: cheap regex scan."""
    found: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule_id, pattern, message in RULES:
            if pattern.search(line):
                found.append(Finding(rule_id, message, lineno))
    return found


def check(text: str, verifier: Optional[Verifier] = None) -> Verdict:
    """Stage 2: optionally let an LLM drop false positives."""
    findings = scan(text)
    if findings and verifier is not None:
        findings = verifier(text, findings)
    return Verdict(allowed=not findings, findings=findings)


def guard(fn: Callable[[str], str], verifier: Optional[Verifier] = None) -> Callable[[str], str]:
    """Wrap a text-in/text-out function (e.g. an agent call) as middleware.

    Checks both input and output; raises PermissionError when blocked.
    """

    def wrapped(text: str) -> str:
        v = check(text, verifier)
        if not v.allowed:
            raise PermissionError(f"input blocked: {[f.rule_id for f in v.findings]}")
        out = fn(text)
        v = check(out, verifier)
        if not v.allowed:
            raise PermissionError(f"output blocked: {[f.rule_id for f in v.findings]}")
        return out

    return wrapped
