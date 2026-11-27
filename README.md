# Multi-Agent Orchestra — Hierarchical Supervisor × LangSmith × NemoClaw

[한국어](README.ko.md) · **English**

**Director → team lead → worker agents · approval-gated mock payments · NemoClaw sandboxes**

[NemoClaw common problems](docs/NEMOCLAW_KNOWN_ISSUES.md) · [Design](docs/DESIGN.md) · [Implementation notes](docs/IMPLEMENTATION.md)

## NemoClaw sandbox — 12 / 12 isolation checks

Real install · WSL2 · NemoClaw v0.0.145 · OpenShell 0.0.116 · 2026-11-27 · [check script](https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw/blob/21-nemoclaw-cli/scripts/nemoclaw_isolation_check.py)

| # | Tried inside the sandbox | Stopped by | Result |
|---|---|---|---|
| 1 | Direct TCP to the internet | network namespace | timeout |
| 2 | HTTPS tunnel to an unlisted host | proxy policy | `403` |
| 3 | Plain HTTP to an unlisted host | proxy policy | `403` |
| 4 | Write `/tmp` (control, must work) | allowed | written |
| 5 | Write `/var/tmp` | Landlock | `EACCES` |
| 6 | Write `/dev/shm` | Landlock | `EACCES` |
| 7 | Change the worker code | file permissions | `EACCES` |
| 8 | Read a file outside the policy | Landlock | `EACCES` |
| 9 | Run as root | sandbox user | uid `10001` |
| 10 | Hold Linux capabilities | none granted | `0` |
| 11 | Gain rights through setuid | `no_new_privs` | on |
| 12 | Run code from memory (`memfd_create`) | seccomp | `EPERM` |
| ⚠️ | Call a model via `inference.local` | **only an empty route**: setting one opens it | `503` |

**Reading the table**

- **Network (1–3):** neither a direct connection nor a proxy request to an unlisted host gets out. The script counts these only if the host machine can reach the same targets.
- **Files (4–8):** `/tmp` is writable (the control), while the other writes and the read are refused. In 5, 6 and 8, ordinary file permissions would allow the access, so the sandbox policy is what stops it.
- **Privileges (9–11):** the worker runs without root or effective capabilities and cannot gain rights through setuid. These checks read the process state.
- **System calls (12):** `memfd_create` is blocked. This does not stop an interpreter such as Python from running code.
- **⚠️ `inference.local`:** not one of the 12. The proxy opens this tunnel even without a network rule; the `503` only means no model route is configured. Once one is set, sandbox requests can reach that model.

## 1. What is implemented

<p align="center">
  <img src="docs/diagrams/implemented-flow-dark.png" width="780"
       alt="Request, pre_request, pre_model; director, team lead, worker; pre_tool, one-shot approval for COMMIT, sandbox runner; post_tool, pre_response, response and trace">
</p>

## 2. Key design decisions (payments are mock)

| Remember this | How | Checked by |
|---|---|---|
| **The model never sets the charge** | The server totals the holds | [tool signature](domains/payment/tools.py) |
| **No approval, no charge** | Ledger check in the tool wrapper, even with approval filters off | [test](tests/test_simulation_golden.py) |
| **An approval works once** | Bound to arguments, amount, user and expiry | [test](tests/test_approval_binding.py) |
| **Resume never charges twice** | The tool node re-runs; capture happens once | [test](tests/test_simulation_golden.py) |
| **Network rules come from code** | Generated from tool declarations; wildcards and duplicate hosts fail | [test](tests/test_policy.py), CI |
| **Isolation is measured** | A probe attempts each action on a real install | [script](https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw/blob/21-nemoclaw-cli/scripts/nemoclaw_isolation_check.py) |

## 3. Division of work and what remains

| Area | Status |
|---|---|
| Hierarchy, 5-stage filters, approval, mock payment, 6 scenarios | ✅ offline |
| LangSmith, 24 features | ✅ offline · ⬜ real server |
| NemoClaw egress policy generation | ✅ offline |
| NemoClaw worker sandbox ([branch](https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw/tree/21-nemoclaw-cli)) | ✅ real install |
| Real LLM | ⬜ |
| Sandbox CPU / memory limits, load, restart (on the 21 branch) | ✅ offline · ⬜ real install |
| Cross-sandbox and DNS-leak tests | ⬜ |
| Claude tool adapters, more scenarios | ⬜ |

## Implementation details

<details>
<summary><b>Contracts, backends, LangSmith, catalog and repository layout</b></summary>

Message contracts require a source label (`user/tool/agent/system`) and bounded
fields/nesting. The host maps explicit SDK roles to source labels; external
TaskBrief/TeamReport DTOs cannot omit the label. Pre-model and handoff validation
fail closed. Agent/tool text is JSON-framed as data with escaped delimiters;
this is defense in depth and cannot guarantee semantic injection resistance.
The `handoff_oversized` scenario verifies rejection on both backends.


Agent construction supports `prebuilt` (the compatibility default) and `v1`.
Install `pip install -e ".[agent-v1]"` and set `ORCHESTRA_AGENT_BACKEND=v1` to use
`langchain.agents.create_agent` for both workers and team supervisors. The builder
also accepts `backend="v1"` explicitly. Development installs include both backends
so parity tests run rather than skip. Missing v1 dependencies produce a clear error.

```powershell
$env:ORCHESTRA_AGENT_BACKEND = 'v1'
python -I examples/simulate_hierarchical.py --mode graph --sandbox fake --security-mode closed
```

Both backends share tool wrappers, approval ledgers, model filters and checkpoint
handling. V1 supports synchronous/asynchronous team handoffs without deprecated
agent factories. Compatibility `prebuilt` and legacy non-hierarchical builders
still emit their existing deprecation warnings. Real provider models remain unverified.

LangSmith observation is available under `orchestra.observability`; the original
`travel_planner.observability` and `monitoring` imports remain compatibility shims.
Run `python -m orchestra.observability.showcase all --offline` to generate a local
24-feature manifest and session report. Real SDK traces terminate at a loopback
receiver; remote mutations are explicitly recorded offline. The payment safety
gate fails CI on an executed COMMIT without matching approval evidence.
See [LangSmith setup and verified limits](docs/langsmith-ui-setup.md) for service
hook integration, Prompt Hub fallbacks, UI procedures and unsupported capabilities.

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

Inspect the read-only plugin catalog without LLMs or external services:

```bash
python -m orchestra.catalog list
python -m orchestra.catalog show pr-review-toolkit/code-reviewer
python -m orchestra.catalog validate
```

The pinned snapshot contains 14 plugins, 16 original agents, 12 skills and 12
commands. Plugin directory names define stable namespaces even when the manifest
uses a renamed alias. `CLAUDE_PLUGINS_DIR` or `--root` selects the input directory.
The original `silent-failure-hunter.md` has invalid YAML; validation reports its
legacy-parser fallback as a warning. Hooks and declared tool names are metadata.
No plugin commands, hooks, file writes or shell tools are executed by this catalog.

`SkillLoader.index()` exposes only skill names and descriptions. `make_skill_tools`
provides READ-only `load_skill(name)` and `load_skill_reference(name, file)` tools
for registered files. Responses pass through PII/PCI masking and are capped at
`ORCHESTRA_SKILL_MAX_CHARS` (default and hard ceiling: 20000); long responses
include a contents summary and prefix. References cannot select arbitrary files.

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
scenarios/*.yaml              6 scenarios shared by simulation and tests
nemoclaw/presets/*.yaml       egress policies generated from ToolSpecs (CI drift check)
```

**Hierarchy bug fixed:** `langgraph_supervisor` adds a static `member->supervisor` edge per member, which woke the team lead mid-handoff; fixed with `_detach_to_supervisor`.

</details>

<details>
<summary><b>Sample run and scenarios</b></summary>

**Sample run (`python -I examples/simulate_hierarchical.py`, payment segment)**

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
| `handoff_oversized` | Oversized delegation contract is blocked before handoff |

</details>

<details>
<summary><b>Playground and LangGraph Studio</b></summary>

Run `python examples/playground.py` and open `http://127.0.0.1:8765`.
The legacy demo remains the first tab. The three diagnostic tabs can run closed/Fake
scripted scenarios or import a C13 `--trace --json` file (up to 2 MB). CLI and viewer
share `export_simulation`; imports are validated, minimized and masked again, and
rendered as text. Approval history replays fixture decisions; it does not approve
live payments. Live HITL controls, Org/Catalog tabs and hosted LangSmith views from
the broader §9.4 design are not implemented by this C14 scope.

`langgraph.json` declares `orchestra.service:studio_graph`. The symbol is importable
and builds a scripted/Fake graph locally. `langgraph-cli` is absent here, so running
`langgraph dev` and viewing Studio remain unverified.

</details>

## Appendix — Original skeleton (supervisor / handoff / hybrid / hierarchical, LangSmith)

Quick summary of the legacy structures:

| Structure | Idea |
|---|---|
| A: supervisor | `supervisor <-> {weather, restaurant, hotel}`; rain -> indoor restaurants -> hotel near the route -> summary; the hotel agent has no booking tool |
| B: handoff | `weather <-> restaurant <-> hotel` pass control via `transfer_to_*` tools; `MemorySaver` keeps multi-turn state |
| A+B: hybrid | `planner(A) -> approval(interrupt) -> hotel_booking`; reject/change returns to `planner`; `draft_plan` is passed as state |
| Hierarchical | Team-lead supervisors under a director, keeping each manager to 3–5 options (now superseded by `orchestra/`) |

LangSmith wrapper modes (`TRAVEL_SECURITY_MODE`): `off` / `masked` / `closed` — in `closed`, tracing is disabled and, under NemoClaw, the LangSmith egress preset is not generated at all (physical block).
