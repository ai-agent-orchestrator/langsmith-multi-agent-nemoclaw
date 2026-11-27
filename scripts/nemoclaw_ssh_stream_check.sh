#!/usr/bin/env bash
# 지속형 워커(ssh_stream)의 전제 확인 — 읽기 전용.
# 샌드박스에 파일을 만들지 않고, 정책·이미지를 바꾸지 않으며, nemoclaw 명령(호스트 잠금)을 쓰지 않는다.
#   1) openshell sandbox ssh-config 출력을 우리 코드가 받아들이는 형식인지
#   2) SSH 로 보낸 입력 한 줄의 응답이 입력을 닫기 전에 돌아오는지
#   3) 로그인 셸이 /etc/profile.d 를 읽는지, 워커가 설치돼 있는지
#
# 사용법 (WSL2): bash scripts/nemoclaw_ssh_stream_check.sh my-assistant
# 사용자 경로는 출력하지 않는다.
# shellcheck disable=SC2016  # 작은따옴표 안의 변수는 샌드박스 쪽 셸에서 펼친다.
set -u
trap '' PIPE

SANDBOX="${1:-}"
case "$SANDBOX" in
  ""|[!a-z]*|*[!a-z0-9-]*) echo "usage: $0 <sandbox-name>"; exit 2 ;;
esac
OPENSHELL="${OPENSHELL_BIN:-openshell}"
SSH="${SSH_BIN:-ssh}"
command -v "$OPENSHELL" >/dev/null 2>&1 || { echo "FAIL openshell 을 찾을 수 없음"; exit 1; }
command -v "$SSH" >/dev/null 2>&1 || { echo "FAIL ssh 클라이언트가 없음"; exit 1; }

umask 077
CONFIG="$(mktemp)" || exit 1
SSH_PROCESS=""
cleanup() {
  [ -n "$SSH_PROCESS" ] && kill "$SSH_PROCESS" 2>/dev/null
  rm -f "$CONFIG"
}
trap cleanup EXIT

if ! timeout 30 "$OPENSHELL" sandbox ssh-config "$SANDBOX" >"$CONFIG" 2>/dev/null; then
  echo "FAIL ssh-config 를 가져오지 못함"; exit 1
fi
ALIAS="$(awk '$1=="Host"{print $2; exit}' "$CONFIG")"
[ -n "$ALIAS" ] || { echo "FAIL ssh-config 에 Host 가 없음"; exit 1; }
echo "Host alias: $ALIAS"
# 실행 파일 경로는 이름만 남긴다.
awk '$1=="ProxyCommand"{n=split($2,p,"/"); $2=p[n]; print "ProxyCommand 형식:", substr($0, index($0,$2))}' "$CONFIG"

run() { timeout 30 "$SSH" -n -F "$CONFIG" -T -o BatchMode=yes -o ForwardAgent=no "$ALIAS" "$@"; }

echo "--- 세션 정보"
run 'echo "user=$(id -un) uid=$(id -u) cwd=$(pwd)"' || { echo "FAIL ssh 접속 실패"; exit 1; }
run 'if grep -q "profile\.d" /etc/profile 2>/dev/null; then echo "etc_profile_reads_profile_d=yes"; else echo "etc_profile_reads_profile_d=no"; fi'
run 'echo "worker_env_vars=$(env | grep -c "^ORCHESTRA_WORKER_")"'
run 'if [ -x /opt/orchestra/venv/bin/python ]; then echo "worker_python=present"; else echo "worker_python=absent"; fi'
BANNER_BYTES="$(run true | wc -c | tr -d ' ')"
echo "login_shell_extra_stdout_bytes=$BANNER_BYTES (0 이어야 함)"

echo "--- 줄 단위 왕복"
coproc STREAM { exec "$SSH" -F "$CONFIG" -T -o BatchMode=yes -o ForwardAgent=no "$ALIAS" \
  'while IFS= read -r line; do printf "got %s\n" "$line"; done' 2>/dev/null; }
SSH_PROCESS="$STREAM_PID"
exec {FROM_REMOTE}<&"${STREAM[0]}" {TO_REMOTE}>&"${STREAM[1]}"
RESULT=PASS
for word in one two three; do
  reply=""
  printf '%s\n' "$word" 1>&"$TO_REMOTE"
  IFS= read -r -t 20 -u "$FROM_REMOTE" reply
  echo "sent=$word received=${reply:-<20초 안에 응답 없음>}"
  [ "$reply" = "got $word" ] || { RESULT=FAIL; break; }
done
echo "STREAMING=$RESULT"
[ "$RESULT" = PASS ] && [ "$BANNER_BYTES" = 0 ]
