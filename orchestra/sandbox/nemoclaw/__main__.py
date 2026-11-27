"""Read-only NemoClaw diagnostics; no onboarding, policy mutation or inference calls."""
from __future__ import annotations

import argparse
import json

from ...org import load_org
from ...registry import build_packs, resolve
from .bootstrap import make_runner


def main(argv: list[str] | None = None) -> int:
    """Resolve a declared organization and diagnose its sandbox contracts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor"])
    parser.add_argument("--org", required=True)
    parser.add_argument("--security-mode", choices=["closed", "masked", "self_hosted"], default="closed")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        org = load_org(args.org)
        resolved = resolve(org, build_packs(org.domains, {}))
        runner = make_runner([spec for _, spec in resolved.tools.values()],
                             {a.name: set(a.tools) for a in resolved.org.agents.values()},
                             security_mode=args.security_mode, dry_run=args.dry_run)
        if args.dry_run:
            result = {"dry_run": True, "commands": [runner.cli.command("policy_get", sandbox=s) for s in runner.expected]}
        else:
            result = runner.doctor()
        print(json.dumps(result))
        return 0 if args.dry_run or result.get("ok") else 1
    except Exception:
        print(json.dumps({"ok": False, "error": "doctor_configuration_or_runtime_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
