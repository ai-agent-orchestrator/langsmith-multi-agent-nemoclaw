#!/usr/bin/env bash
# NemoClaw 호스트 잠금(~/.nemoclaw-portable-host.lock) 진단·정리.
#
# 기본은 읽기 전용: 잠금 소유 PID, 그 프로세스의 명령·경과 시간, 다른 nemoclaw CLI 프로세스,
# 열린 connect/launch 세션(#9737: 열려 있으면 샌드박스 대상 명령이 전부 멈춤)을 보여 준다.
#
#   bash -l scripts/nemoclaw_lock_doctor.sh            # 진단만
#   bash -l scripts/nemoclaw_lock_doctor.sh --clean    # 오래된 nemoclaw CLI 프로세스만 종료
#   MIN_AGE=120 bash -l scripts/nemoclaw_lock_doctor.sh --clean
#
# --clean 이 종료하는 대상: 명령줄이 `node .../nemoclaw <...>` 인 CLI 프로세스 중
#   MIN_AGE(기본 90초) 이상 실행됐고, connect/launch/onboard/rebuild/destroy/update/uninstall 이 아닌 것.
# 종료하지 않는 것: openshell 게이트웨이, 샌드박스 컨테이너, docker, ollama, 대화형 세션, 설치·재생성 작업.
# 잠금 디렉터리 자체는 지우지 않는다 — 소유자가 죽으면 NemoClaw 가 회수하도록 둔다.
set -u
CLEAN=0
[ "${1:-}" = "--clean" ] && CLEAN=1
MIN_AGE="${MIN_AGE:-90}"
LOCK="${NEMOCLAW_LOCK:-$HOME/.nemoclaw-portable-host.lock}"
PROTECT_RE='nemoclaw(\.js)? +([a-z0-9-]+ +)?(connect|launch|onboard|rebuild|destroy|update|uninstall|upgrade-sandboxes|backup-all|setup)( |$)'

echo "== 잠금: $LOCK"
OWNER_PID=""
if [ -e "$LOCK" ]; then
  if [ -d "$LOCK" ]; then
    ls -la "$LOCK" 2>/dev/null | sed 's/^/   /'
    for f in "$LOCK"/owner "$LOCK"/owner.json "$LOCK"/pid; do
      [ -f "$f" ] && { echo "   -- $(basename "$f"):"; head -c 400 "$f" | sed 's/^/      /'; echo; OWNER_PID="$(grep -oE '[0-9]+' "$f" | head -1)"; break; }
    done
  else
    head -c 400 "$LOCK" | sed 's/^/   /'; echo
    OWNER_PID="$(grep -oE '[0-9]+' "$LOCK" | head -1)"
  fi
  if [ -n "$OWNER_PID" ]; then
    if kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "   소유 PID $OWNER_PID: 살아 있음"
      ps -o pid,ppid,etimes,stat,args -p "$OWNER_PID" | sed 's/^/   /'
    else
      echo "   소유 PID $OWNER_PID: 이미 종료됨 → 다음 nemoclaw 명령이 잠금을 회수해야 정상 (안 되면 #10461 증상)"
    fi
  else
    echo "   소유 PID 를 읽지 못함"
  fi
else
  echo "   잠금 없음"
fi

echo
echo "== nemoclaw CLI 프로세스 (경과 초 / 명령)"
mapfile -t ROWS < <(ps -eo pid=,etimes=,args= | awk '$3 ~ /node$/ && $0 ~ /nemoclaw/ {print}' | grep -v nemoclaw_lock_doctor)
if [ "${#ROWS[@]}" -eq 0 ]; then echo "   없음"; fi
CANDIDATES=()
for row in "${ROWS[@]}"; do
  pid="$(awk '{print $1}' <<<"$row")"; age="$(awk '{print $2}' <<<"$row")"
  tag=""
  if grep -qE "$PROTECT_RE" <<<"$row"; then tag="[보호: 대화형/설치 작업]"
  elif [ "$age" -ge "$MIN_AGE" ]; then tag="[정리 대상: ${MIN_AGE}초 이상]"; CANDIDATES+=("$pid")
  else tag="[진행 중]"; fi
  [ "$pid" = "$OWNER_PID" ] && tag="$tag [잠금 소유]"
  echo "   $row  $tag"
done

echo
echo "== 열린 connect / launch 세션 (#9737: 열려 있으면 샌드박스 대상 명령이 멈춤)"
ps -eo pid=,etimes=,args= | grep -E 'nemoclaw(\.js)? +([a-z0-9-]+ +)?(connect|launch)( |$)' | grep -v grep | sed 's/^/   /' || true
ps -eo pid=,etimes=,args= | grep -E 'nemoclaw(\.js)? +([a-z0-9-]+ +)?(connect|launch)( |$)' | grep -qv grep || echo "   없음"

echo
echo "== 샌드박스별 잠금 (mcp-lifecycle-locks) — exec/policy/status/doctor 가 모두 잡는다"
echo "   (v0.0.145 소스: 기본 대기 30분, 대기 중 출력 없음. 주인이 다른 PID 네임스페이스면 회수하지 않고 계속 기다림 = #14070)"
MY_NS="$(readlink /proc/self/ns/pid 2>/dev/null || echo unknown)"
MY_HOST="linux:$(cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null)"
mapfile -t SB_LOCKS < <(find "$HOME/.nemoclaw" -maxdepth 6 -path '*mcp-lifecycle-locks/*.lock' 2>/dev/null)
STALE_SB_LOCKS=()
if [ "${#SB_LOCKS[@]}" -eq 0 ]; then echo "   없음"; fi
for lockfile in "${SB_LOCKS[@]}"; do
  owner="$lockfile"; [ -d "$lockfile" ] && owner="$(find "$lockfile" -maxdepth 1 -type f | head -1)"
  verdict="$(python3 - "$owner" "$MY_NS" "$MY_HOST" <<'PY' 2>/dev/null
import json, os, sys
path, my_ns, my_host = sys.argv[1:4]
try:
    o = json.load(open(path))
except Exception:
    print("unreadable|?|?|?|?"); sys.exit()
pid = o.get("pid"); ns = o.get("pidNamespaceIdentity"); host = o.get("hostIdentity")
alive = False
if isinstance(pid, int):
    try:
        os.kill(pid, 0); alive = True
    except PermissionError:
        alive = True
    except OSError:
        alive = False
if host != my_host:
    state = "foreign-host"
elif ns not in (None, my_ns):
    state = "foreign-namespace"
elif alive:
    state = "live"
else:
    state = "dead-owner"
print(f"{state}|{o.get('sandboxName')}|{pid}|{o.get('acquiredAt')}|{'alive' if alive else 'dead'}")
PY
)"
  IFS='|' read -r state sbname pid acquired alive <<<"$verdict"
  echo "   $(basename "$lockfile")"
  echo "      sandbox=$sbname owner_pid=$pid ($alive) acquired=$acquired → $state"
  case "$state" in
    foreign-namespace) echo "      ⚠ 이전 WSL 부팅 등 다른 PID 네임스페이스에서 만든 잠금. NemoClaw 는 이걸 회수하지 않아 샌드박스 명령이 최대 30분 멈춘다."
                       [ "$alive" = dead ] && STALE_SB_LOCKS+=("$lockfile") ;;
    dead-owner)        echo "      주인 프로세스 종료됨 → 다음 명령이 회수해야 정상" ;;
    live)              ps -o pid,etimes,args -p "$pid" 2>/dev/null | tail -1 | sed 's/^/      주인: /' ;;
  esac
done

echo
echo "== OpenShell 게이트웨이 (정리 대상 아님)"
ps -eo pid=,etimes=,args= | grep -E '[o]penshell-gatew|[o]penshell.*gateway' | sed 's/^/   /' || true

if [ "$CLEAN" -eq 0 ]; then
  echo
  [ "${#CANDIDATES[@]}" -gt 0 ] && echo "정리 대상 프로세스 ${#CANDIDATES[@]}개."
  [ "${#STALE_SB_LOCKS[@]}" -gt 0 ] && echo "회수 대상 샌드박스 잠금 ${#STALE_SB_LOCKS[@]}개 (다른 네임스페이스, 주인 없음)."
  if [ "${#CANDIDATES[@]}" -gt 0 ] || [ "${#STALE_SB_LOCKS[@]}" -gt 0 ]; then
    echo "정리하려면: bash -l scripts/nemoclaw_lock_doctor.sh --clean"
  else
    echo "정리할 것 없음."
  fi
  exit 0
fi

echo
for pid in "${CANDIDATES[@]}"; do
  # 종료 직전에 다시 확인: 같은 PID 가 여전히 nemoclaw CLI 인지
  ps -o args= -p "$pid" 2>/dev/null | grep -q nemoclaw || { echo "   $pid: 이미 없음/다른 프로세스 → 건너뜀"; continue; }
  kill -TERM "$pid" 2>/dev/null
done
[ "${#CANDIDATES[@]}" -gt 0 ] && sleep 2
for pid in "${CANDIDATES[@]}"; do
  if kill -0 "$pid" 2>/dev/null && ps -o args= -p "$pid" | grep -q nemoclaw; then
    kill -KILL "$pid" 2>/dev/null; echo "   프로세스 $pid: 강제 종료"
  else
    echo "   프로세스 $pid: 종료됨"
  fi
done

if [ "${#STALE_SB_LOCKS[@]}" -gt 0 ]; then
  # 다른 nemoclaw CLI 가 하나라도 돌고 있으면 잠금은 건드리지 않는다 (상호 배제를 깨지 않기 위해).
  if ps -eo args= | awk '$1 ~ /node$/ && /nemoclaw/' | grep -v nemoclaw_lock_doctor | grep -q .; then
    echo "   nemoclaw CLI 가 아직 실행 중이라 샌드박스 잠금은 건드리지 않음. 끝난 뒤 다시 실행하세요."
  else
    for lockfile in "${STALE_SB_LOCKS[@]}"; do
      dest="$lockfile.stale-$(date +%Y%m%d-%H%M%S)"
      # 지우지 않고 이름만 바꾼다 — 필요하면 되돌릴 수 있다.
      mv "$lockfile" "$dest" && echo "   샌드박스 잠금 회수: $(basename "$lockfile") → $(basename "$dest")"
    done
  fi
fi
echo "완료. 재확인: bash -l scripts/nemoclaw_lock_doctor.sh"
