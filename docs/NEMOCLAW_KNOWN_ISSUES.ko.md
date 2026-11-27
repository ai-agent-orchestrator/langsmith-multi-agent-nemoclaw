# NemoClaw 흔한 문제 정리 — 증상 · 원인 · 우회 · 면접 포인트

[English](NEMOCLAW_KNOWN_ISSUES.md) | **한국어**

> 이 문서가 가리키는 스크립트와 코드(`scripts/nemoclaw_*.sh`, `orchestra/sandbox/nemoclaw/cli.py`, `tests/fixtures/nemoclaw/`)는 [`21-nemoclaw-cli`](https://github.com/ai-agent-orchestrator/langsmith-multi-agent-nemoclaw/tree/21-nemoclaw-cli) 브랜치에 있다.

> 기준: **nemoclaw v0.0.145 / openshell 0.0.116**, WSL2 Ubuntu 26.04 + Docker Desktop, 샌드박스 `my-assistant`(OpenClaw, ollama `qwen3:1.7b`, GPU).
> 근거는 세 가지다. ① 2026-11-27 실측(`scripts/nemoclaw_probe.sh`, `nemoclaw_hang_diag.sh`), ② NemoClaw v0.0.145 소스(`NVIDIA/NemoClaw` 태그 `v0.0.145`, 커밋 `6f36fef`), ③ 공개 이슈·공식 문서.
> 표시: **[실측]** 이 환경에서 재현 · **[소스]** v0.0.145 코드에서 확인 · **[이슈]/[문서]** 공개 자료 근거 · **[추정]** 아직 확인 안 됨.

---

## 0. 한눈에 보기

| # | 증상 | 원인 | 바로 할 일 | 우리 코드의 대응 |
|---|---|---|---|---|
| 1 | 샌드박스 명령(`exec`, `policy list`, `status`, `doctor`)이 **출력 없이 수십 초~30분 멈춤** | 샌드박스별 잠금(`mcp-lifecycle-locks`) 대기. 기본 30분, 대기 중 무출력. 주인이 다른 PID 네임스페이스면 회수 안 함 **[실측 확인][소스][이슈 #14070]** | `scripts/nemoclaw_lock_doctor.sh` → 필요 시 `--clean` | 실행 경로를 nemoclaw 에 기대지 않음, 타임아웃 시 실패 처리 |
| 2 | `Failed to acquire lock on ~/.nemoclaw-portable-host.lock after 120 retries` | 호스트 잠금을 다른 nemoclaw 프로세스(대개 멈춘 것)가 쥐고 있음. 0.1초×120회 ≈ 12초 후 포기 **[실측][소스]** | 잠금 소유 PID 확인 후 그 프로세스만 종료 | `nemoclaw_host_lock_busy` 로 분류, 우리 호출은 한 번에 하나 |
| 3 | 타임아웃으로 끊은 뒤에도 잠금이 안 풀림 | 부모만 죽고 자식/고아 프로세스가 잠금을 계속 쥠 **[이슈 #10663]** | `ps` 로 남은 `node …/nemoclaw` 확인 후 종료 | 타임아웃 시 **프로세스 그룹 전체** 종료 |
| 4 | 같은 명령을 동시에 두 번 돌리면 둘 다 실패 | 모든 명령이 호스트 잠금으로 직렬화됨 **[실측]** | 동시에 돌리지 않기 | 점검 스크립트 중복 실행 방지, CLI 단일 실행 잠금 |
| 5 | `nemoclaw <sb> status`(텍스트)는 느리고 `status --json`은 빠름 | 텍스트 status 는 샌드박스 안에서 **실제 추론 요청**(시도당 90초, 최대 3회)까지 보냄 **[소스][문서]** | 상태 확인은 `status --json` | 실행 중 점검은 `status_json` 만 사용 |
| 6 | 모든 게 안 됨, `failureLayer: docker_unreachable` | Docker Desktop 꺼짐 → 게이트웨이 다운 **[실측]** | Docker Desktop 켜기 → `nemoclaw <sb> recover` | 시작 시 doctor 실패면 기동 거부 |
| 7 | `connect` 세션이 열려 있으면 다른 샌드박스 명령이 전부 멈춤 | v0.0.111 에서 생긴 회귀로 보고됨, 현재 버전 수정 여부 미확인 **[이슈 #9737]** | 다른 터미널의 `connect`/`launch` 닫기 | lock doctor 가 열린 세션 표시 |
| 8 | 정책 일부를 바꿨는데 다른 프리셋이 **조용히 사라짐** | `openshell policy set` 은 병합이 아니라 **전체 교체** **[문서][소스]** | 변경은 `nemoclaw <sb> policy add` 만 | `policy set` 은 명령 목록에 없음(테스트로 고정) |
| 9 | 채널을 지웠는데 그 채널용 네트워크 허용이 남아 있음 | 프리셋 잔존 버그 **[이슈 #9109]**(이후 릴리스에서 수정 보고) | `policy list` 로 정기 점검 | 정책 드리프트 검사 |
| 10 | 샌드박스가 생각보다 넓게 외부와 통신 가능 | 기본 프리셋 일부가 `access: full` + `tls: skip`(경로 규칙 미적용) **[실측]** | 안 쓰는 프리셋 `policy remove` | 프리셋 생성기는 경로 단위 규칙만 생성 |
| 11 | 파일시스템 격리가 기대보다 약함 | `landlock: compatibility: best_effort` — 커널이 못 받치면 **조용히 약화** **[실측][문서]** | 커널 5.13+ 확인 | 2차 방어(샌드박스 안 필터, N3) 필요 |
| 12 | CLI 가 NVIDIA 로 통신함 | v0.0.141부터 운영 텔레메트리 **기본 켜짐** **[문서]** | `export NEMOCLAW_DISABLE_TELEMETRY=1` | 래퍼가 기본으로 끔(`ORCHESTRA_NEMOCLAW_TELEMETRY=1` 로만 켬) |
| 13 | onboard 가 WSL2 에서 멈춤 | Windows 쪽 Ollama 확인(PowerShell) 지연 **[이슈 #9604, 수정됨]**, sudo+pty 타임아웃 **[이슈 #10663, #12281 재발]** | 최신 버전, Windows Ollama 대신 WSL Ollama | — |
| 14 | 이미지 빌드 중 멈춤 / exit 137 | 메모리 부족(이미지 압축 약 2.4GB), 런타임 자원 부족 **[문서]** | Docker Desktop 자원 상향, 스왑 | — |
| 15 | 샌드박스 안에서 `CONNECT tunnel failed, response 403` | 정책에 없는 호스트 → 프록시 거부. 정상 동작 **[문서]** | `nemoclaw <sb> logs` 로 거부 흐름 확인 후 프리셋 검토 | egress 반복 거부 시 샌드박스 격리(NS5) |

---

## 1. 왜 이런 문제가 생기나 — 구조

```text
nemoclaw (Node CLI, 호스트)        ← 편의 계층: onboard, 프리셋, 상태 점검, 잠금
   │  호스트 잠금 ~/.nemoclaw-portable-host.lock
   │  샌드박스별 잠금 ~/.nemoclaw/state/mcp-lifecycle-locks/<sha256(이름)>.lock
   ▼
openshell (CLI) ──► openshell-gateway (127.0.0.1:8080)   ← 실제 격리 엔진: 정책 강제, 실행
                         ▼
                Docker 컨테이너 안 k3s ──► 샌드박스 (OpenClaw, landlock, 네트워크 프록시)
```

- **불안정한 쪽은 위층(nemoclaw CLI)이다.** 같은 시점에 `openshell policy get --base`는 1초 안에 끝났고, nemoclaw 의 샌드박스 명령은 45초 넘게 멈췄다 **[실측]**.
- nemoclaw 는 "안전하게 멈추는" 쪽으로 설계돼 있다. 잠금 주인을 확신할 수 없으면 뺏지 않고 기다린다. 보안에는 맞지만 **가용성은 나빠진다**. 특히 WSL2 처럼 PID 네임스페이스가 부팅마다 바뀌는 환경에서 그렇다.

## 2. 잠금 문제 자세히

### 2.1 호스트 잠금 `~/.nemoclaw-portable-host.lock`
- 디렉터리이고, 안의 `owner` 파일에 PID 가 들어 있다 **[소스: `src/lib/state/registry/lock.ts`]**.
- 재시도는 `LOCK_RETRY_MS = 100`, `LOCK_MAX_RETRIES = 120`이라 약 12초 뒤에 실패한다. 실측 13.1초와 일치한다.
- 메시지의 "portable"은 Portable 프로필을 안 써도 나온다. 이름이 오해를 부른다는 지적이 있다 **[이슈 #11903]**.
- 같은 잠금 문제가 경로만 바꿔 반복 보고된다: 중단된 onboard 가 남긴 잠금을 회수 못 함 **[#10461]**, uninstall 경합 시 원인 대신 재시도 소진만 표시 **[#11132]**, 소유권 오류가 고쳐진 뒤 v0.0.132 에서 재발 **[#9746 → #13017]**.

### 2.2 샌드박스별 잠금 `mcp-lifecycle-locks`
- 대화형 세션(`connect` 등)을 뺀 **모든 샌드박스 대상 명령**이 공통 기반 클래스에서 `withSandboxLifecycleLock`으로 감싸진다. 호스트 잠금(펜스)을 잡은 뒤 이 잠금을 또 잡는다 **[소스: `src/lib/cli/nemoclaw-oclif-command.ts` `runAdmittedLifecycleBoundary`, `src/lib/actions/sandbox/lifecycle/lock.ts`]**.
- 예외는 `status` 하나다. 호스트 잠금이 바쁘면 잠금 없이 읽는 경로로 빠지고 추론 확인을 생략한다 → `status --json`만 빨랐던 이유 **[소스][실측]**.
- 기본 대기 시간은 `DEFAULT_TIMEOUT_MS = 30 * 60_000`, 즉 **30분**이고 기다리는 동안 출력이 없다 **[소스: `src/lib/state/mcp-lifecycle-lock-acquisition.ts`]**.
- 잠금 파일에는 `pid`, `hostIdentity`(machine-id), `pidNamespaceIdentity`(`/proc/self/ns/pid`)가 기록된다. 주인이 **다른 호스트이거나 다른 PID 네임스페이스면 "active"로 보고 절대 회수하지 않는다** **[소스: `mcp-lifecycle-lock-identity.ts` `classifyMcpLifecycleLock`]**.
  - 의도: 공유 디렉터리에서 남의 프로세스를 잘못 판단해 상호 배제를 깨지 않으려는 것이다.
  - 부작용: WSL 을 재시작하면 이전 부팅에서 남은 잠금이 영원히 "살아 있는 주인"으로 취급된다 → 30분 멈춤 **[이슈 #14070]**.

### 2.3 실제 사례 (2026-11-27)
- 증상: `my-assistant` 대상 nemoclaw 명령이 모두 무출력으로 45초 이상 멈춤. 텍스트 `status`, `doctor`, `policy list/get`, `exec -- true`, dry-run 모두 같았다.
- 진단(`nemoclaw_lock_doctor.sh`): 샌드박스 잠금이 **2026-11-25 04:44 UTC**에 만들어진 채 남아 있었다. 주인 PID 는 이미 종료됐고 `foreign-namespace`였다(이후 WSL 이 재부팅됨).
- 조치: `--clean`으로 잠금 파일 이름을 바꿨다 → `nemoclaw my-assistant policy list` **45초 이상 → 1.15초**.
- 교훈: WSL 재시작이나 PC 재부팅 뒤 nemoclaw 샌드박스 명령이 말없이 멈추면, 다른 걸 의심하기 전에 lock doctor 부터 돌린다.

### 2.4 진단과 정리
```bash
bash -l scripts/nemoclaw_lock_doctor.sh          # 읽기 전용 진단
bash -l scripts/nemoclaw_lock_doctor.sh --clean  # 멈춘 CLI 프로세스 종료 + 주인 없는 외부 네임스페이스 잠금 이름 변경
```
- 종료 대상: `node …/nemoclaw …` CLI 프로세스 중 90초 이상 지난 것. `connect`/`launch`/`onboard`/`rebuild`/`destroy`/`update`/`uninstall`은 건드리지 않는다.
- 잠금 파일은 **지우지 않고** `*.stale-<시각>`으로 이름만 바꾼다(되돌릴 수 있음). nemoclaw CLI 가 하나라도 돌고 있으면 잠금은 건드리지 않는다.
- 게이트웨이, 샌드박스 컨테이너, docker, ollama 는 대상이 아니다.

## 3. WSL2 에서 특히 자주 보이는 것

| 증상 | 원인 / 확인 | 조치 |
|---|---|---|
| `docker` 가 `npipe:////./pipe/dockerDesktopLinuxEngine` 오류 | Docker Desktop 이 꺼져 있음 **[실측]** | Docker Desktop 실행. WSL integration 에서 Ubuntu 켜기 **[문서]** |
| WSL 안 `docker ps` 가 정상인데 nemoclaw 만 멈춤 | 2절 잠금 | lock doctor |
| PowerShell 에서 받은 저장소의 `.sh` 실행 시 `$'\r': command not found` | Windows git 이 CRLF 로 체크아웃 **[실측]** | `.gitattributes`에 `*.sh text eol=lf` (적용함) |
| PowerShell 의 `git clone … ~/x` 가 `C:\WINDOWS\system32\~\x`에 생김 | PowerShell 은 네이티브 명령 인자의 `~`를 확장하지 않음 **[실측]** | WSL 안에서 작업하거나 전체 경로 사용 |
| PowerShell 에서 `bash -lc '… $! …'` 결과 PID 가 빔 | PowerShell → bash 로 넘어가며 특수 변수·따옴표가 깨짐 **[실측]** | 긴 명령은 스크립트 파일로 |
| onboard 가 "Configuring inference provider"에서 멈춤 | Windows 쪽 Ollama 확인이 타임아웃 없이 대기 **[이슈 #9604, 수정됨]** | 최신 버전 |
| WSL2 + GPU 에서 샌드박스가 생성 직후 사라짐 | 초기 버전이 `--gpu`를 강제 **[이슈 #208, 초기]** | 현재 버전은 GPU 검증 후 사용(실측: `cudaVerified: true`) |

## 4. "느리다"의 정체

- **`nemoclaw <sb> status`(텍스트):** 게이트웨이·샌드박스 상태를 확인한 뒤, 샌드박스 안에서 추론 엔드포인트로 **실제 요청**을 보낸다. 시도당 90초, 일시 오류면 최대 3회다 **[소스: `inference-invocation-probe.ts` `INFERENCE_INVOCATION_REQUEST_TIMEOUT_SECONDS = 90`][문서]**. 로컬 모델이 처음 메모리에 올라가는 중이면 오래 걸린다.
- **`nemoclaw <sb> status --json`:** 다른 작업이 진행 중이면 추론 확인을 생략한다(`inferenceProbeSuppressed: true`) **[실측][문서]**. 실측 2초.
- **`openshell policy get --base <sb>`:** 게이트웨이에 바로 묻는다. 실측 1초 미만.
- 결론: **실행 중 점검에는 `status --json`과 `openshell`을 쓰고, 텍스트 `status`/`doctor`는 사람이 볼 때만 쓴다.**

## 5. 정책(네트워크·파일시스템) 함정

1. **`openshell policy set`은 전체 교체다.** 일부만 넣으면 나머지 프리셋이 조용히 사라진다. 변경은 `nemoclaw <sb> policy add`(병합 후 검증)로만 한다 **[문서]**.
2. **`--base` vs `--full`:** `--base`는 사람이 넣은 정책만 보여 준다(프리셋 이름이 키). `--full`/`status`는 provider 가 합성한 `_provider_*` 항목까지 보여 준다 **[실측]**. 드리프트 비교 기준을 하나로 정해야 한다. 우리는 `--base`를 쓴다.
3. **넓은 기본 프리셋:** `brew-balanced`는 `github.com`, `ghcr.io` 등에 `access: full`을 주고, 일부는 `tls: skip`까지 붙인다 **[실측]**. TLS 를 건너뛰면 경로 규칙을 검사할 수 없으므로 "그 호스트로는 무엇이든"이 된다 → 데이터 유출 경로가 된다.
4. **`binaries` 글롭 허용:** `/usr/bin/python3*`, `/sandbox/.uv/python/**/python*` **[실측]**. 실행 파일 이름만으로 정밀하게 격리하기 어렵다 → **격리 단위는 샌드박스**로 잡는다.
5. **커스텀 프리셋은 NemoClaw 의 검토를 거치지 않는다.** 이름은 소문자 RFC 1123 이어야 하고, 내장 프리셋 이름(`pypi`, `slack` 등)과 겹치면 안 된다 **[문서]**.
6. **비대화 실행에서는 `--yes` 필요:** 없으면 확인 프롬프트에서 멈춘다. 미리보기는 `--dry-run` **[실측: `policy add --help`]**.
7. **채널 프리셋 잔존:** 채널을 지워도 허용 정책이 남는 버그가 있었다 **[이슈 #9109]**. `policy list`로 정기 확인한다.

## 6. 추론(inference) 함정

- 샌드박스 안 에이전트는 `inference.local`로만 모델을 부르고, OpenShell 이 실제 엔드포인트로 중계한다 **[문서][실측: `managed_inference` 프리셋]**.
- 로컬 Ollama 가 루프백에만 바인딩돼 있으면 샌드박스에서 못 닿는다. `host.docker.internal`은 k3s 네트워크에서 신뢰할 수 없다 **[문서]**. 실측 경로는 `host.openshell.internal:11435`(사설 IP 대역만 허용)이다.
- 컨텍스트 길이 부족(OpenClaw 는 16384 이상 권장), 콜드 로드 지연 → 관련 타임아웃 환경변수: `NEMOCLAW_AGENT_TIMEOUT`(기본 600초, 이미지에 고정), `NEMOCLAW_LOCAL_INFERENCE_TIMEOUT`(180초), `NEMOCLAW_SANDBOX_READY_TIMEOUT`(180초) **[문서]**.

## 7. 자원·플랫폼

- 샌드박스 이미지는 압축 상태로 약 2.4GB **[문서]**. 메모리 8GB 이하 + 스왑 없음이면 이미지 푸시 중 OOM(exit 137) **[문서]**.
- 컨테이너 런타임 자원이 4 vCPU / 8GiB 미만이면 빌드가 무출력으로 멈출 수 있다 **[문서]**.
- Landlock 은 `best_effort`라서, 커널이 지원하지 않으면 **경고만 하고 약화된 채로 실행**된다 **[문서][실측 정책]**.

## 8. 텔레메트리

- v0.0.141부터 설치기·CLI 운영 텔레메트리가 **기본으로 켜져** NVIDIA 로 전송된다 **[문서: 11/3 릴리스 노트]**.
- 끄기: 셸마다 `export NEMOCLAW_DISABLE_TELEMETRY=1` **[문서: CLI Commands Reference]**.
- 폐쇄망·규제 환경이라면 기본값을 반드시 확인해야 하는 항목이다. 우리 래퍼는 기본으로 끈다.

## 9. 이 저장소의 대응 (코드 위치)

| 문제 | 대응 | 위치 |
|---|---|---|
| 멈춤 | 모든 호출에 타임아웃, 초과 시 **프로세스 그룹 종료**, 실패는 차단(fail closed) | `orchestra/sandbox/nemoclaw/cli.py` `_kill_group` |
| 잠금 경합 | 우리 nemoclaw 호출은 `flock` 단일 실행. 잠금 오류는 `nemoclaw_host_lock_busy` | `cli.py` `_single_flight` |
| 느린 읽기 | 정책 조회는 `openshell policy get --base`, 상태는 `status --json` | `cli.py` `command()` |
| 출력 형식 | OpenShell 헤더(`Version/Hash/…` + `---`) 파싱. 모르는 헤더는 거부 | `orchestra/sandbox/nemoclaw/live.py` `split_policy_output` |
| 정책 교체 위험 | `policy set` 명령 없음. 변경은 승인 후 `policy add --from-file … --yes` 한 번만 | `cli.py`, `orchestra/security/policy_change.py` |
| 텔레메트리 | 하위 프로세스 환경에 `NEMOCLAW_DISABLE_TELEMETRY=1` | `cli.py` `_child_env` |
| 운영 진단 | 실측 점검, 멈춤 진단, 잠금 진단·정리 | `scripts/nemoclaw_probe.sh`, `nemoclaw_hang_diag.sh`, `nemoclaw_lock_doctor.sh` |
| 호출마다 `exec` | 선택형 지속 워커(`NEMOCLAW_TRANSPORT=ssh_stream`): OpenShell SSH 로 워커를 한 번 띄워 줄 단위 JSON 을 주고받음. 실패하면 세션을 종료하고 **재전송하지 않음** **[소스; SSH 왕복은 실측, 워커 실연동 미확인]** | `persistent.py`, `ssh_launcher.py`, [REALITY §3.1](NEMOCLAW_REALITY.md) |
| 직접 만든 이미지 | `nemoclaw onboard --from`은 NemoClaw 런타임이 든 이미지가 필요 → 워커는 `openshell sandbox create --from` 사용 **[문서·소스, 실설치 미실행]** | `scripts/nemoclaw_worker_sandbox.py`, [REALITY §3.2](NEMOCLAW_REALITY.md) |
| 빈 네트워크 정책 | `policy get`이 빈 `network_policies`를 생략 → OpenShell 출력일 때만 "규칙 없음"으로 해석 **[소스]** | `orchestra/sandbox/nemoclaw/live.py` `parse_policy` |
| 회귀 방지 | 실측 출력 fixture 기반 테스트 | `tests/fixtures/nemoclaw/`, `tests/test_nemoclaw_cli_hardening.py` |

`nemoclaw <sb> exec`는 원격 명령이 끝날 때까지 2절의 두 잠금 안에서 돌고 **[소스]**, 파이프 입력을 끝까지 읽은 뒤에야 명령을 시작한다(OpenShell 0.0.116) **[소스]**. 그래서 상시 워커에는 쓸 수 없다.

지속 워커의 한계: 같은 샌드박스의 요청은 하나씩 처리한다. 호스트가 시간 초과로 SSH 를 끊어도 샌드박스 안에서 실행 중인 툴까지 종료되는지는 확인하지 않았다. 기존 1회 실행 방식(`stdio_via_cli`)은 작업 디렉터리가 `sys.path` 앞에 올 수 있다(지속 워커는 `-P`로 막음).

남은 일: 지속 워커 실설치 확인, 커스텀 프리셋 키 형식(`nemoclaw_custom__…`) 실측 확인.

---

## 10. 면접 포인트

**Q1. 샌드박스 명령이 출력 없이 멈춥니다. 어떻게 접근했나요?**
- 증상을 계층별로 나눴다. Docker(`docker ps` 0.05초) → 게이트웨이(`openshell policy get` 1초 미만) → nemoclaw CLI(45초 초과) 순으로 재서 **문제 계층을 CLI 로 한정**했다.
- 멈춘 프로세스를 관찰했다. 하위 프로세스는 없었고 Node 혼자 대기 중이었다 → 외부 프로그램이 아니라 내부 대기다.
- 해당 버전 소스를 읽어 원인을 특정했다. 30분 무출력 대기 잠금과, 다른 PID 네임스페이스의 주인은 회수하지 않는 규칙이었다. 공개 이슈와도 대조했다.
- 재발 방지: 진단 도구, 래퍼의 타임아웃·프로세스 그룹 종료, 실행 경로 분리.

**Q2. 왜 NemoClaw 는 오래된 잠금을 그냥 지우지 않나요? 그게 맞는 설계인가요?**
- 잠금 주인이 다른 네임스페이스나 다른 호스트에 있으면 "죽었다"고 확신할 수 없다. 잘못 지우면 두 작업이 동시에 정책을 바꾸는 더 큰 사고가 난다.
- 그래서 **안전성(상호 배제)을 가용성보다 우선**한 선택이다. 문제는 그 대가를 사용자에게 알리지 않는다는 점이다(30분 무출력).
- 개선안: 대기 중 진행 메시지, 주인 정보 표시, 운영자 명시 회수 명령, 리스(lease) 만료.

**Q3. fail-closed 와 가용성은 어떻게 균형을 잡나요?**
- 보안 판단 경로(허용/차단)는 fail-closed 로 둔다. 응답이 없으면 차단한다.
- 대신 차단이 장애로 번지지 않게 한다: 짧은 타임아웃, 빠른 대체 읽기 경로(`openshell`), 명확한 오류 코드(`cli_busy`, `nemoclaw_host_lock_busy`), 사람이 쓰는 진단 도구.
- "막혔다"와 "왜 막혔는지 모른다"는 다르다. 앞의 것은 설계이고, 뒤의 것은 결함이다.

**Q4. 정책 변경을 왜 `policy add`만 허용하나요?**
- `policy set`은 전체 교체라서, 부분 문서를 넣으면 기존 허용이 조용히 사라지거나 반대로 넓어질 수 있다.
- 병합 + 사후 검증(드리프트 검사) + 사람 승인 + 감사 기록을 한 번의 추가 호출로 묶어야 추적할 수 있다. 실패하면 재시도하지 않고 "결과 불명"으로 남긴다. 같은 변경이 두 번 적용되는 것을 막기 위해서다.

**Q5. 샌드박스가 있는데 왜 별도 보안 계층(가드)이 필요한가요?**
- 샌드박스(담장)는 "어디로 갈 수 있나"를 막는다. 허용된 경로로 하는 나쁜 행동은 못 막는다. 예: 허용된 결제 API 로 다른 금액 결제, 허용된 GitHub 로 개인정보 전송.
- 가드(대문)는 "이걸 해도 되나"를 판단한다: 인젝션, 승인 없는 결제, 권한 밖 위임.
- 게다가 이 환경의 담장은 `best_effort` Landlock, `access: full` 프리셋처럼 생각보다 낮을 수 있다 → 다층 방어가 필요하다.

**Q6. 외부 alpha 도구를 운영에 들일 때 원칙은?**
- 버전 고정, 실측으로 문법 확인, 실측 출력을 fixture 로 만들어 회귀 테스트.
- 인터페이스 뒤에 숨겨 교체 가능하게 하고(`SandboxRunner`), 실행 경로에서는 가장 안정적인 하위 계층(`openshell`)에 붙는다.
- 기본값을 검토한다: 텔레메트리 기본 켜짐, 넓은 기본 프리셋.
- 알려진 문제를 문서화하고 업스트림 이슈와 연결한다(이 문서).

**Q7. 디버깅하면서 직접 겪은 실수는?**
- 동시에 점검을 두 번 돌려 잠금 경합을 스스로 만들었다 → 단일 실행 잠금을 추가했다.
- "Docker 가 Windows 쪽 프로그램이라 느리다"는 가설을 세웠는데, 측정(0.047초)으로 기각했다 → **가설은 측정으로 확인한 뒤 고친다**.

---

## 출처

- NemoClaw v0.0.145 소스: `src/lib/state/registry/lock.ts`, `src/lib/state/mcp-lifecycle-lock-acquisition.ts`, `src/lib/state/mcp-lifecycle-lock-identity.ts`, `src/lib/actions/sandbox/lifecycle/lock.ts`, `src/lib/cli/nemoclaw-oclif-command.ts`, `src/commands/sandbox/exec.ts`, `src/lib/actions/sandbox/inference-invocation-probe.ts`, `docs/reference/troubleshooting.mdx`
- 이슈: [#14070](https://github.com/NVIDIA/NemoClaw/issues/14070) · [#10663](https://github.com/NVIDIA/NemoClaw/issues/10663) · [#12281](https://github.com/NVIDIA/NemoClaw/issues/12281) · [#11903](https://github.com/NVIDIA/NemoClaw/issues/11903) · [#11132](https://github.com/NVIDIA/NemoClaw/issues/11132) · [#10461](https://github.com/NVIDIA/NemoClaw/issues/10461) · [#9746](https://github.com/NVIDIA/NemoClaw/issues/9746) · [#13017](https://github.com/NVIDIA/NemoClaw/issues/13017) · [#9737](https://github.com/NVIDIA/NemoClaw/issues/9737) · [#9604](https://github.com/NVIDIA/NemoClaw/issues/9604) · [#9109](https://github.com/NVIDIA/NemoClaw/issues/9109) · [#208](https://github.com/NVIDIA/NemoClaw/issues/208)
- 문서: [Troubleshooting](https://docs.nvidia.com/nemoclaw/reference/troubleshooting) · [CLI Commands Reference](https://docs.nvidia.com/nemoclaw/latest/reference/commands) · [Create Custom Policy Presets](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/network-policy/configure-policies/create-custom-policy-presets) · [Configure Inference Timeouts](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/inference/manage-inference/configure-inference-timeouts) · [Release notes 2026-11-03 (v0.0.141)](https://docs.nvidia.com/nemoclaw/user-guide/openclaw/release-notes/2026/11/3)
- 실측 기록: 로컬 점검 결과 폴더(커밋하지 않음), fixture: `21-nemoclaw-cli` 브랜치의 `tests/fixtures/nemoclaw/`
