"""SARIF ingestion must stay byte-bound and fail closed on anything unexpected."""
import json

import pytest

from orchestra.security.scanner import SarifScanner, files_digest, from_sarif, require_scan

FILES = {"pkg/a.py": b"print(1)\n", "pkg/b.py": b"print(2)\n"}
DIGEST = files_digest(FILES)


def sarif(results, rules=None, version="2.1.0"):
    return json.dumps({"version": version, "runs": [{"tool": {"driver": {"name": "x", "rules": rules or []}},
                                                      "results": results}]})


def result(rule="CWE-78", level="warning", uri="pkg/a.py", line=3, **extra):
    return {"ruleId": rule, "level": level, "message": {"text": "secret snippet"},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": line}}}], **extra}


def test_clean_log_passes_and_binds_digest():
    report = require_scan(FILES, SarifScanner(sarif([]), DIGEST))
    assert report.scanner == "claude-security" and report.digest == DIGEST and report.passed


@pytest.mark.parametrize("level,severity,passes", [("error", "high", False), ("warning", "medium", True),
                                                   ("note", "low", True), ("bogus", "high", False)])
def test_levels_map_fail_closed(level, severity, passes):
    report = from_sarif(sarif([result(level=level)]), FILES, scanned_digest=DIGEST)
    assert report.findings[0].severity == severity and report.passed is passes
    assert "snippet" not in report.model_dump_json()


def test_security_severity_overrides_level_and_critical_fails():
    rules = [{"id": "R1", "properties": {"security-severity": "9.8"}}]
    report = from_sarif(sarif([result(rule="R1", level="note")], rules), FILES, scanned_digest=DIGEST)
    assert report.findings[0].severity == "critical" and not report.passed


def test_suppressions_do_not_lower_gate_and_rule_ids_are_sanitised():
    item = result(rule="a b/c;d", level="error", suppressions=[{"kind": "inSource"}])
    report = from_sarif(sarif([item]), FILES, scanned_digest=DIGEST)
    assert report.findings[0].rule == "a_b_c_d" and not report.passed


def test_file_uri_and_missing_line_are_normalised():
    item = result(uri="file:///pkg/b.py")
    item["locations"][0]["physicalLocation"].pop("region")
    finding = from_sarif(sarif([item]), FILES, scanned_digest=DIGEST).findings[0]
    assert (finding.path, finding.line) == ("pkg/b.py", 1)


def test_stale_digest_and_unknown_paths_are_rejected():
    with pytest.raises(ValueError, match="scanner_snapshot_mismatch"):
        from_sarif(sarif([]), {**FILES, "pkg/c.py": b"x"}, scanned_digest=DIGEST)
    with pytest.raises(ValueError, match="scanner_snapshot_mismatch"):
        from_sarif(sarif([result(uri="other.py")]), FILES, scanned_digest=DIGEST)


@pytest.mark.parametrize("payload", ["not json", "{}", sarif([], version="2.0.0"), json.dumps({"version": "2.1.0", "runs": []}),
                                     json.dumps({"version": "2.1.0", "runs": [{}]}), sarif([{"ruleId": "x"}])])
def test_malformed_logs_are_rejected(payload):
    with pytest.raises(ValueError):
        from_sarif(payload, FILES, scanned_digest=DIGEST)


def test_oversized_log_is_rejected():
    with pytest.raises(ValueError):
        from_sarif("x" * 5_000_001, FILES, scanned_digest=DIGEST)
