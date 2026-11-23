"""Distribution and offline test configuration contracts (DESIGN §4, §13)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_includes_public_packages_and_extras():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert {"dev", "langsmith", "nemoclaw"} <= config["project"]["optional-dependencies"].keys()
    includes = config["tool"]["setuptools"]["packages"]["find"]["include"]
    assert {"orchestra*", "domains*", "travel_planner*"} <= set(includes)


def test_env_example_covers_design_settings():
    design = (ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
    section = design.split("## 13.", 1)[1].split("## 14.", 1)[0]
    required = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", section, re.MULTILINE))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    available = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    assert required <= available


def test_external_integration_markers_are_excluded_by_default():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    assert "not integration and not langsmith and not nemoclaw" in options["addopts"]
    assert options["testpaths"] == ["tests"]
    assert not (ROOT / "pytest.ini").exists()
