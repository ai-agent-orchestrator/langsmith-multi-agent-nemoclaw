"""도메인 팩의 ToolSpec 에서 NemoClaw egress preset 을 생성/검증/drift 검사한다.

    python scripts/gen_nemoclaw_presets.py --domains travel,payment --security-mode masked        # nemoclaw/presets 갱신
    python scripts/gen_nemoclaw_presets.py --check                                               # CI: 커밋본과 diff, 검증
    python scripts/gen_nemoclaw_presets.py --strict                                              # placeholder 호스트 거부(운영용)
PAYMENT_PROVIDER_HOST 가 없으면 placeholder(payments.example.invalid)로 생성되며 --strict 에서는 실패한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestra import registry  # noqa: E402
from orchestra.sandbox.nemoclaw import policy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", default="travel,payment")
    ap.add_argument("--security-mode", default="masked", choices=["off", "masked", "closed", "self_hosted"])
    ap.add_argument("--langsmith-host", default=policy.LANGSMITH_HOST)
    ap.add_argument("--out", default=str(ROOT / "nemoclaw" / "presets"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--strict", action="store_true", help="placeholder 호스트를 오류로 취급")
    a = ap.parse_args()

    packs = registry.build_packs(a.domains.split(","), {})
    specs = [s for p in packs for _, s in p.tools().values()]
    presets = policy.build_presets(specs, security_mode=a.security_mode, langsmith_host=a.langsmith_host)
    errs = policy.validate_presets(presets, tool_specs=specs, allow_placeholder=not a.strict)
    if errs:
        print("INVALID:\n  - " + "\n  - ".join(errs))
        return 2
    out = Path(a.out)
    if a.check:
        problems = policy.diff_dir(presets, out)
        print("OK (no drift)" if not problems else "DRIFT:\n  - " + "\n  - ".join(problems))
        return 1 if problems else 0
    for p in policy.write_all(presets, out):
        print("wrote", p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
