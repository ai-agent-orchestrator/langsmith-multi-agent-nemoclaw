"""Role-separated pipeline: Explorer -> Architect -> Implementer -> Reviewer.

Pattern inspired by `feature-dev`. Plain-Python state machine with an optional
LangGraph build (`pip install langgraph`). `llm` is any callable
(system_prompt, user_text) -> str; plug in your own model client.
"""
from __future__ import annotations

from typing import Callable, TypedDict

LLM = Callable[[str, str], str]

ROLES = {
    "explorer": "You explore the codebase and list relevant files and constraints.",
    "architect": "You design the approach and interfaces from the exploration notes.",
    "implementer": "You implement the design. Output code only.",
    "reviewer": "You review the implementation. Reply APPROVE or list required fixes.",
}
ORDER = ["explorer", "architect", "implementer", "reviewer"]


class State(TypedDict, total=False):
    task: str
    explorer: str
    architect: str
    implementer: str
    reviewer: str
    approved: bool
    rounds: int


def _node(role: str, llm: LLM, prompts: dict[str, str]) -> Callable[[State], State]:
    def run(s: State) -> State:
        ctx = "\n\n".join(f"[{r}]\n{s[r]}" for r in ORDER if r in s and r != role)
        out = llm(prompts[role], f"Task: {s['task']}\n\n{ctx}")
        update: State = {role: out}  # type: ignore[typeddict-item]
        if role == "reviewer":
            update["approved"] = out.strip().upper().startswith("APPROVE")
            update["rounds"] = s.get("rounds", 0) + 1
        return update

    return run


def run_pipeline(task: str, llm: LLM, prompts: dict[str, str] | None = None, max_rounds: int = 2) -> State:
    """Plain-Python version. Re-runs implementer+reviewer until approved or capped."""
    prompts = prompts or ROLES
    nodes = {r: _node(r, llm, prompts) for r in ORDER}
    state: State = {"task": task, "rounds": 0}
    for r in ("explorer", "architect"):
        state.update(nodes[r](state))
    while True:
        state.update(nodes["implementer"](state))
        state.update(nodes["reviewer"](state))
        if state["approved"] or state["rounds"] >= max_rounds:
            return state


def build_langgraph(llm: LLM, prompts: dict[str, str] | None = None, max_rounds: int = 2):
    """Same flow as a LangGraph graph (requires `langgraph`)."""
    from langgraph.graph import END, StateGraph

    prompts = prompts or ROLES
    g = StateGraph(State)
    for r in ORDER:
        g.add_node(r, _node(r, llm, prompts))
    g.set_entry_point("explorer")
    g.add_edge("explorer", "architect")
    g.add_edge("architect", "implementer")
    g.add_edge("implementer", "reviewer")
    g.add_conditional_edges(
        "reviewer",
        lambda s: END if s.get("approved") or s.get("rounds", 0) >= max_rounds else "implementer",
    )
    return g.compile()
