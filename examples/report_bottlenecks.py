"""토큰 비용 / 지연시간 병목 리포트.

  python -I examples/report_bottlenecks.py --demo                 # 합성 데이터 (LangSmith 불필요)
  python -I examples/report_bottlenecks.py --project my-proj --pattern hierarchical --hours 24
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travel_planner.monitoring import fetch_runs, render_report, summarize_runs  # noqa: E402


def demo_runs():
    t0 = datetime(2026, 11, 20, tzinfo=timezone.utc)

    def run(name, sec, tokens, error=None):
        return SimpleNamespace(name=name, start_time=t0, end_time=t0 + timedelta(seconds=sec),
                               total_tokens=tokens, error=error)

    runs = [run("travel_director", 2.0, 1800) for _ in range(20)]
    runs += [run("planning_lead", 3.0, 2500) for _ in range(20)]
    runs += [run("restaurant_agent", 1.5, 900) for _ in range(19)] + [run("restaurant_agent", 20.0, 900)]
    runs += [run("hotel_agent", 2.5, 700, error="MCP connection reset" if i < 3 else None) for i in range(20)]
    return runs


ap = argparse.ArgumentParser()
ap.add_argument("--demo", action="store_true")
ap.add_argument("--project")
ap.add_argument("--pattern")
ap.add_argument("--hours", type=int, default=24)
args = ap.parse_args()

if not args.demo and not args.project:
    ap.error("--demo 또는 --project 가 필요합니다")

runs = demo_runs() if args.demo else fetch_runs(args.project, hours=args.hours, pattern=args.pattern)
print(render_report(summarize_runs(runs)))
