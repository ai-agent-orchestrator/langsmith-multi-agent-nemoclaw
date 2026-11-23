"""Aggregate parallel reviewer outputs, dedupe, and drop low-confidence noise.

Pattern inspired by `code-review` / `pr-review-toolkit` (parallel sub-agents,
confidence cutoff 80). Independent illustrative implementation. Stdlib only.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

THRESHOLD = 80


@dataclass(frozen=True)
class Issue:
    file: str
    line: int
    summary: str
    confidence: int  # 0-100
    reviewer: str = ""


Reviewer = Callable[[str], list[Issue]]


def run_reviewers(diff: str, reviewers: dict[str, Reviewer]) -> list[Issue]:
    """Run reviewers in parallel; tag each issue with its reviewer name."""
    with ThreadPoolExecutor(max_workers=len(reviewers) or 1) as pool:
        futures = {name: pool.submit(fn, diff) for name, fn in reviewers.items()}
    out: list[Issue] = []
    for name, fut in futures.items():
        for i in fut.result():
            out.append(Issue(i.file, i.line, i.summary, i.confidence, name))
    return out


def aggregate(issues: Iterable[Issue], threshold: int = THRESHOLD) -> list[Issue]:
    """Keep issues >= threshold; for the same (file, line), keep the most confident.

    Agreement bonus: if several reviewers flag the same spot, confidence is
    raised by 5 per extra reviewer (capped at 100) before filtering.
    """
    groups: dict[tuple[str, int], list[Issue]] = {}
    for i in issues:
        groups.setdefault((i.file, i.line), []).append(i)
    kept: list[Issue] = []
    for group in groups.values():
        best = max(group, key=lambda x: x.confidence)
        boosted = min(100, best.confidence + 5 * (len(group) - 1))
        if boosted >= threshold:
            kept.append(Issue(best.file, best.line, best.summary, boosted, best.reviewer))
    return sorted(kept, key=lambda x: (-x.confidence, x.file, x.line))
