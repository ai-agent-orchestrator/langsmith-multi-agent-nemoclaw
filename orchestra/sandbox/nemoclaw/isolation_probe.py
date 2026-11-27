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
import ssl
import stat
import struct
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
# Judged only when the host script names the sandbox's inference host.
INFERENCE_CHECK = "inference_route_refused"
# Judged only when the host script asks for them.
DNS_CHECKS = ("dns_lookup_refused", "dns_query_refused")
PEER_CHECK = "peer_refused"
# OpenShell 0.0.116 answers this when the sandbox's workspace has no inference route.
_NO_ROUTE = b"cluster inference is not configured"


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


def _head(connection: socket.socket) -> tuple[int | None, bytes, bytes]:
    """Read one response head; return its status, the head and any body bytes that came with it."""
    reply = b""
    while b"\r\n\r\n" not in reply and len(reply) < 16_384:
        chunk = connection.recv(4096)
        if not chunk:
            break
        reply += chunk
    head, _, body = reply.partition(b"\r\n\r\n")
    match = re.match(rb"HTTP/1\.[01] (\d{3})", head)
    return (int(match.group(1)) if match else None), head, body


def inference_exchange(proxy: tuple[str, int], host: str, method: str, path: str, body: bytes = b"", *,
                       timeout_s: float = 20.0, context: Any = None) -> tuple[int | None, str, bytes]:
    """Send one request through the proxy tunnel to `host`.

    Returns (status, "", start of the body), or (None, reason, b"") when no response status arrived.
    """
    try:
        with socket.create_connection(proxy, timeout=timeout_s) as plain:
            plain.sendall(connect_request(host, 443).encode("ascii"))
            tunnel, _, _ = _head(plain)
            if tunnel != 200:
                return None, f"tunnel_status={tunnel}", b""
            context = context or ssl.create_default_context()
            with context.wrap_socket(plain, server_hostname=host) as secured:
                headers = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
                if body:
                    headers += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
                secured.sendall("\r\n".join(headers).encode("ascii") + b"\r\n\r\n" + body)
                status, head, received = _head(secured)
                length = re.search(rb"(?im)^content-length:\s*(\d{1,9})", head)
                wanted = min(int(length.group(1)), 4096) if length else 0
                while len(received) < wanted:
                    chunk = secured.recv(4096)
                    if not chunk:
                        break
                    received += chunk
    except socket.timeout:
        return None, "timeout", b""
    except ssl.SSLError:
        return None, "tls_failed", b""
    except OSError as error:
        return None, _name(error), b""
    return (status, "", received[:4096]) if status is not None else (None, "unrecognized_reply", b"")


def inference_refused(proxy: tuple[str, int] | None, host: str, **options: Any) -> Result:
    """Ask for the model list. Only a refused tunnel or "no route configured" counts as refused."""
    if proxy is None:
        return UNKNOWN, "no_proxy_configured"
    status, reason, body = inference_exchange(proxy, host, "GET", "/v1/models", **options)
    if status is None:
        return (PASS if reason == "tunnel_status=403" else UNKNOWN), reason
    if status == 503 and _NO_ROUTE in body:
        return PASS, "no_route"
    if 200 <= status < 300:
        return FAIL, f"model_server_answered,status={status}"
    return FAIL, f"route_present,status={status}"


def generation_report(proxy: tuple[str, int] | None, host: str, **options: Any) -> str:
    """Ask for a one-token reply to a fixed word; report only the status."""
    if proxy is None:
        return "no_proxy_configured"
    body = json.dumps({"model": "probe", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    status, reason, _ = inference_exchange(proxy, host, "POST", "/v1/chat/completions", body,
                                           **{"timeout_s": 60.0, **options})
    return reason if status is None else f"status={status}"


def dns_query(name: str, ident: int) -> bytes:
    """One question for the name's IPv4 address, with recursion requested."""
    labels = b"".join(bytes((len(part),)) + part for part in name.encode("ascii").split(b".") if part)
    return struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack(">HH", 1, 1)


def dns_reply(reply: bytes, ident: int) -> tuple[int, int] | None:
    """(response code, answer count) of a reply to our question, else None."""
    if len(reply) < 12:
        return None
    got, flags, _, answers, _, _ = struct.unpack(">HHHHHH", reply[:12])
    return (flags & 0x000F, answers) if got == ident and flags & 0x8000 else None


def ask_resolver(server: str, port: int, name: str, transport: str, timeout_s: float = 5.0) -> tuple[bytes, str]:
    """Send one DNS question over "udp" or "tcp"; return (reply bytes, "") or (b"", reason)."""
    ident = int.from_bytes(os.urandom(2), "big")
    query = dns_query(name, ident)
    try:
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        kind = socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
        with socket.socket(family, kind) as connection:
            connection.settimeout(timeout_s)
            connection.connect((server, port))
            if transport == "udp":
                connection.send(query)
                return connection.recv(4096), ""
            connection.sendall(struct.pack(">H", len(query)) + query)
            reply = b""
            while len(reply) < 2 or len(reply) < 2 + int.from_bytes(reply[:2], "big"):
                chunk = connection.recv(4096)
                if not chunk or len(reply) > 8192:
                    break
                reply += chunk
            return reply[2:], "" if len(reply) > 2 else "closed"
    except socket.timeout:
        return b"", "timeout"
    except OSError as error:
        return b"", _name(error)


def local_resolvers(text: str) -> list[str]:
    """The nameserver addresses of a resolv.conf."""
    found = [parts[1] for parts in (line.split() for line in text.splitlines())
             if len(parts) >= 2 and parts[0] == "nameserver"]
    return found[:3]


def dns_lookup_refused(name: str) -> Result:
    """The system resolver must not answer for a name no policy declares."""
    try:
        socket.getaddrinfo(name, 443)
    except OSError:
        return PASS, "not_resolved"
    return FAIL, "resolved"


def dns_query_refused(public: tuple[str, int], local: list[str], name: str, *, port: int = 53,
                      timeout_s: float = 5.0) -> Result:
    """No question may reach an outside resolver, and a configured resolver must not answer with records.

    Any bytes back from the outside resolver mean traffic left the sandbox. A configured resolver may
    refuse or return no records; an answer with records means the name was looked up for the sandbox.
    """
    seen = []
    for transport in ("udp", "tcp"):
        reply, reason = ask_resolver(public[0], public[1], name, transport, timeout_s)
        if reply:
            return FAIL, f"public_resolver_answered,transport={transport}"
        seen.append(f"public_{transport}={reason or 'empty'}")
    for server in local:
        reply, _ = ask_resolver(server, port, name, "udp", timeout_s)
        parsed = dns_reply(reply, int.from_bytes(reply[:2], "big")) if reply else None
        if parsed is not None and parsed[0] == 0 and parsed[1] > 0:
            return FAIL, "configured_resolver_answered"
    seen.append(f"configured={len(local)}")
    return PASS, ",".join(seen)


def peer_refused(proxy: tuple[str, int] | None, peers: list[tuple[str, int]], timeout_s: float = 5.0) -> Result:
    """Neighbours (another sandbox, a host service) must be closed both through the proxy and directly."""
    unknown = ""
    for number, (host, port) in enumerate(peers, 1):
        tunnel, detail = proxy_refused(proxy, connect_request(host, port), timeout_s)
        direct, _ = direct_tcp(host, port, timeout_s)
        if FAIL in (tunnel, direct):
            return FAIL, f"peer={number},{'tunnel_open' if tunnel == FAIL else 'connected'}"
        if tunnel != PASS:
            unknown = unknown or f"peer={number},{detail}"
    return (UNKNOWN, unknown) if unknown else (PASS, f"peers={len(peers)}")


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
    inference = config.get("inference_host")
    if inference:
        checks[INFERENCE_CHECK] = inference_refused(proxy, str(inference))
    if config.get("dns_server"):
        try:
            with open("/etc/resolv.conf", encoding="ascii", errors="replace") as handle:
                resolvers = local_resolvers(handle.read(65_536))
        except OSError:
            resolvers = []
        checks["dns_lookup_refused"] = dns_lookup_refused(web)
        checks["dns_query_refused"] = dns_query_refused((str(config["dns_server"]), 53), resolvers, web)
    peers = [(str(host), int(port)) for host, port in config.get("peers") or []]
    if peers:
        checks[PEER_CHECK] = peer_refused(proxy, peers)
    lines = [f"CHECK {name} {result} {detail}" for name, (result, detail) in checks.items()]
    try:
        socket.getaddrinfo(web, 443)
        lines.append("INFO dns_resolution resolved")
    except OSError:
        lines.append("INFO dns_resolution failed")
    if inference and config.get("send_prompt"):
        lines.append(f"INFO inference_generation {generation_report(proxy, str(inference))}")
    names = sorted(name for name in os.environ
                   if _SECRET_NAME.search(name.upper()) and not name.endswith("_FILE"))
    listed = f"count={len(names)},{','.join(names)}"[:200] if names else "none"
    lines.append(f"INFO secret_like_environment_names {listed}")
    return lines


if __name__ == "__main__":
    print("\n".join(run(json.loads(sys.argv[1]))))
