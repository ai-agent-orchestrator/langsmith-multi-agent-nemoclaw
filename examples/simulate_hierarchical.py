"""계층형 흐름 시뮬레이션 — LLM 없이 '실제 컴파일된 그래프'를 ScriptedChatModel 로 실행한다.

기존(print 전용) 스크립트와 달리 라우팅·툴 호출·필터·승인(interrupt)·샌드박스가 전부 진짜 코드 경로를 탄다.

실행(저장소 루트):
    python -I examples/simulate_hierarchical.py --list
    python -I examples/simulate_hierarchical.py                      # jeju_trip_payment
    python -I examples/simulate_hierarchical.py --scenario jeju_payment_rejected
    python -I examples/simulate_hierarchical.py --mode graph --json out/sim/jeju.json
옵션: --approve yes|no (시나리오의 approve 덮어쓰기) / --sandbox fake|off / --security-mode closed|masked|off
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.simulation import check_expect, list_scenarios, render, run_scenario  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="jeju_trip_payment")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mode", choices=["print", "graph"], default="print",
                    help="print: 흐름 서사 / graph: 서사 + 컴파일된 그래프(mermaid) + 검증 결과")
    ap.add_argument("--approve", choices=["yes", "no"], default=None)
    ap.add_argument("--sandbox", choices=["fake", "off"], default="fake")
    ap.add_argument("--security-mode", default="closed")
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args()

    if a.list:
        for s in list_scenarios():
            print(s)
        return 0

    approve = None if a.approve is None else [a.approve == "yes"] * 5
    res = run_scenario(a.scenario, sandbox=a.sandbox, approve=approve, security_mode=a.security_mode)
    print(render(res))
    errs = check_expect(res)

    if a.mode == "graph":
        print("\n--- 컴파일된 그래프 (mermaid) ---")
        for xray in (True, False):  # xray 는 같은 이름의 중첩 서브그래프에서 실패할 수 있어 평면 그래프로 폴백
            try:
                print(res.app.get_graph(xray=xray).draw_mermaid())
                break
            except Exception as e:  # noqa: BLE001
                print(f"(mermaid xray={xray} 실패: {type(e).__name__})")
    print("\n--- expect 검증 ---")
    print("PASS" if not errs else "FAIL\n  - " + "\n  - ".join(errs))

    if a.json_out:
        out = Path(a.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "scenario": a.scenario, "final": res.final, "interrupts": res.interrupts, "decisions": res.decisions,
            "errors": errs,
            "events": [{"seq": e.seq, "kind": e.kind, **e.data} for e in res.events.events],
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"saved: {out}")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
