"""Check on a real install that the worker sandbox refuses what its policy says it refuses.

Run inside WSL2 from a checkout, after the worker sandbox exists. It sends
orchestra/sandbox/nemoclaw/isolation_probe.py to the sandbox's Python over the same ssh path the
worker uses. It changes nothing: the probe removes any file it manages to create. It makes a few
connection attempts to the targets below, from this machine and from the sandbox, sending no data
beyond the request line.

    python scripts/nemoclaw_isolation_check.py
    python scripts/nemoclaw_isolation_check.py --inference-host inference.local
    python scripts/nemoclaw_isolation_check.py --inference-host inference.local --send-prompt

--inference-host adds a judged check: it asks that host for its model list. --send-prompt also asks
for a one-token reply to the word "ping", which runs whatever model the gateway routes to (and may
cost money if that is a paid service); only the response status is reported.

Exit status: 0 ISOLATION=PASS, 1 FAIL (something was allowed), 2 INCOMPLETE (not shown either way).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "orchestra-code"
PYTHON = "/opt/orchestra/venv/bin/python"
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,200}[A-Za-z0-9])?")
# Sandbox output is untrusted: only these shapes are printed.
_CHECK = re.compile(r"CHECK ([a-z0-9_]{1,40}) (PASS|FAIL|UNKNOWN) ([A-Za-z0-9_=.,:-]{0,200})")
_INFO = re.compile(r"INFO ([a-z0-9_]{1,40}) ([A-Za-z0-9_=.,:-]{1,200})")
NETWORK_CHECKS = ("direct_tcp_refused", "proxy_tunnel_refused", "proxy_http_refused")


def host_name(value: str) -> str:
    if _HOST.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("not a host name")
    return value


def tcp_target(value: str) -> tuple[str, int]:
    """An IP address and port, so the direct attempt does not depend on name resolution."""
    host, _, port = value.rpartition(":")
    try:
        ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError("expected IP:port") from None
    if not 0 < number < 65536:
        raise argparse.ArgumentTypeError("port out of range")
    return host, number


def reachable(host: str, port: int, timeout_s: float = 5.0) -> bool:
    """Whether this machine can reach a target; if it cannot, a refusal inside proves nothing."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def judge(output: str, names: tuple[str, ...], *, targets_reachable: bool) -> tuple[list[str], str]:
    """Return the lines to print and PASS, FAIL or INCOMPLETE. A missing or repeated check is unknown."""
    seen: dict[str, list[tuple[str, str]]] = {}
    notes: list[str] = []
    for line in output.splitlines()[:200]:
        check, info = _CHECK.fullmatch(line), _INFO.fullmatch(line)
        if check and check[1] in names:
            seen.setdefault(check[1], []).append((check[2], check[3]))
        elif info:
            notes.append(line)
    lines, results = [], []
    for name in names:
        found = seen.get(name, [])
        result, detail = found[0] if len(found) == 1 else ("UNKNOWN", "repeated" if found else "missing")
        if any(item[0] == "FAIL" for item in found):
            result, detail = "FAIL", next(item[1] for item in found if item[0] == "FAIL")
        if name in NETWORK_CHECKS and result == "PASS" and not targets_reachable:
            result, detail = "UNKNOWN", "host_cannot_reach_target"
        lines.append(f"CHECK {name} {result} {detail}")
        results.append(result)
    verdict = "FAIL" if "FAIL" in results else "INCOMPLETE" if "UNKNOWN" in results else "PASS"
    return lines + notes[:20], verdict


def main(argv: list[str] | None = None) -> int:
    """Send the probe into the worker sandbox and print its judged results."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tcp-target", type=tcp_target, default=("1.1.1.1", 443), help="public IP:port")
    parser.add_argument("--web-host", type=host_name, default="example.com", help="public web host")
    parser.add_argument("--inference-host", type=host_name, default=None,
                        help="also check that no model route answers on this host")
    parser.add_argument("--send-prompt", action="store_true", help="also request a one-token reply")
    args = parser.parse_args(argv)
    if args.send_prompt and not args.inference_host:
        parser.error("--send-prompt needs --inference-host")
    sys.path.insert(0, str(ROOT))
    from orchestra.sandbox.nemoclaw.cli import NemoClawCli
    from orchestra.sandbox.nemoclaw.isolation_probe import CHECK_NAMES, INFERENCE_CHECK
    from orchestra.sandbox.nemoclaw.ssh_launcher import OpenShellSshLauncher

    config = {"tcp_host": args.tcp_target[0], "tcp_port": args.tcp_target[1], "web_host": args.web_host,
              "inference_host": args.inference_host, "send_prompt": args.send_prompt}
    names = CHECK_NAMES + ((INFERENCE_CHECK,) if args.inference_host else ())
    controls = reachable(*args.tcp_target) and reachable(args.web_host, 443) and reachable(args.web_host, 80)
    launcher = OpenShellSshLauncher(NemoClawCli(openshell=os.environ.get("OPENSHELL_BIN", "openshell"),
                                                dialect="modern"), ssh=os.environ.get("SSH_BIN", "ssh"))
    source = (ROOT / "orchestra" / "sandbox" / "nemoclaw" / "isolation_probe.py").read_bytes()
    try:
        command = launcher.argv(SANDBOX, shlex.join((PYTHON, "-I", "-", json.dumps(config))))
        done = subprocess.run(command, shell=False, input=source, capture_output=True, timeout=180)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"the probe could not run: {type(error).__name__}")
        print("ISOLATION=INCOMPLETE")
        return 2
    if done.returncode != 0:
        print(f"the probe exited with status {done.returncode}")
    lines, verdict = judge(done.stdout[:100_000].decode("utf-8", errors="replace"), names,
                           targets_reachable=controls)
    if done.returncode != 0 and verdict == "PASS":
        verdict = "INCOMPLETE"
    print("\n".join(lines))
    print(f"ISOLATION={verdict}")
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
