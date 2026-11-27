"""플러그인 도입 게이트 회귀 테스트 (LLM·네트워크·NemoClaw 불필요)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

import pytest

from orchestra.security.decision import ReasonCode
from orchestra.security.intake import (
    IntakeGate,
    IntakeManifest,
    IntakeRegistry,
    check_coverage,
    manifests_from_lock,
    unit_root,
)
from orchestra.security.scanner import (
    FakeScanner,
    Finding,
    ScanReport,
    StaticScanner,
    files_digest,
)

SHA = "a" * 40


class _Scanner:
    def __init__(self, findings=(), name="test-scanner"):
        self.findings, self.name, self.seen = list(findings), name, []

    def scan(self, files: Mapping[str, bytes]) -> ScanReport:
        self.seen.append(dict(files))
        return ScanReport(scanner=self.name, digest=files_digest(files), findings=self.findings)


def _plugin(tmp_path: Path, files: dict[str, str]) -> tuple[Path, IntakeManifest]:
    root = tmp_path / "p"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    manifest = IntakeManifest(name="src/p", commit=SHA, files={
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()})
    return root, manifest


FILES = {".claude-plugin/plugin.json": "{}", "skills/s/SKILL.md": "# s\n"}


def test_clean_plugin_is_admitted_and_scanner_sees_the_hashed_bytes(tmp_path):
    root, manifest = _plugin(tmp_path, FILES)
    scanner = _Scanner()
    verdict = IntakeGate(scanner).evaluate(manifest, root)
    assert verdict.decision.allowed, verdict.problems
    assert scanner.seen == [{name: text.encode() for name, text in FILES.items()}]
    registry = IntakeRegistry()
    assert registry.admit(verdict) and registry.admitted() == ["src/p"]
    assert registry.assert_exposable(manifest).allowed


@pytest.mark.parametrize("commit", ["main", "v1.2.0", "abc1234", "A" * 40, "a" * 40 + "\n", ""])
def test_unpinned_commit_denied(tmp_path, commit):
    root, manifest = _plugin(tmp_path, FILES)
    verdict = IntakeGate(_Scanner()).evaluate(manifest.model_copy(update={"commit": commit}), root)
    assert verdict.decision.reason_code == ReasonCode.INTAKE_UNPINNED


def test_modified_missing_unlisted_and_oversized_files_denied(tmp_path):
    root, manifest = _plugin(tmp_path / "a", FILES)
    (root / "skills/s/SKILL.md").write_text("changed\n", encoding="utf-8")
    verdict = IntakeGate(_Scanner()).evaluate(manifest, root)
    assert verdict.decision.reason_code == ReasonCode.INTAKE_HASH_MISMATCH and "sha256 mismatch" in verdict.problems[0]

    root, manifest = _plugin(tmp_path / "b", FILES)
    (root / "__pycache__").mkdir()
    (root / "__pycache__/x.pyc").write_bytes(b"x")
    assert any("not in manifest" in p for p in IntakeGate(_Scanner()).evaluate(manifest, root).problems)

    root, manifest = _plugin(tmp_path / "c", FILES)
    (root / ".claude-plugin/plugin.json").unlink()
    assert any("missing" in p for p in IntakeGate(_Scanner()).evaluate(manifest, root).problems)

    root, manifest = _plugin(tmp_path / "d", {"big.md": "x" * 2_000_001})
    assert any("too large" in p for p in IntakeGate(_Scanner()).evaluate(manifest, root).problems)


def test_symlinks_and_path_escape_denied(tmp_path):
    root, manifest = _plugin(tmp_path, FILES)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plugin.json").write_text("{}", encoding="utf-8")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    assert any(p.startswith("linked-dir") for p in IntakeGate(_Scanner()).evaluate(manifest, root).problems)
    (root / "linked-dir").unlink()
    target = root / ".claude-plugin/plugin.json"
    target.unlink()
    target.symlink_to(outside / "plugin.json")
    verdict = IntakeGate(_Scanner()).evaluate(manifest, root)
    assert verdict.decision.reason_code == ReasonCode.INTAKE_HASH_MISMATCH
    assert any("symlink" in p for p in verdict.problems)
    bad = manifest.model_copy(update={"files": {"../outside/plugin.json": "0" * 64}})
    assert any("unsafe path" in p for p in IntakeGate(_Scanner()).evaluate(bad, root).problems)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert not IntakeGate(_Scanner()).evaluate(manifest, alias).decision.allowed


def test_blocking_findings_threshold_cannot_be_weakened(tmp_path):
    root, manifest = _plugin(tmp_path, FILES)
    high = Finding(rule="hook.exfiltration", severity="high", path="skills/s/SKILL.md")
    verdict = IntakeGate(_Scanner([high])).evaluate(manifest, root)
    assert verdict.decision.reason_code == ReasonCode.INTAKE_SCAN_BLOCKED and verdict.blocking == [high]
    medium = Finding(rule="m", severity="medium", path="skills/s/SKILL.md")
    assert IntakeGate(_Scanner([medium])).evaluate(manifest, root).decision.allowed
    assert not IntakeGate(_Scanner([medium]), fail_on="medium").evaluate(manifest, root).decision.allowed
    with pytest.raises(ValueError):
        IntakeGate(_Scanner(), fail_on="critical")


def test_scanner_failure_fake_and_stale_reports_fail_closed(tmp_path):
    root, manifest = _plugin(tmp_path, FILES)

    class Boom:
        def scan(self, files):
            raise RuntimeError

    class Stale:
        def scan(self, files):
            return ScanReport(scanner="old", digest="0" * 64)

    for scanner in (Boom(), Stale(), FakeScanner()):
        verdict = IntakeGate(scanner).evaluate(manifest, root)
        assert verdict.decision.reason_code == ReasonCode.INTAKE_SCAN_BLOCKED


def test_registry_rejects_unadmitted_failed_and_changed_manifest(tmp_path):
    root, manifest = _plugin(tmp_path, FILES)
    registry = IntakeRegistry()
    assert registry.assert_exposable(manifest).reason_code == ReasonCode.INTAKE_NOT_ADMITTED
    bad = IntakeGate(_Scanner()).evaluate(manifest.model_copy(update={"commit": "main"}), root)
    assert registry.admit(bad) is False
    registry.admit(IntakeGate(_Scanner()).evaluate(manifest, root))
    changed = manifest.model_copy(update={"files": {**manifest.files, "extra.md": "1" * 64}})
    assert registry.assert_exposable(changed).reason_code == ReasonCode.INTAKE_HASH_MISMATCH
    registry.revoke("src/p")
    assert registry.assert_exposable(manifest).reason_code == ReasonCode.INTAKE_NOT_ADMITTED


def _lock(tmp_path: Path) -> Path:
    base = tmp_path / "third_party"
    files = {"plugins/alpha/plugin.json": "{}", "plugins/alpha/agents/a.md": "a",
             "plugins/beta/plugin.json": "{}", "NOTICE.md": "n"}
    generated = {"__init__.py": '"""marker"""\n'}
    for name, text in {**files, **generated}.items():
        path = base / "claude_plugins" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    def digests(entries):
        return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in entries.items()}

    lock = {"schema_version": 1, "sources": {"claude_plugins": {
        "commit": SHA, "files": digests(files), "generated_files": digests(generated)}}}
    path = base / "LOCK.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


def test_manifests_from_lock_split_per_plugin_and_verify(tmp_path):
    lock = _lock(tmp_path)
    manifests = manifests_from_lock(lock)
    assert [m.name for m in manifests] == ["claude_plugins/alpha", "claude_plugins/beta", "claude_plugins/_root"]
    assert set(manifests[0].files) == {"plugin.json", "agents/a.md"}
    assert set(manifests[2].files) == {"NOTICE.md", "__init__.py"}
    for manifest in manifests:
        root, exclude = unit_root(lock.parent, manifest)
        assert IntakeGate(_Scanner()).evaluate(manifest, root, exclude).decision.allowed, manifest.name
    assert check_coverage(lock.parent, manifests) == []


@pytest.mark.parametrize("stray", [
    "claude_plugins/plugins/evil/plugin.json", "claude_plugins/plugins/loose.py", "other_source/x.py"])
def test_unlisted_plugins_and_sources_are_not_silently_skipped(tmp_path, stray):
    lock = _lock(tmp_path)
    path = lock.parent / stray
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert check_coverage(lock.parent, manifests_from_lock(lock))


def test_loose_source_file_is_reported_even_without_a_root_manifest(tmp_path):
    lock = _lock(tmp_path)
    data = json.loads(lock.read_text(encoding="utf-8"))
    record = data["sources"]["claude_plugins"]
    record["files"] = {name: digest for name, digest in record["files"].items() if name.startswith("plugins/")}
    record["generated_files"] = {}
    manifests = manifests_from_lock(data)
    assert [m.name for m in manifests] == ["claude_plugins/alpha", "claude_plugins/beta"]
    assert {p.split(":")[0] for p in check_coverage(lock.parent, manifests)} == {
        "claude_plugins/NOTICE.md", "claude_plugins/__init__.py"}


def test_symlinked_source_directory_is_reported(tmp_path):
    lock = _lock(tmp_path)
    source = lock.parent / "claude_plugins"
    moved = tmp_path / "moved"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=True)
    assert check_coverage(lock.parent, manifests_from_lock(lock))


@pytest.mark.parametrize("lock", [
    {"schema_version": 2, "sources": {}},
    {"schema_version": 1, "sources": {"../x": {"files": {}}}},
    {"schema_version": 1, "sources": {"s": {"files": {"plugins/../a": "0"}}}},
    {"schema_version": 1, "sources": {"s": {"files": {"a": "0"}, "generated_files": {"a": "1"}}}},
])
def test_manifests_from_lock_rejects_unsafe_input(lock):
    with pytest.raises(ValueError):
        manifests_from_lock(lock)


def test_intake_gate_cli(tmp_path, capsys):
    from scripts.intake_gate import main

    lock = _lock(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    args = ["--lock", str(lock), "--reports-dir", str(reports)]
    assert main(args) == 1                          # 보고서 없음 = 통과 아님
    assert "FAIL claude_plugins/alpha INTAKE_SCAN_BLOCKED" in capsys.readouterr().out

    assert main(["--lock", str(lock), "--print-digests"]) == 0
    digests = {line.split()[1]: line.split()[2] for line in capsys.readouterr().out.splitlines()
               if line.startswith("DIGEST")}
    for unit, digest in digests.items():
        (reports / f"{unit.replace('/', '__')}.report.json").write_text(
            json.dumps({"scanner": "external", "digest": digest, "findings": []}), encoding="utf-8")
    assert main(args) == 0

    (reports / "claude_plugins__beta.report.json").write_text(json.dumps({
        "scanner": "external", "digest": digests["claude_plugins/beta"],
        "findings": [{"rule": "x", "severity": "critical", "path": "plugin.json"}]}), encoding="utf-8")
    capsys.readouterr()
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "FAIL claude_plugins/beta INTAKE_SCAN_BLOCKED" in out and "PASS claude_plugins/alpha" in out
    assert main(["--lock", str(tmp_path / "nope.json"), "--static"]) == 2


def test_intake_gate_cli_static_mode(tmp_path):
    from scripts.intake_gate import main

    lock = _lock(tmp_path)
    assert main(["--lock", str(lock), "--static"]) == 0
    assert StaticScanner().scan({"a.py": b"eval('1')"}).findings
    (lock.parent / "claude_plugins/plugins/alpha/extra.md").write_text("x", encoding="utf-8")
    assert main(["--lock", str(lock), "--static"]) == 1
