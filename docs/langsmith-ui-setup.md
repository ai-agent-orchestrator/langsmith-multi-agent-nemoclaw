# LangSmith observation and UI setup

The installed SDK verified for C11 is **langsmith 0.16.0**, with LangGraph
1.2.24 and langchain-core 1.8.4. Hosted LangSmith, paid models, Studio's browser
UI, and a real NemoClaw/OTel exporter were not exercised here.

## Local verification

```sh
pip install -e ".[dev,langsmith]"
python -m orchestra.observability.showcase all --offline
pytest -q tests/test_langsmith_offline.py
```

Artifacts are in `out/showcase/manifest.json` and `report.html`. The graph,
approval/resume, filters, fake sandbox and SDK trace HTTP requests are real.
Requests terminate at an ephemeral loopback receiver. Other SDK mutations are
recorded by an explicitly named `OfflineClient`; a recording is not evidence that
a hosted endpoint accepted a request. Tests also forbid non-loopback sockets.
Evaluation calls the real SDK with `upload_results=False` and evaluator tracing
disabled; its client is explicitly bound to loopback even though no upload is requested.

The manifest lists all 24 design rows, their commands, and where to inspect them.
Inspect each result's status: missing judge models and unavailable APIs are not
reported as successful remote features. `--security-mode closed` sends zero HTTP
requests for tracing. It still permits the local deterministic evaluation work.

## Application connection

Create `Observation(org=app.org.name, domains=app.org.domains, mode="masked")`
and call `observation.service(app)`. This binds the existing
`OrchestraService(config_hook=..., scope=...)` hooks, preserving recursion limits,
thread IDs, approval ledgers and sandbox execution semantics. Enable with
`LANGSMITH_TRACING=true`; configure `LANGSMITH_PROJECT`, `LANGSMITH_API_KEY`, and
`LANGSMITH_ENDPOINT` through the environment. No endpoint or model ID is embedded
in the new observation layer. `ORCHESTRA_SECURITY_MODE` takes precedence over
`TRAVEL_SECURITY_MODE`. Legacy `travel_planner` imports retain their old mode and
masking contracts through re-export shims.

`closed` constructs no SDK client. Explicit callbacks and distributed headers
are suppressed by the observation adapter. `masked` sanitizes run inputs/outputs,
metadata, feedback, datasets and warning messages. `self_hosted` uses the same
masking with an environment-specified endpoint. `off` permits raw facade payloads;
custom spans and local artifacts still apply masking. Sharing requires an explicit
run ID and mode `off`; masked, self_hosted and closed never call `share_run`.

SDK failures pass through `safe_call`, which logs only the exception class.
Application/filter failures are not retried by the observation adapter. The
payment evaluator checks ordered, matching, one-use approval evidence. A zero
score raises outside `safe_call`; pytest and the showcase exit unsuccessfully.

## Feature map and UI checks

Use the project selected by `LANGSMITH_PROJECT` (default `orchestra-dev`;
examples also use `orchestra-sim` and `orchestra-eval`). Hosted calls below require
explicit credentials and permissions. Each screenshot placeholder requires a
future real-account verification; no screenshot is presented as already verified.

| # | Command / API | Where and what to verify |
|---|---|---|
| 1 | `trace --offline`, `Observation` | Project → Runs: director/team/worker graph tree. [Screenshot pending] |
| 2 | `trace --deep --offline`, `span` | Run tree: `filter:*`, `sandbox:*` children and decision/result metadata. C8 reducers can use the same span helper. |
| 3 | `mask --offline` | Inspect wire artifact: REDACTED markers, no raw PII/PCI. Repeat with `--security-mode closed`: no requests. |
| 4 | `threads --offline` | Project → Threads: `thread_id == session_id` for ask and resume. |
| 5 | `distributed --offline`, `trace_headers` / `parent_scope` | Child `parent_run_id` points to host run; closed exports no headers. Actual NemoClaw transport is unverified. |
| 6 | `query --tag org:travel_payment --offline`, `fetch_runs(filter=...)` | Runs → Filters: tags, metadata, errors, latency expressions. |
| 7 | `feedback --offline`, `feedback` | Run → Feedback: payment_safety and filter scores. |
| 8 | `feedback --user --offline`, `user_feedback_token` | Explicit run's presigned `user_score` token. Do not publish tokens in logs. |
| 9 | `dataset push --offline`, `scenario_examples` / `push_dataset(tag=...)` | Datasets → orchestra-scenarios: inputs, expected flags/trajectory, metadata and version tag. |
| 10 | `dataset from-runs --offline`, `from_runs` | Filter low feedback before upload to orchestra-regressions; retain source_run_id. |
| 11 | `eval --offline`, `sdk_evaluate` | Local ExperimentResults; hosted uploads require offline=False explicitly. |
| 12 | `eval --evaluators all --offline` | payment_safety, trajectory_match, tool_args_valid, security_block, pii_leak, latency_budget, pass_rate. `llm_judge` requires an injected callable and uses the original grader prompt. |
| 13 | `pairwise --a hierarchical --b supervisor --offline` | Without a comparator model the command reports unavailable. `compare_targets` executes supplied targets; `sdk_compare` compares two hosted experiments using `langsmith.evaluation.evaluate_comparative`. Injected comparator uses the original comparator prompt. |
| 14 | `pytest -m langsmith --langsmith-output` | Opt-in test in tests/eval; `langsmith.testing.log_*` calls are guarded. Install the SDK's pytest extra for its rich display. Default tests exclude the marker. |
| 15 | `prompts push/pull/diff --offline`, `PromptHub` | Prompts → private orchestra-* prompt and commit URL; `push_agents` handles AgentSpec/catalog declarations. See fallback injection below. |
| 16 | `annotate --offline`, `create_queue` / `enqueue_low_scores` | Annotation queues → orchestra-review. Rubric checks approval evidence and helpfulness. |
| 17 | `online setup --offline` | SDK 0.16.0 has no create_run_rule. In UI, Project → Rules → create online evaluation, select hierarchical-tag filter, choose sampling and scoring prompt. [Screenshot pending] |
| 18 | `monitor --demo --offline` | Project → Monitoring: P50/P95/P99, errors, tokens; create alerts with a project-appropriate threshold and notification destination in UI. [Screenshot pending] |
| 19 | `monitor --cost --offline`, `estimate_cost` | Set ORCHESTRA_PRICE_TABLE JSON prices per million input/output tokens. Missing price is unknown, never zero-cost. |
| 20 | `share RUN_ID` | Explicit opt-in only, mode off. Default and closed/masked paths cannot share. |
| 21 | `otel` | See OpenTelemetry section. Exporter availability is not inferred from a configured env flag. |
| 22 | `studio` | Install LangGraph CLI separately; run langgraph dev from repository root. langgraph.json loads the scripted/fake studio_graph factory. [Screenshot pending] |
| 23 | `insights` | Hosted Workspace → available Insights/Engine/Context Hub UI. Inspect recurrent blocked requests, long prompts, retrieval context. Labels/availability depend on account rollout; no code depends on them. [Screenshot pending] |
| 24 | `report --offline` | report.html shows actual captured node tokens, latency, expensive prompt inputs and parent IDs. `--demo` uses clearly synthetic sample runs. |

## Prompt fallback injection

Construct `hub = PromptHub(client)` and provide an explicit local mapping to
`services["prompt_resolver"] = hub.resolver({"orchestra-name": local_prompt})`.
Agent/team declarations may then use `hub:orchestra-name`. A failed pull returns
the local fallback; successful pulls are cached for the hub object's lifetime.
No fallback mapping means a build error rather than silently using a missing prompt.
Prompt Hub commits returned by push can be stored in Observation metadata for provenance.
Private static SystemMessage templates preserve literal braces without a Jinja runtime.

## Verified API differences

- `Client.create_presigned_feedback_token(run_id, feedback_key, ...)` is available.
- Comparative evaluation lives in `langsmith.evaluation`, not the top-level module.
- `evaluate` accepts `upload_results` as an alias for the new `upload_runs` parameter.
- Pytest uses its marker/plugin and `testing.log_*`; `testing.unit` is absent.
- Distribution uses RunTree.to_headers/from_headers. In this installed version,
  `trace.create_child` inherits the reconstructed parent's client; the adapter
  explicitly binds that parent to avoid an implicit default hosted client.
- Online rule creation is unavailable in the installed client and degrades to UI.

## OpenTelemetry

`otel.configuration()` reports configuration only and starts no exporter.
The installed SDK has OTLP integration code under `_internal/otel`, but the
`LANGSMITH_OTEL_ENABLED` flag is not a verified switch in this release. Inspect the
installed Client's `tracing_mode` option and install compatible OTel extras before
enabling export. Supply OTEL_EXPORTER_OTLP_ENDPOINT or the full traces endpoint
through env; never place credentials in the repository. Validate masked payloads
against a local OTel collector before enabling a real exporter. Do not start an
OTel exporter in closed mode. Real sandbox OTLP correlation remains unverified.

## Known limits

No persistence is added to approval/payment ledgers. Offline records do not prove
hosted authentication, API permissions, UI rollout, OTel transport or model quality.
The scripted showcase currently accepts travel_payment/hierarchical; application
adapters accept explicitly constructed apps and injected comparison targets.
