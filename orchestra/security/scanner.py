"""Vendor-neutral scanner contract and conservative offline code checks."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["info", "low", "medium", "high", "critical"]


class Finding(BaseModel):
    """Findings carry stable rule identifiers rather than source snippets or secrets."""

    model_config = ConfigDict(extra="forbid", strict=True)
    rule: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    severity: Severity
    path: str
    line: int = Field(default=1, ge=1)


class ScanReport(BaseModel):
    """Bind scan findings to the exact bytes that were checked."""

    model_config = ConfigDict(extra="forbid", strict=True)
    scanner: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[Finding] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """High and critical findings always fail the ingestion gate."""
        return not any(f.severity in {"high", "critical"} for f in self.findings)


def files_digest(files: Mapping[str, bytes]) -> str:
    """Hash canonical names and bytes together, detecting additions and removals."""
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SecurityScanner(Protocol):
    """A real product adapter must implement the same byte-bound contract."""

    def scan(self, files: Mapping[str, bytes]) -> ScanReport: ...


class FakeScanner:
    """Explicit test double; never silently substituted for a production scanner."""

    def __init__(self, findings: list[Finding] | None = None) -> None:
        self.findings = findings or []

    def scan(self, files: Mapping[str, bytes]) -> ScanReport:
        """Return injected findings bound to this input snapshot."""
        return ScanReport(scanner="fake", digest=files_digest(files), findings=self.findings)


class StaticScanner:
    """Small deterministic baseline, not a replacement for a full vulnerability scanner."""

    def scan(self, files: Mapping[str, bytes]) -> ScanReport:
        """Flag dynamic code execution and explicit shell=True calls via Python AST."""
        findings = []
        for path, raw in sorted(files.items()):
            if not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(raw)
            except (SyntaxError, ValueError, UnicodeError):
                findings.append(Finding(rule="python.parse", severity="high", path=path))
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                    findings.append(Finding(rule="python.dynamic_execution", severity="high", path=path, line=node.lineno))
                if any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                       for k in node.keywords):
                    findings.append(Finding(rule="python.shell_true", severity="high", path=path, line=node.lineno))
        return ScanReport(scanner="static-v1", digest=files_digest(files), findings=findings)


def from_audit_json(payload: str, files: Mapping[str, bytes]) -> ScanReport:
    """Consume a product-neutral audit export, rejecting stale input fingerprints."""
    report = ScanReport.model_validate_json(payload)
    if report.digest != files_digest(files):
        raise ValueError("scanner_snapshot_mismatch")
    return report


MAX_SARIF_BYTES = 5_000_000
_RULE_CHARS = re.compile(r"[^a-zA-Z0-9_.-]")
_LEVEL_SEVERITY: dict[str, Severity] = {"error": "high", "warning": "medium", "note": "low", "none": "info"}


def _sarif_severity(result: dict, rule: dict) -> Severity:
    """Prefer numeric security-severity (GitHub convention); otherwise map the SARIF level fail-closed."""
    score = (result.get("properties") or {}).get("security-severity", (rule.get("properties") or {}).get("security-severity"))
    try:
        value = float(score) if score is not None else None
    except (TypeError, ValueError):
        value = None
    if value is not None and 0 <= value <= 10:
        return "critical" if value >= 9 else "high" if value >= 7 else "medium" if value >= 4 else "low" if value > 0 else "info"
    # A missing level is "warning" by the SARIF spec; an unknown value is treated as high rather than ignored.
    return _LEVEL_SEVERITY.get(result.get("level", "warning"), "high")


def from_sarif(payload: str | bytes, files: Mapping[str, bytes], *, scanned_digest: str,
               scanner: str = "claude-security") -> ScanReport:
    """Convert a SARIF 2.1.0 log (e.g. CLAUDE-SECURITY-RESULTS.sarif) into a byte-bound report.

    SARIF carries no input fingerprint, so the operator must supply the digest recorded when the scan ran.
    Source snippets, messages and suppressions are dropped: a scanner export never lowers the gate.
    """
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > MAX_SARIF_BYTES or scanned_digest != files_digest(files):
        raise ValueError("scanner_snapshot_mismatch")
    try:
        log = json.loads(raw)
        runs = log["runs"]
        if log.get("version") != "2.1.0" or not isinstance(runs, list) or not runs:
            raise ValueError("invalid_sarif")
        findings: list[Finding] = []
        for run in runs:
            rules = {r.get("id"): r for r in (run["tool"]["driver"].get("rules") or []) if isinstance(r, dict)}
            for result in run.get("results") or []:
                rule_id = str(result.get("ruleId") or "unknown")
                location = (result.get("locations") or [{}])[0].get("physicalLocation", {})
                uri = str(location.get("artifactLocation", {}).get("uri", ""))
                path = uri.removeprefix("file://").lstrip("/") if uri.startswith("file://") else uri
                if path not in files:
                    raise ValueError("scanner_snapshot_mismatch")
                line = location.get("region", {}).get("startLine", 1)
                findings.append(Finding(rule=_RULE_CHARS.sub("_", rule_id)[:100] or "unknown",
                                        severity=_sarif_severity(result, rules.get(rule_id, {})), path=path,
                                        line=line if type(line) is int and line >= 1 else 1))
    except (KeyError, TypeError, AttributeError, IndexError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_sarif") from exc
    return ScanReport(scanner=scanner, digest=scanned_digest, findings=findings)


class SarifScanner:
    """Adapter for a scan the operator already ran; it never calls the product or the network itself."""

    def __init__(self, payload: str | bytes, scanned_digest: str, scanner: str = "claude-security") -> None:
        self.payload, self.scanned_digest, self.scanner = payload, scanned_digest, scanner

    def scan(self, files: Mapping[str, bytes]) -> ScanReport:
        """Reject exports whose recorded digest does not match the bytes being ingested."""
        return from_sarif(self.payload, files, scanned_digest=self.scanned_digest, scanner=self.scanner)


def require_scan(files: Mapping[str, bytes], scanner: SecurityScanner) -> ScanReport:
    """Reject failures, stale exports and test doubles before production ingestion."""
    report = scanner.scan(files)
    if report.scanner == "fake" or report.digest != files_digest(files) or not report.passed:
        raise ValueError("security_scan_failed")
    return report
