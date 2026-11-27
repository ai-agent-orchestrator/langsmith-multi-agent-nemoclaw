# NemoClaw Known Issues — Symptoms · Causes · Workarounds · Interview Notes

**English** | [한국어](NEMOCLAW_KNOWN_ISSUES.ko.md)

> The scripts and code referenced here (`scripts/nemoclaw_*.sh`, `orchestra/sandbox/nemoclaw/cli.py`, `tests/fixtures/nemoclaw/`) are in this repository.

> Baseline: **nemoclaw v0.0.145 / openshell 0.0.116**, WSL2 Ubuntu 26.04 + Docker Desktop, sandbox `my-assistant` (OpenClaw, ollama `qwen3:1.7b`, GPU).
> Evidence comes from three places: ① measurements on 2026-11-27 (`scripts/nemoclaw_probe.sh`, `nemoclaw_hang_diag.sh`), ② the NemoClaw v0.0.145 source (`NVIDIA/NemoClaw` tag `v0.0.145`, commit `6f36fef`), ③ public issues and official docs.
> Tags: **[measured]** reproduced in this environment · **[source]** confirmed in v0.0.145 code · **[issue]/[docs]** public material · **[guess]** not yet confirmed.

---

## 0. At a glance

| # | Symptom | Cause | Do this now | How our code responds |
|---|---|---|---|---|
| 1 | Sandbox commands (`exec`, `policy list`, `status`, `doctor`) **hang with no output for tens of seconds up to 30 minutes** | Waiting on the per-sandbox lock (`mcp-lifecycle-locks`). Default wait is 30 minutes, silent. If the owner is in a different PID namespace it is never reclaimed **[measured & confirmed][source][issue #14070]** | `scripts/nemoclaw_lock_doctor.sh`, then `--clean` if needed | The execution path does not depend on nemoclaw; timeouts fail closed |
| 2 | `Failed to acquire lock on ~/.nemoclaw-portable-host.lock after 120 retries` | Another nemoclaw process (usually a hung one) holds the host lock. Gives up after 0.1 s × 120 ≈ 12 s **[measured][source]** | Find the lock owner PID and stop only that process | Classified as `nemoclaw_host_lock_busy`; our calls run one at a time |
| 3 | The lock stays held after a timeout | Only the parent dies; a child or orphan keeps the lock **[issue #10663]** | Find leftover `node …/nemoclaw` with `ps` and stop it | On timeout we kill the **whole process group** |
| 4 | Running the same command twice at once makes both fail | Every command is serialized through the host lock **[measured]** | Don't run them concurrently | Probe script refuses to run twice; single-flight lock in the CLI wrapper |
| 5 | Text `nemoclaw <sb> status` is slow, `status --json` is fast | Text status sends a **real inference request** from inside the sandbox (90 s per attempt, up to 3 attempts) **[source][docs]** | Use `status --json` for checks | Runtime checks use only `status_json` |
| 6 | Nothing works, `failureLayer: docker_unreachable` | Docker Desktop is off → gateway is down **[measured]** | Start Docker Desktop → `nemoclaw <sb> recover` | Startup refuses to run if doctor fails |
| 7 | With a `connect` session open, every other sandbox command hangs | Reported as a regression in v0.0.111; fix status in the current version unknown **[issue #9737]** | Close `connect`/`launch` in other terminals | Lock doctor lists open sessions |
| 8 | After changing part of the policy, other presets **silently disappear** | `openshell policy set` **replaces** the whole policy, it does not merge **[docs][source]** | Change policy only with `nemoclaw <sb> policy add` | `policy set` is not in the command allowlist (pinned by a test) |
| 9 | A channel was removed but its network allowance remains | Preset-retention bug **[issue #9109]** (reported fixed in later releases) | Check `policy list` regularly | Policy drift check |
| 10 | The sandbox can reach more of the internet than expected | Some default presets use `access: full` + `tls: skip` (no path rules) **[measured]** | `policy remove` presets you don't use | Our preset generator emits path-scoped rules only |
| 11 | Filesystem isolation is weaker than expected | `landlock: compatibility: best_effort` — **silently degrades** if the kernel can't support it **[measured][docs]** | Check kernel 5.13+ | Needs a second line of defense (in-sandbox filters, N3) |
| 12 | The CLI talks to NVIDIA | Operation telemetry is **on by default** since v0.0.141 **[docs]** | `export NEMOCLAW_DISABLE_TELEMETRY=1` | Wrapper turns it off by default (on only with `ORCHESTRA_NEMOCLAW_TELEMETRY=1`) |
| 13 | onboard hangs on WSL2 | Slow Windows-side Ollama check (PowerShell) **[issue #9604, fixed]**, sudo+pty timeout **[issues #10663, #12281 regression]** | Latest version; use Ollama inside WSL instead of on Windows | — |
| 14 | Image build hangs / exit 137 | Out of memory (image ~2.4 GB compressed), under-provisioned runtime **[docs]** | Raise Docker Desktop resources, add swap | — |
| 15 | `CONNECT tunnel failed, response 403` inside the sandbox | Host not in policy → proxy refused it. This is expected behavior **[docs]** | Check the denied flow with `nemoclaw <sb> logs`, then review presets | Repeated egress denials isolate the sandbox (NS5) |

---

## 1. Why these problems happen — architecture

```text
nemoclaw (Node CLI, host)          ← convenience layer: onboard, presets, health checks, locks
   │  host lock     ~/.nemoclaw-portable-host.lock
   │  sandbox lock  ~/.nemoclaw/state/mcp-lifecycle-locks/<sha256(name)>.lock
   ▼
openshell (CLI) ──► openshell-gateway (127.0.0.1:8080)   ← the real isolation engine: enforces policy, runs commands
                         ▼
                k3s inside a Docker container ──► sandbox (OpenClaw, landlock, network proxy)
```

- **The unstable part is the top layer (nemoclaw CLI).** At the same moment, `openshell policy get --base` finished in under a second while nemoclaw sandbox commands hung for more than 45 seconds **[measured]**.
- nemoclaw is designed to "stop safely". If it cannot be sure who owns a lock, it waits instead of taking it. That is right for safety but **hurts availability**, especially on WSL2 where the PID namespace changes on every boot.

## 2. Locks in detail

### 2.1 Host lock `~/.nemoclaw-portable-host.lock`
- It is a directory; the `owner` file inside holds a PID **[source: `src/lib/state/registry/lock.ts`]**.
- Retries are `LOCK_RETRY_MS = 100` and `LOCK_MAX_RETRIES = 120`, so it fails after about 12 seconds. This matches the measured 13.1 s.
- The word "portable" appears even if you never used the Portable profile; the name has been called misleading **[issue #11903]**.
- The same lock problem keeps coming back on different code paths: an interrupted onboard leaves a lock that is never reclaimed **[#10461]**; uninstall contention reports retry exhaustion instead of the cause **[#11132]**; an ownership error was fixed and then regressed in v0.0.132 **[#9746 → #13017]**.

### 2.2 Per-sandbox lock `mcp-lifecycle-locks`
- **Every sandbox-scoped command** except interactive sessions (`connect` and similar) is wrapped in `withSandboxLifecycleLock` by the shared base command class. It takes the host lock (fence) and then this lock as well **[source: `src/lib/cli/nemoclaw-oclif-command.ts` `runAdmittedLifecycleBoundary`, `src/lib/actions/sandbox/lifecycle/lock.ts`]**.
- The only exception is `status`. When the host lock is busy it falls back to an unlocked read and skips the inference check → this is why only `status --json` was fast **[source][measured]**.
- The default wait is `DEFAULT_TIMEOUT_MS = 30 * 60_000`, i.e. **30 minutes**, with no output while waiting **[source: `src/lib/state/mcp-lifecycle-lock-acquisition.ts`]**.
- The lock file records `pid`, `hostIdentity` (machine-id) and `pidNamespaceIdentity` (`/proc/self/ns/pid`). If the owner is **on another host or in another PID namespace, it is treated as "active" and never reclaimed** **[source: `mcp-lifecycle-lock-identity.ts` `classifyMcpLifecycleLock`]**.
  - Intent: never misjudge another machine's or namespace's process and break mutual exclusion on a shared directory.
  - Side effect: after a WSL restart, a lock left by the previous boot is treated as a live owner forever → 30-minute hangs **[issue #14070]**.

### 2.3 Real incident (2026-11-27)
- Symptom: every nemoclaw command against `my-assistant` hung silently for more than 45 seconds — text `status`, `doctor`, `policy list/get`, `exec -- true` and dry-runs alike.
- Diagnosis (`nemoclaw_lock_doctor.sh`): a sandbox lock created at **2026-11-25 04:44 UTC** was still there. Its owner PID had already exited and it was `foreign-namespace` (WSL had rebooted since).
- Fix: `--clean` renamed the lock file → `nemoclaw my-assistant policy list` went from **over 45 s to 1.15 s**.
- Lesson: if nemoclaw sandbox commands hang silently after a WSL or PC restart, run the lock doctor before suspecting anything else.

### 2.4 Diagnose and clean up
```bash
bash -l scripts/nemoclaw_lock_doctor.sh          # read-only diagnosis
bash -l scripts/nemoclaw_lock_doctor.sh --clean  # stop hung CLI processes + rename orphaned foreign-namespace locks
```
- Stopped: `node …/nemoclaw …` CLI processes older than 90 seconds. `connect`/`launch`/`onboard`/`rebuild`/`destroy`/`update`/`uninstall` are never touched.
- Lock files are **renamed, not deleted** (`*.stale-<timestamp>`), so the change can be undone. Locks are left alone while any nemoclaw CLI process is still running.
- The gateway, sandbox containers, docker and ollama are never targeted.

## 3. Common on WSL2

| Symptom | Cause / check | Action |
|---|---|---|
| `docker` fails with `npipe:////./pipe/dockerDesktopLinuxEngine` | Docker Desktop is not running **[measured]** | Start Docker Desktop; enable Ubuntu under WSL integration **[docs]** |
| `docker ps` works inside WSL but nemoclaw hangs | Locks from section 2 | Lock doctor |
| Running a `.sh` from a repo cloned in PowerShell gives `$'\r': command not found` | Windows git checked it out with CRLF **[measured]** | `*.sh text eol=lf` in `.gitattributes` (done) |
| `git clone … ~/x` in PowerShell lands in `C:\WINDOWS\system32\~\x` | PowerShell does not expand `~` in arguments to native commands **[measured]** | Work inside WSL or use full paths |
| `bash -lc '… $! …'` from PowerShell yields an empty PID | Special variables and quotes break between PowerShell and bash **[measured]** | Put long commands in a script file |
| onboard hangs at "Configuring inference provider" | Windows-side Ollama check waited with no timeout **[issue #9604, fixed]** | Latest version |
| Sandbox disappears right after creation on WSL2 + GPU | Early versions forced `--gpu` **[issue #208, early]** | Current version verifies the GPU first (measured: `cudaVerified: true`) |

## 4. What "slow" actually means

- **`nemoclaw <sb> status` (text):** checks gateway and sandbox state, then sends a **real request** to the inference endpoint from inside the sandbox — 90 s per attempt, up to 3 attempts on transient errors **[source: `inference-invocation-probe.ts` `INFERENCE_INVOCATION_REQUEST_TIMEOUT_SECONDS = 90`][docs]**. A local model loading into memory for the first time makes it slow.
- **`nemoclaw <sb> status --json`:** skips the inference check when another operation is running (`inferenceProbeSuppressed: true`) **[measured][docs]**. Measured 2 s.
- **`openshell policy get --base <sb>`:** asks the gateway directly. Measured under 1 s.
- Bottom line: **use `status --json` and `openshell` for runtime checks; use text `status`/`doctor` only when a person is reading.**

## 5. Policy traps (network and filesystem)

1. **`openshell policy set` replaces everything.** Pass a partial document and the other presets silently vanish. Change policy only with `nemoclaw <sb> policy add` (merges, then validates) **[docs]**.
2. **`--base` vs `--full`:** `--base` shows only what people added (preset names are the keys). `--full`/`status` also show provider-composed `_provider_*` entries **[measured]**. Pick one as the drift baseline; we use `--base`.
3. **Broad default presets:** `brew-balanced` grants `access: full` to `github.com`, `ghcr.io` and others, some with `tls: skip` **[measured]**. Skipping TLS means path rules can't be inspected, so it becomes "anything to that host" → a data-exfiltration route.
4. **`binaries` globs are allowed:** `/usr/bin/python3*`, `/sandbox/.uv/python/**/python*` **[measured]**. Binary names alone can't isolate precisely → **the sandbox is the isolation unit**.
5. **Custom presets skip NemoClaw's review.** Names must be lowercase RFC 1123 and must not collide with built-in presets (`pypi`, `slack`, …) **[docs]**.
6. **Non-interactive runs need `--yes`:** without it the confirmation prompt hangs. Preview with `--dry-run` **[measured: `policy add --help`]**.
7. **Channel presets lingering:** a bug left a channel's allowance in place after the channel was removed **[issue #9109]**. Check `policy list` regularly.

## 6. Inference traps

- The agent inside the sandbox calls models only through `inference.local`; OpenShell relays to the real endpoint **[docs][measured: `managed_inference` preset]**.
- A local Ollama bound only to loopback is unreachable from the sandbox. `host.docker.internal` is unreliable on the k3s network **[docs]**. The measured route is `host.openshell.internal:11435` (private IP ranges only).
- Too-short context (OpenClaw wants 16384+) and cold model loads → timeout variables: `NEMOCLAW_AGENT_TIMEOUT` (default 600 s, baked into the image), `NEMOCLAW_LOCAL_INFERENCE_TIMEOUT` (180 s), `NEMOCLAW_SANDBOX_READY_TIMEOUT` (180 s) **[docs]**.

## 7. Resources and platform

- The sandbox image is about 2.4 GB compressed **[docs]**. With 8 GB RAM or less and no swap, the image push can be OOM-killed (exit 137) **[docs]**.
- Below 4 vCPU / 8 GiB for the container runtime, the build can stall with no output **[docs]**.
- Landlock is `best_effort`: if the kernel can't support it, the sandbox **runs degraded with only a warning** **[docs][measured policy]**.

## 8. Telemetry

- Since v0.0.141, installer and CLI operation telemetry is **on by default** and sent to NVIDIA **[docs: 2026-11-03 release notes]**.
- To turn it off: `export NEMOCLAW_DISABLE_TELEMETRY=1` in every shell **[docs: CLI Commands Reference]**.
- For air-gapped or regulated environments this default must be checked. Our wrapper turns it off by default.

## 9. How this repository responds (code locations)

| Problem | Response | Location |
|---|---|---|
| Hangs | Timeout on every call; on expiry **kill the process group**; failures block (fail closed) | `orchestra/sandbox/nemoclaw/cli.py` `_kill_group` |
| Lock contention | Our nemoclaw calls are single-flight via `flock`; lock errors become `nemoclaw_host_lock_busy` | `cli.py` `_single_flight` |
| Slow reads | Policy via `openshell policy get --base`, state via `status --json` | `cli.py` `command()` |
| Output format | Parse the OpenShell banner (`Version/Hash/…` + `---`); unknown banners are rejected | `orchestra/sandbox/nemoclaw/live.py` `split_policy_output` |
| Policy replacement risk | No `policy set`; changes are one approved `policy add --from-file … --yes` | `cli.py`, `orchestra/security/policy_change.py` |
| Telemetry | `NEMOCLAW_DISABLE_TELEMETRY=1` in the child environment | `cli.py` `_child_env` |
| Operational diagnosis | Install probe, hang diagnosis, lock diagnosis and cleanup | `scripts/nemoclaw_probe.sh`, `nemoclaw_hang_diag.sh`, `nemoclaw_lock_doctor.sh` |
| One `exec` per call | Optional persistent worker (`NEMOCLAW_TRANSPORT=ssh_stream`): start it once over OpenShell SSH and exchange one JSON line per request. On failure the session is killed and the request is **not resent** **[measured: probe round trip; restart and timeout are not verified on a real install]** | `persistent.py`, `ssh_launcher.py`, [REALITY §3.1·§3.2](NEMOCLAW_REALITY.md) |
| Custom images | `nemoclaw onboard --from` needs an image containing the NemoClaw runtime → the worker uses `openshell sandbox create --from` **[measured: create, key install, worker probe; network blocking not verified]** | `scripts/nemoclaw_worker_sandbox.py`, [REALITY §3.2](NEMOCLAW_REALITY.md) |
| Empty network policy | `policy get` omits an empty `network_policies` → read as "no rules" only for OpenShell output **[source]** | `orchestra/sandbox/nemoclaw/live.py` `parse_policy` |
| Regression prevention | Tests built on real CLI output fixtures | `tests/fixtures/nemoclaw/`, `tests/test_nemoclaw_cli_hardening.py` |

`nemoclaw <sb> exec` runs inside both locks from section 2 until the remote command ends **[source]**, and the command starts only after piped input has been read to the end (OpenShell 0.0.116) **[source]**, so it cannot host a long-lived worker.

Limits of the persistent worker: requests to one sandbox run one at a time. Whether closing the SSH session on a timeout also stops a tool already running in the sandbox is unverified. The older one-call path (`stdio_via_cli`) can put the working directory first on `sys.path` (the persistent worker blocks this with `-P`).

Still to do: verifying persistent-worker restart and forced exit on a real install, and measuring the custom preset key format (`nemoclaw_custom__…`).

---

## 10. Interview notes

**Q1. Sandbox commands hang with no output. How did you approach it?**
- Split the symptom by layer and measured each: Docker (`docker ps` 0.05 s) → gateway (`openshell policy get` under 1 s) → nemoclaw CLI (over 45 s). That **narrowed the problem to the CLI layer**.
- Observed the hung process: no child processes, Node waiting on its own → an internal wait, not an external program.
- Read the source for that exact version to pin the cause: a lock with a 30-minute silent wait, and a rule that never reclaims owners from another PID namespace. Cross-checked against public issues.
- Prevented recurrence: a diagnosis tool, wrapper timeouts with process-group kill, and a separate execution path.

**Q2. Why doesn't NemoClaw just delete stale locks? Is that the right design?**
- If the owner is in another namespace or on another host, you can't be sure it is dead. Deleting it wrongly lets two operations change policy at once — a much worse failure.
- So it **puts safety (mutual exclusion) ahead of availability**. The flaw is that it hides the cost from the user (30 minutes of silence).
- Improvements: progress output while waiting, show the owner, an explicit operator reclaim command, lease expiry.

**Q3. How do you balance fail-closed with availability?**
- Keep the security decision path (allow/deny) fail-closed: no answer means deny.
- But don't let a deny turn into an outage: short timeouts, a fast alternative read path (`openshell`), precise error codes (`cli_busy`, `nemoclaw_host_lock_busy`), and diagnosis tools for humans.
- "Blocked" and "blocked for an unknown reason" are different. The first is a design choice; the second is a defect.

**Q4. Why allow only `policy add` for policy changes?**
- `policy set` replaces everything, so a partial document can silently drop existing allowances or, conversely, widen them.
- Merge + post-change verification (drift check) + human approval + audit record must be tied to one additive call to stay traceable. On failure we don't retry; we record "outcome unknown" so the same change can't be applied twice.

**Q5. You have a sandbox — why a separate security layer (guard)?**
- The sandbox (the wall) controls *where* things can go. It can't stop bad actions over allowed routes, e.g. paying the wrong amount through an allowed payment API, or sending personal data to an allowed GitHub.
- The guard (the gate) decides *whether* an action should happen: injection, unapproved payments, out-of-scope delegation.
- And in this environment the wall can be lower than it looks — `best_effort` Landlock, `access: full` presets → defense in depth is required.

**Q6. Principles for bringing an alpha external tool into production?**
- Pin the version, verify syntax by measurement, turn real outputs into fixtures for regression tests.
- Hide it behind an interface so it can be replaced (`SandboxRunner`), and bind the execution path to the most stable lower layer (`openshell`).
- Review the defaults: telemetry on by default, broad default presets.
- Document known issues and link them to upstream issues (this document).

**Q7. A mistake you made while debugging?**
- Ran the probe twice at once and created the lock contention myself → added a single-instance lock.
- Hypothesized "Docker is slow because it's the Windows binary", then rejected it by measurement (0.047 s) → **confirm a hypothesis by measuring before fixing**.

---

## Sources

- NemoClaw v0.0.145 source: `src/lib/state/registry/lock.ts`, `src/lib/state/mcp-lifecycle-lock-acquisition.ts`, `src/lib/state/mcp-lifecycle-lock-identity.ts`, `src/lib/actions/sandbox/lifecycle/lock.ts`, `src/lib/cli/nemoclaw-oclif-command.ts`, `src/commands/sandbox/exec.ts`, `src/lib/actions/sandbox/inference-invocation-probe.ts`, `docs/reference/troubleshooting.mdx`
- Issues: [#14070](https://github.com/NVIDIA/NemoClaw/issues/14070) · [#10663](https://github.com/NVIDIA/NemoClaw/issues/10663) · [#12281](https://github.com/NVIDIA/NemoClaw/issues/12281) · [#11903](https://github.com/NVIDIA/NemoClaw/issues/11903) · [#11132](https://github.com/NVIDIA/NemoClaw/issues/11132) · [#10461](https://github.com/NVIDIA/NemoClaw/issues/10461) · [#9746](https://github.com/NVIDIA/NemoClaw/issues/9746) · [#13017](https://github.com/NVIDIA/NemoClaw/issues/13017) · [#9737](https://github.com/NVIDIA/NemoClaw/issues/9737) · [#9604](https://github.com/NVIDIA/NemoClaw/issues/9604) · [#9109](https://github.com/NVIDIA/NemoClaw/issues/9109) · [#208](https://github.com/NVIDIA/NemoClaw/issues/208)
- Docs: [Troubleshooting](https://docs.nvidia.com/nemoclaw/reference/troubleshooting) · [CLI Commands Reference](https://docs.nvidia.com/nemoclaw/latest/reference/commands) · [Create Custom Policy Presets](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/network-policy/configure-policies/create-custom-policy-presets) · [Configure Inference Timeouts](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/inference/manage-inference/configure-inference-timeouts) · [Release notes 2026-11-03 (v0.0.141)](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/release-notes/2026/11/3)
- Measurement records: local probe output folders (not committed); fixtures: `tests/fixtures/nemoclaw/`.
