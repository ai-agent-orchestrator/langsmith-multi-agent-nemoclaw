"""Offline gate for source scans, scripted security invariants and optional garak exports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestra.security.invariants import check_invariants  # noqa: E402
from orchestra.security.scanner import StaticScanner  # noqa: E402
from orchestra.simulation import check_expect, list_scenarios, run_scenario  # noqa: E402


def garak_failures(path: Path, *, max_failure_rate: float = 0.0) -> list[str]:
    """Consume eval records; require numeric counts and coverage for all three probe families."""
    families = {"promptinject", "encoding", "xss"}
    seen: set[str] = set()
    errors = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("entry_type") != "eval":
            continue
        probe = row.get("probe", "").split(".")[0]
        if probe not in families:
            continue
        passed, total = row.get("passed"), row.get("total")
        if type(passed) is not int or type(total) is not int or not 0 <= passed <= total or total == 0:
            errors.append("invalid_garak_counts")
            continue
        seen.add(probe)
        if (total - passed) / total > max_failure_rate:
            errors.append("garak_failure_threshold")
    if seen != families:
        errors.append("garak_probe_coverage_missing")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Security gate errors are never swallowed by observation safe_call."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--garak-report", type=Path)
    args = parser.parse_args(argv)
    try:
        files = {p.relative_to(ROOT).as_posix(): p.read_bytes()
                 for directory in ("orchestra", "domains", "scripts") for p in (ROOT / directory).rglob("*.py")}
        report = StaticScanner().scan(files)
        errors = ["source_scan_failed"] if not report.passed else []
        for scenario in list_scenarios():
            result = run_scenario(scenario)
            if check_expect(result) or not all(check_invariants(result.events.events).values()):
                errors.append("scenario_security_failed:" + scenario)
        if args.garak_report:
            errors.extend(garak_failures(args.garak_report))
        print(json.dumps({"ok": not errors, "errors": errors, "scan": report.model_dump(),
                          "garak": "validated" if args.garak_report else "not_run"}))
        return int(bool(errors))
    except Exception:
        print(json.dumps({"ok": False, "errors": ["security_gate_input_failed"]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
