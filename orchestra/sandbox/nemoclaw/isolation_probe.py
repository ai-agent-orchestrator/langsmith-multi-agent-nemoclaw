"""Try, from inside a worker sandbox, the things its policy should refuse.

Standard library only: scripts/nemoclaw_isolation_check.py sends this file to the sandbox's Python
over ssh stdin, so it also runs against an image built before it existed. Output is one line per
check, `CHECK <name> PASS|FAIL|UNKNOWN <detail>`, plus `INFO <name> <detail>` lines that are not
judged. It never prints file contents or environment values.
"""
from __future__ import annotations

import errno
import json
import os
import re
import socket
import stat
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

Result = tuple[str, str]
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
_REFUSED = (errno.EACCES, errno.EPERM, errno.EROFS)
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL")
CHECK_NAMES = ("control_tmp_write", "direct_tcp_refused", "proxy_tunnel_refused", "proxy_http_refused",
               "write_var_tmp_refused", "write_dev_shm_refused", "write_code_refused", "read_unlisted_refused",
               "not_root", "no_capabilities", "no_new_privileges", "memfd_refused")


def _name(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, type(error).__name__)


def control_write(directory: str) -> Result:
    """A write the policy allows must work, or the refusals below prove nothing."""
    path = os.path.join(directory, f"orchestra-probe-{uuid4().hex}")
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(path)
    except OSError as error:
        return UNKNOWN, _name(error)
    return PASS, "written_and_removed"


def write_refused(directory: str, *, needs_open_mode: bool = True) -> Result:
    """With `needs_open_mode`, require a world-writable directory so only the sandbox policy can refuse."""
    try:
        mode = os.stat(directory).st_mode
    except OSError as error:
        return UNKNOWN, _name(error)
    if needs_open_mode and not mode & stat.S_IWOTH:
        return UNKNOWN, "not_world_writable"
    path = os.path.join(directory, f"orchestra-probe-{uuid4().hex}")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        return (PASS if error.errno in _REFUSED else UNKNOWN), _name(error)
    os.close(descriptor)
    try:
        os.unlink(path)
    except OSError:
        return FAIL, "created_and_left_behind"
    return FAIL, "created"


def read_refused(path: str) -> Result:
    """The file must be world-readable, so only the sandbox policy can refuse. Nothing is read."""
    try:
        if not os.stat(path).st_mode & stat.S_IROTH:
            return UNKNOWN, "not_world_readable"
        os.close(os.open(path, os.O_RDONLY))
    except OSError as error:
        return (PASS if error.errno in _REFUSED else UNKNOWN), _name(error)
    return FAIL, "opened"


def direct_tcp(host: str, port: int, timeout_s: float = 5.0) -> Result:
    """A connection that bypasses the proxy must not be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return FAIL, "connected"
    except socket.timeout:
        return PASS, "timeout"
    except OSError as error:
        return PASS, _name(error)


def proxy_address(environ: dict[str, str]) -> tuple[str, int] | None:
    """Return the proxy the sandbox hands to its processes."""
    for variable in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        try:
            parts = urlsplit(environ.get(variable, ""))
            if parts.hostname and parts.port:
                return parts.hostname, parts.port
        except ValueError:
            continue
    return None


def _status(proxy: tuple[str, int], request: str, timeout_s: float) -> tuple[int | None, str]:
    """Return the HTTP status of the first reply line, or None with the reason there is none."""
    try:
        with socket.create_connection(proxy, timeout=timeout_s) as connection:
            connection.sendall(request.encode("ascii"))
            reply = b""
            while b"\n" not in reply and len(reply) < 1024:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                reply += chunk
    except socket.timeout:
        return None, "timeout"
    except OSError as error:
        return None, _name(error)
    match = re.match(rb"HTTP/1\.[01] (\d{3})", reply)
    return (int(match.group(1)), "") if match else (None, "unrecognized_reply")


def proxy_refused(proxy: tuple[str, int] | None, request: str, timeout_s: float = 10.0) -> Result:
    """Only an explicit 403 counts as refused; a missing or silent proxy leaves the question open."""
    if proxy is None:
        return UNKNOWN, "no_proxy_configured"
    status, reason = _status(proxy, request, timeout_s)
    if status is None:
        return UNKNOWN, reason
    if status == 403:
        return PASS, "status=403"
    return (FAIL if 200 <= status < 400 else UNKNOWN), f"status={status}"


def connect_request(host: str, port: int) -> str:
    return f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"


def get_request(host: str) -> str:
    return f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"


def process_status(text: str) -> dict[str, Result]:
    """Judge the privileges listed in /proc/self/status."""
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    capabilities = fields.get("CapEff", "").strip()
    locked = fields.get("NoNewPrivs", "").strip()
    empty = bool(capabilities) and set(capabilities) == {"0"}
    return {
        "no_capabilities": (UNKNOWN, "missing") if not capabilities else
                           ((PASS if empty else FAIL), f"CapEff={capabilities[:32]}"),
        "no_new_privileges": (UNKNOWN, "missing") if not locked else
                             ((PASS if locked == "1" else FAIL), f"NoNewPrivs={locked[:4]}"),
    }


def memfd_refused() -> Result:
    """The syscall filter blocks anonymous executable files."""
    create = getattr(os, "memfd_create", None)
    if create is None:
        return UNKNOWN, "unsupported"
    try:
        os.close(create("orchestra-probe"))
    except OSError as error:
        return (PASS if error.errno == errno.EPERM else UNKNOWN), _name(error)
    return FAIL, "created"


def run(config: dict[str, Any]) -> list[str]:
    """Run every check with targets chosen by the host script."""
    web, tcp_host, tcp_port = str(config["web_host"]), str(config["tcp_host"]), int(config["tcp_port"])
    proxy = proxy_address(dict(os.environ))
    checks: dict[str, Result] = {
        "control_tmp_write": control_write("/tmp"),
        "direct_tcp_refused": direct_tcp(tcp_host, tcp_port),
        "proxy_tunnel_refused": proxy_refused(proxy, connect_request(web, 443)),
        "proxy_http_refused": proxy_refused(proxy, get_request(web)),
        "write_var_tmp_refused": write_refused("/var/tmp"),
        "write_dev_shm_refused": write_refused("/dev/shm"),
        "write_code_refused": write_refused("/opt/orchestra", needs_open_mode=False),
        "read_unlisted_refused": read_refused("/var/lib/dpkg/status"),
        "not_root": ((PASS if os.getuid() != 0 else FAIL), f"uid={os.getuid()}"),
        "memfd_refused": memfd_refused(),
    }
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            checks.update(process_status(handle.read(65_536)))
    except OSError as error:
        checks.update({name: (UNKNOWN, _name(error)) for name in ("no_capabilities", "no_new_privileges")})
    lines = [f"CHECK {name} {result} {detail}" for name, (result, detail) in checks.items()]
    try:
        socket.getaddrinfo(web, 443)
        lines.append("INFO dns_resolution resolved")
    except OSError:
        lines.append("INFO dns_resolution failed")
    inference = config.get("inference_host")
    if inference and proxy is not None:
        status, reason = _status(proxy, connect_request(str(inference), 443), 10.0)
        lines.append(f"INFO inference_tunnel {f'status={status}' if status is not None else reason}")
    names = sorted(name for name in os.environ
                   if _SECRET_NAME.search(name.upper()) and not name.endswith("_FILE"))
    listed = f"count={len(names)},{','.join(names)}"[:200] if names else "none"
    lines.append(f"INFO secret_like_environment_names {listed}")
    return lines


if __name__ == "__main__":
    print("\n".join(run(json.loads(sys.argv[1]))))
