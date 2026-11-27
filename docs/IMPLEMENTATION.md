# Implementation notes

Back to the [README](../README.md).

## NemoClaw in this repo

This repo covers the part of NemoClaw you can **see and run offline**: `ToolSpec` egress declarations -> preset generation, validation and drift check (`scripts/gen_nemoclaw_presets.py`), the security-mode <-> preset mapping (`closed` removes the LangSmith egress preset entirely), and a fake sandbox that makes the `sandbox_egress_denied` scenario show the multi-agent -> LangSmith -> NemoClaw flow end to end.

**Not covered on this branch:** executing inside a real sandbox. The worker sandbox work is on branch [`21-nemoclaw-cli`](https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw/tree/21-nemoclaw-cli); see the isolation checks in the [README](../README.md). CLI and policy schemas vary by NemoClaw version.

---

## Appendix — Original skeleton (supervisor / handoff / hybrid / hierarchical, LangSmith)

The earlier skeleton (`travel_planner/`) is kept as is (its 14 original tests still pass); a backward-compatible shim onto `orchestra/` is Codex task C2.
The detailed legacy write-up (structure diagrams, LangSmith wrapper, security modes, bottleneck report, playground) is in the Korean README: [`README.ko.md`](IMPLEMENTATION.ko.md#부록--기존-골격-supervisor--handoff--hybrid--hierarchical-langsmith-연동).

Approval records bind the full canonical arguments, caller identity, server-derived
amount, expiry and request/thread ID with a full SHA-256 key. Set
`ORCHESTRA_APPROVAL_TTL_SECONDS` (default 300); supply the authenticated caller as
`user_id=` to both OrchestraService.ask and resume. Legacy calls default to
`anonymous`; this is compatibility, not authentication. Checkpointed approval
payloads preserve the original deadline across interrupt re-execution. The ledger
atomically claims the grant before execution. Signature fields are reserved only;
no signing, persistence, or hash-chain audit was added.

### Delegation capabilities (C18)

`TeamSpec.capabilities` accepts optional `tools`, `domains`, and `sandboxes` lists.
An explicit list is an upper bound: every descendant grant must fit inside it.
Omitted dimensions derive from the subtree's actual grants; they are not wildcards.
Empty lists grant nothing. Agent tools remain restricted to `AgentSpec.tools`.
After domain resolution, sandbox ownership comes from registered pack metadata;
foreign-domain sandbox tools and unknown grants fail closed before graph construction.
Generated fanout leads inherit the same ancestor bounds. Host-defined agents without
pack metadata have no domain or sandbox authority. These checks validate graph
configuration; OS sandbox startup/policy attestation remains the separate runner work.

### Inspection normalization and tool provenance (C19)

The request gate checks a separate inspection copy: NFKC, decomposition/removal
of combining marks and zero-width formatters, whitespace folding, and 1→i / 0→o.
Business payloads, amounts, dates and original user messages are never rewritten.
The legacy security gate uses the same inspection function.

Successful post-tool data is exposed as `{"source":"tool","data": ...}`; this
host-owned outer label overrides any source claim inside the returned data.
The existing pre-model/handoff paths retain tool roles and reject recognized
instruction patterns even through replayed data frames. Nested tool-labelled
instructions cannot be passed as delegation or COMMIT data. Once a tool result
matches the shared gate patterns, its raw text is replaced with a generic blocked
marker and that thread is conservatively quarantined: delegation and COMMIT are
blocked before approval or execution. Normal model continuation also stops on
the marker. Quarantine is scoped by LangGraph configuration and is not cleared by
user approval; use a new thread after investigation. State is process-memory only.

This is **defense in depth**, not a complete prompt-injection solution or semantic
taint tracking. Unrecognized paraphrases, other encodings, and model reasoning
cannot be proven safe by these rules. A recognized quoted example can also trigger
a conservative block. Source labels attest transport, not truth. Scanner, red-team
framework, audit signatures and real sandbox runner remain separate work.

### Simulation CLI (C13)

```bash
python -I examples/simulate_hierarchical.py --org orgs/travel_payment.yaml --pattern tools --trace --json out/sim/trace.json
```

`--pattern graph` is the default and retains the previous behavior. `tools` is a
thin entry point in the control-tool factory module and calls the same safe graph
builder; it is not another orchestration or model backend. An explicit `--org`
replaces the scenario organization without modifying the scenario file. A missing
path or directory exits with status 2 and one diagnostic line before graph execution.
`--mode print|graph` still controls presentation only; both execute the real graph.

`--trace` collects local filter decisions (including ALLOW), approval requests and
results, sandbox routes/results, and blocks. Each `[TRACE]` line is one JSON record
with a monotonically ordered event `seq`. Newlines inside fields are escaped.
`--json` additionally includes the same `trace` array, using existing event order
and deduplication. Trace console text and JSON exports always pass through masking,
even with `--security-mode off`. Trace records whitelist diagnostic fields and omit
approval bindings/full tool args. This option does not enable LangSmith transmission.
Without it, the existing narrative/JSON shape and event capture behavior are retained.

### Ledger persistence (C22)

The default `LedgerStore` is isolated process memory and needs no key or file.
For SQLite, set `ORCHESTRA_LEDGER=sqlite:///C:/private/orchestra/ledger.db`
with an explicit path in an existing directory, and set
`ORCHESTRA_LEDGER_HMAC_KEY` to a random secret of at least 32 UTF-8 bytes in the
environment. Missing/short keys reject startup before opening the database.
`ORCHESTRA_LEDGER_KEY_ID` is an optional public rotation label; when omitted,
a non-secret HMAC-derived identifier is used. No default database path or secret
is provided. Schema version 1 is recorded in SQLite and approval rows; unknown
future versions fail closed. Tests create databases only under pytest `tmp_path`.

The original C17 `ApprovalRecord` and canonical arguments stay in memory. SQLite
stores a domain-separated, full-length HMAC-SHA256 binding over the entire original
C17 tuple, a separate HMAC of the user, a key ID, expiry, consumption flag and
masked display. Display omits argument/user/request contents and may show amount.
Execution recomputes the HMAC from a valid transient record, including the original
expiry and request ID; a new deadline never revives an old authorization. Expired
records are invalid on read and `store.cleanup_expired(now)` deletes them.

`ApprovalLedger`, `EventLog` and the local `MockPaymentProvider` share one store.
`BEGIN IMMEDIATE` plus a conditional consume UPDATE prevents two connections from
claiming the same grant. Consumption, mock payment state, and the keyed idempotency
entry commit together. Failed mock-state writes roll back all three. Original
arguments, unkeyed approval hashes, and raw binding payloads are omitted from
persistent events. Payment safety evaluation correlates host-generated HMAC
references after this redaction. A stable `EventLog(store=store, scope="app")`
reloads masked events and deduplicates across instances; use separate scopes for
independent runs. Default runtime scope is `runtime`.

Key rotation: stop old writers, let outstanding approvals expire (or deny them),
then deploy a new environment key/key ID together. New-key instances cannot consume
or overwrite old-key approvals; retain their masked metadata as read-only until
expiry, then clean it up. Old idempotency indexes are key-specific: do not blindly
retry old requests across key epochs. Inspect recorded receipts and reconcile
pending requests before enabling new work; no automatic re-keying or recovery of
raw inputs is implemented. Keep old secrets out of the database and version control.

One masked `append_event` hook is available for future signature/chain integration;
its default is a no-op. No signature or hash chain is implemented. These guarantees
cover the local mock/Fake path. SQLite COMMIT rejects nonparticipating payment
providers and remote sandbox transports. Graph checkpoints, holds, model queues,
and actual payment-provider effects are not made durable or transactionally coupled
by this feature; full workflow restart/resume and real NemoClaw remain unverified.
`Memory` mode retains C17 consumption on execution failure; only participating SQLite mock operations roll back authorization together with payment state.
