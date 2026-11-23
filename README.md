# Multi-Agent Orchestra — Hierarchical Supervisor × LangSmith × NemoClaw

[한국어](README.ko.md) · **English**

A design-and-implementation repo for organizing specialist agents (weather, restaurants, lodging, payment…) into a
**director → team lead → worker** hierarchy, with **swappable domain packs**, a **5-stage security filter chain**, and
**NemoClaw/OpenShell sandboxes** that isolate risky actions (payments, code execution).
Real compiled graphs run end to end from scenarios **without any LLM, network, or NemoClaw**.

> **Status (2026-11-22)** — The core is implemented and tested (60 tests); the rest is handed off to Codex.
> Real LLMs, a real LangSmith server, and **real NemoClaw integration were not verified in the authoring environment**
> (see [NemoClaw supplement plan](#nemoclaw-supplement-plan)).

| Doc | Contents |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Full design: architecture, specs, 24-feature LangSmith map, NemoClaw placement |
| [`docs/CODEX_HANDOFF.md`](docs/CODEX_HANDOFF.md) | **Handoff**: what is done / remaining tasks C1–C15 / design deviations / constraints |
| [`docs/NEMOCLAW_ROADMAP.md`](docs/NEMOCLAW_ROADMAP.md) | NemoClaw plan N1–N7, limits, post-install checklist |
| [`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md) | **Multi-agent-era security design**, NemoClaw hardening, Claude Security integration |
| [`AGENTS.md`](AGENTS.md) | Working rules and PR template for Codex |

## Quick start

Use Python 3.11+ and install with `pip install -e ".[dev]"`.
`requirements.txt` delegates to this same dependency definition. The `langsmith`
extra names the SDK already needed by the compatibility API; `nemoclaw` and
`claude-cli` are dependency-free extras for separately installed external CLIs.
They do not install or verify those CLIs. Examples currently use repository data,
so run them from this checkout. Tests exclude real-service markers by default;
opt in explicitly with `pytest -m integration`, `-m langsmith`, or `-m nemoclaw`.

```bash
pip install -r requirements.txt
python -m pytest -q tests                                   # 60 passed
python -I examples/simulate_hierarchical.py --list          # 5 scenarios
python -I examples/simulate_hierarchical.py                 # Jeju trip + hotel hold + approved payment
python -I examples/simulate_hierarchical.py --scenario jeju_payment_rejected
python -I examples/simulate_hierarchical.py --scenario sandbox_egress_denied --mode graph
python scripts/gen_nemoclaw_presets.py --check              # NemoClaw egress preset drift check
```

## What is implemented

Third-party snapshots are pinned in `third_party/LOCK.json` with per-file SHA-256
hashes. Run `python scripts/sync_third_party.py --check` without network access to
check integrity. To update a snapshot, use only this script:

```bash
python scripts/sync_third_party.py --component claude_plugins --source /path/to/claude-10-plugin-for-multi-agent --commit <40-character-sha>
python scripts/sync_third_party.py --component multi_agent_module_lib --source /path/to/multi-agent-module-lib --commit <40-character-sha>
pytest -q tests/third_party
```

`--source` also accepts the component's allowlisted HTTPS Git URL. Source trees
are never modified; selected Git blobs are copied unchanged. See
`third_party/NOTICE.md`: plugin Apache licenses are retained, while the pinned
module-lib examples have no upstream LICENSE and need license clarification.
Vendored hooks are catalog data and are never executed during synchronization.

```
orchestra/                    domain-agnostic core
  specs.py  org.py            declarative OrgSpec, validation, automatic fan-out splitting (≤5 options per lead)
  filters/                    FilterChain (5 stages) + built-ins + tool wrapper (approval/sandbox/last-line defense)
  patterns/                   hierarchical_graph.py, hierarchical_tools.py  <- hierarchy + handoff + report/escalate
  models/                     ScriptedChatModel (run real graphs without an LLM), ModelFactory, privacy routing
  sandbox/                    SandboxRunner contract, fake runner, nemoclaw/policy.py (egress preset generation/validation)
  hitl.py runtime.py service.py simulation.py
domains/                      swappable domain packs
  travel/                     17 tools; bookings stop at a temporary hold
  payment/                    intent -> user approval -> capture -> confirm; state machine, idempotency, limits, hold expiry
orgs/travel_payment.yaml      org chart (1 director + 4 leads + 10 workers)
scenarios/*.yaml              5 scenarios shared by simulation and tests
nemoclaw/presets/*.yaml       egress policies generated from ToolSpecs (CI drift check)
```

### Key design decisions
- **Swappable domains**: tools, agents, filters, and scenarios live in `domains/*`. The core never imports a domain (enforced by a test).
- **The LLM never decides the amount**: `create_payment_intent(hold_ids)` computes the total server-side from holds.
- **Layered approval**: amount filter -> `side_effect_policy` (COMMIT needs approval) -> **tool-wrapper last-line defense** (no approval-ledger entry, no capture, even with all filters disabled). Approvals are one-shot; no answer means denial.
- **Safe interrupt/resume**: tool nodes re-execute on resume, yet the payment is captured exactly once (pinned by a test).
- **Fail-closed**: a filter exception becomes BLOCK; sandbox mode without a runner aborts execution.
- **Hierarchy bug fixed**: `langgraph_supervisor` adds a static `member->supervisor` edge per member, which woke the team lead mid-handoff; fixed with `_detach_to_supervisor`.
- **NemoClaw egress policy is generated from declarations**: the same host:port opened in two sandboxes fails validation; inference-provider hosts, `access: full`, and wildcards are rejected; `closed` mode omits the LangSmith preset entirely.

### Sample run (`python -I examples/simulate_hierarchical.py`, payment segment)
Only routing is scripted; the **real graph, filter, approval, and sandbox code paths** all execute. Values are mock.

```text
  [LLM #19] payment_lead: checkout_agent 호출 → 호출 transfer_to_checkout_agent
⑫ checkout_agent (실무자)
  [LLM #20] checkout_agent: 호출 create_payment_intent(hold_ids=['hold_0001'])
      [FILTER] pre_tool idempotency(create_payment_intent): modify — idempotency_key injected
      [FILTER] pre_tool side_effect_policy(create_payment_intent): route_sandbox — create_payment_intent runs in orchestra-payments
      [SANDBOX orchestra-payments] create_payment_intent ok (policy rev 1)
      · tool create_payment_intent(hold_ids=['hold_0001']) -> {'intent_id': 'pi_0001', 'amount': 180000, 'currency': 'KRW', 'status': 'created', 'hold_ids': ['hold_0001']}
  [LLM #21] checkout_agent: 호출 capture_payment(intent_id='pi_0001')
      [FILTER] pre_tool idempotency(capture_payment): modify — idempotency_key injected
      [FILTER] pre_tool amount_limit(capture_payment): require_approval — 결제 180,000원 사용자 승인 필요
      [FILTER] pre_tool side_effect_policy(capture_payment): require_approval — commit tool capture_payment needs user approval
      [HITL] 사용자 승인: 예
      [FILTER] pre_tool side_effect_policy(capture_payment): route_sandbox — capture_payment runs in orchestra-payments
      [SANDBOX orchestra-payments] capture_payment ok (policy rev 1)
      · tool capture_payment(intent_id='pi_0001') -> {'intent_id': 'pi_0001', 'status': 'captured', 'receipt_id': 'rcpt_0001', 'amount': 180000, 'currency': 'KRW'}
  [LLM #22] checkout_agent: 호출 confirm_booking(intent_id='pi_0001')
      [FILTER] pre_tool idempotency(confirm_booking): modify — idempotency_key injected
      [FILTER] pre_tool side_effect_policy(confirm_booking): route_sandbox — confirm_booking runs in orchestra-payments
      [SANDBOX orchestra-payments] confirm_booking ok (policy rev 1)
      · tool confirm_booking(intent_id='pi_0001') -> {'intent_id': 'pi_0001', 'status': 'confirmed', 'receipt_id': 'rcpt_0001', 'bookings': ['h1 standard 2026-11-28 1박 2명']}
  [LLM #23] checkout_agent: 결제 180,000원 완료, 영수증 rcpt_0001, 예약 확정
⑬ payment_lead (총괄/팀장)
  [LLM #24] payment_lead: 호출 report_to_parent(status='done', summary='결제 완료 및 예약 확정', artifacts={'receipt_id': 'rcpt_0001'})
      [REPORT payment_lead] done: 결제 완료 및 예약 확정
  [LLM #25] payment_lead: 결제·확정 완료 → 총괄에 보고
⑭ travel_director (총괄/팀장)
  [LLM #26] travel_director: 최종: 비(70%) → 실내 코스(고기국수 → 흑돼지), 제주시내 호텔 1박 hold → 180,000원 승인·결제·예약 확정 완료.

[interrupt #1] 180,000원을 Orchestra Travel (mock)에 결제할까요? (결제 후 환불 가능)  → 응답: 예

최종 답변:
  최종: 비(70%) → 실내 코스(고기국수 → 흑돼지), 제주시내 호텔 1박 hold → 180,000원 승인·결제·예약 확정 완료.

슈퍼바이저(총괄/팀장) LLM 호출 13회, 전체 LLM 호출 26회 (조직 깊이 2)

--- expect 검증 ---
PASS
```

Full flow: `travel_director -> planning_lead -> (weather_agent -> restaurant_agent) -> booking_lead -> hotel_agent -> payment_lead -> checkout_agent`.

| Scenario | What it verifies |
|---|---|
| `jeju_trip_payment` | Full hierarchy + in-team handoff + HITL approval + sandboxed payment + booking confirmation |
| `jeju_payment_rejected` | User declines -> no capture, no confirmation |
| `payment_over_limit` | 2,400,000 KRW suite -> blocked by `amount_limit`, no approval request, alternatives offered |
| `pii_bulk_blocked` | Blocked at the gate, zero LLM calls |
| `sandbox_egress_denied` | Payment host missing from policy -> sandbox denies egress -> no payment, operator-approval notice |

## Division of work and what remains

| Area | Status |
|---|---|
| Specs/org chart/fan-out, filter chain + built-ins, tool wrapper, HITL, ScriptedChatModel | ✅ done |
| Hierarchical graph/tools, service, travel + payment packs, simulation + 5 scenarios | ✅ done |
| NemoClaw policy generation/validation/drift/fail-closed, fake sandbox | ✅ done (real NemoClaw integration ⬜) |
| Packaging (pyproject), legacy shims and de-duplication | ⬜ C1–C2 |
| Vendor 14 plugins, catalog, tool mapping, `claude_dev` pack | ⬜ C3–C7 |
| 5 module-lib adapters, hookify rule filter, 4 more scenarios, `concert_demo` | ⬜ C8–C10 |
| **All 24 LangSmith features** + `showcase` CLI | ⬜ C11 |
| **NemoClaw execution layer** (runner, worker, inference, audit, code sandbox) | ⬜ C12 -> below |
| v1 backend (`langchain.agents.create_agent`), playground tab | ⬜ C14–C15 |

## NemoClaw supplement plan

Today the repo covers **generating and validating policy from declarations**; the layer that actually executes inside a sandbox does not exist yet. Full plan: [`docs/NEMOCLAW_ROADMAP.md`](docs/NEMOCLAW_ROADMAP.md).

| Step | Content | Status |
|---|---|---|
| N0 | ToolSpec -> preset generation/validation/drift, fake runner, security-mode <-> preset mapping | ✅ |
| N1 | Real runner: `nemoclaw`/`openshell` CLI wrapper (allowlist, dry-run), `doctor` | ⬜ |
| N2 | In-sandbox worker + transport, **dual filter checks**, distributed-tracing headers | ⬜ |
| N3 | Startup check that applied presets match declarations (**fail startup on mismatch**), measure real LangSmith path rules | ⬜ |
| N4 | Inference via `inference.local`, privacy router (PII/PCI -> local model) | ⬜ |
| N5 | Egress deny/approval events -> LangSmith feedback / annotation queue | ⬜ |
| N6 | Code-execution sandbox (`orchestra-code`), Landlock/process layers, optional Deep Agents Code | ⬜ |
| N7 | `Dockerfile.worker`, operations runbook | ⬜ |

**Known limits**: (1) `binaries` is a `python3*` glob, so isolation is per **sandbox**, not per executable — keep only the worker in the payment sandbox; (2) `openshell policy set` replaces the whole policy, so use `policy-add` in operations; (3) filesystem/process layers require **recreating** the sandbox; (4) approval/payment ledgers are in-process memory; (5) CLI and policy schemas vary by NemoClaw version — **verify on the installed version first** (`[VERIFY]`).

## Roadmap — aiming for top-tier security (NemoClaw hardening + Claude Security)

Every handoff, tool result, plugin, and memory in a multi-agent system is a trust boundary. So the design is **not "write better prompts" but a Zero-Trust Agent Mesh**. Details: [`docs/SECURITY_ROADMAP.md`](docs/SECURITY_ROADMAP.md).

- **Principles**: the LLM is never the policy decider (deterministic filters are) · inter-agent messages are data, not instructions · privileges only shrink on delegation · approvals are signed and bound to `(tool, args hash, amount, expiry)` · fail closed · every block/denial is observable in LangSmith.
- **NemoClaw hardening (NS1–NS9)**: continuous policy-drift checks, risk-tiered sandboxes (read / payments / code), in-sandbox dual filters, `inference.local` + privacy routing, egress denial -> automatic isolation and review, `policy-add`-only change approval, secret hygiene, resource caps, pinned images + SBOM.
- **Claude Security integration (S1–S8)**: (1) CI scan on every PR (block High+, post patch suggestions) (2) an **ingestion gate** for the 14 plugins (SHA pin + hash + scan before registration) (3) a `security_lead` team (scan / policy / red-team agents that **propose only; humans approve**) (4) a red-team regression suite (injection, handoff poisoning, amount tampering, PII exfiltration) (5) a hash-chained audit log.
- **Status**: design stage. The real Claude Security and NemoClaw interfaces are unverified (`[VERIFY]`), so the core attaches through **interface-neutral adapters**.

---

# Appendix — Original skeleton (supervisor / handoff / hybrid / hierarchical, LangSmith)

The earlier skeleton (`travel_planner/`) is kept as is (its 14 original tests still pass); a backward-compatible shim onto `orchestra/` is Codex task C2.
The detailed legacy write-up (structure diagrams, LangSmith wrapper, security modes, bottleneck report, playground) is in the Korean README: [`README.ko.md`](README.ko.md#부록--기존-골격-supervisor--handoff--hybrid--hierarchical-langsmith-연동).

Quick summary of the legacy structures:

| Structure | Idea |
|---|---|
| A: supervisor | `supervisor <-> {weather, restaurant, hotel}`; rain -> indoor restaurants -> hotel near the route -> summary; the hotel agent has no booking tool |
| B: handoff | `weather <-> restaurant <-> hotel` pass control via `transfer_to_*` tools; `MemorySaver` keeps multi-turn state |
| A+B: hybrid | `planner(A) -> approval(interrupt) -> hotel_booking`; reject/change returns to `planner`; `draft_plan` is passed as state |
| Hierarchical | Team-lead supervisors under a director, keeping each manager to 3–5 options (now superseded by `orchestra/`) |

LangSmith wrapper modes (`TRAVEL_SECURITY_MODE`): `off` / `masked` / `closed` — in `closed`, tracing is disabled and, under NemoClaw, the LangSmith egress preset is not generated at all (physical block).
