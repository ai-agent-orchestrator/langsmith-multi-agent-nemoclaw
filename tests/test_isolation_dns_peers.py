"""The optional DNS and neighbour checks, against loopback stand-ins: no sandbox, no external network."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import threading

import pytest

from orchestra.sandbox.nemoclaw import isolation_probe as probe

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("isolation_check_dns", ROOT / "scripts" / "nemoclaw_isolation_check.py")
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


@contextlib.contextmanager
def resolver(rcode: int = 0, answers: int = 1, *, udp: bool = True, tcp: bool = False):
    """A loopback DNS server on one port; records the questions it receives."""
    asked: list[bytes] = []
    datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    datagram.bind(("127.0.0.1", 0))
    port = datagram.getsockname()[1]
    listener = socket.create_server(("127.0.0.1", port)) if tcp else None
    if not udp:
        datagram.close()

    def reply(query: bytes) -> bytes:
        asked.append(query)
        return query[:2] + struct.pack(">HHHHH", 0x8180 | rcode, 1, answers, 0, 0) + query[12:]

    def serve_udp() -> None:
        with contextlib.suppress(OSError):
            while True:
                query, sender = datagram.recvfrom(4096)
                datagram.sendto(reply(query), sender)

    def serve_tcp() -> None:
        assert listener is not None
        with contextlib.suppress(OSError):
            while True:
                connection, _ = listener.accept()
                with connection:
                    query = connection.recv(4096)[2:]
                    answer = reply(query)
                    connection.sendall(struct.pack(">H", len(answer)) + answer)

    for target, wanted in ((serve_udp, udp), (serve_tcp, tcp)):
        if wanted:
            threading.Thread(target=target, daemon=True).start()
    try:
        yield port, asked
    finally:
        datagram.close()
        if listener is not None:
            listener.close()


def closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        return listener.getsockname()[1]


def test_the_question_is_a_single_a_record_query():
    query = probe.dns_query("example.com", 0x1234)
    assert query[:6] == b"\x12\x34\x01\x00\x00\x01"
    assert query.endswith(b"\x07example\x03com\x00\x00\x01\x00\x01")
    assert probe.dns_reply(b"short", 1) is None
    assert probe.dns_reply(query, 0x1234) is None  # a question is not a reply


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_any_answer_from_the_public_resolver_fails(transport):
    with resolver(rcode=5, answers=0, udp=transport == "udp", tcp=transport == "tcp") as (port, asked):
        result, detail = probe.dns_query_refused(("127.0.0.1", port), [], "example.com", timeout_s=2)
    assert (result, detail) == ("FAIL", f"public_resolver_answered,transport={transport}")
    assert asked


def test_a_silent_public_resolver_and_a_refusing_local_one_pass():
    with resolver(rcode=5, answers=0) as (port, asked):
        result, detail = probe.dns_query_refused(("127.0.0.1", closed_port()), ["127.0.0.1"], "example.com",
                                                 port=port, timeout_s=1)
    assert result == "PASS" and "configured=1" in detail and asked
    assert check._CHECK.fullmatch(f"CHECK dns_query_refused {result} {detail}")


@pytest.mark.parametrize("rcode,answers,expected", [(0, 1, "FAIL"), (0, 0, "PASS"), (3, 0, "PASS"), (2, 0, "PASS")])
def test_a_configured_resolver_fails_only_when_it_returns_records(rcode, answers, expected):
    with resolver(rcode=rcode, answers=answers) as (port, _):
        assert probe.dns_query_refused(("127.0.0.1", closed_port()), ["127.0.0.1"], "example.com",
                                       port=port, timeout_s=1)[0] == expected


def test_the_system_lookup_check(monkeypatch):
    assert probe.dns_lookup_refused("localhost") == ("FAIL", "resolved")

    def refuse(*args: object) -> None:
        raise socket.gaierror(-3, "no resolver")

    monkeypatch.setattr(probe.socket, "getaddrinfo", refuse)
    assert probe.dns_lookup_refused("example.com") == ("PASS", "not_resolved")


def test_resolver_addresses_come_from_resolv_conf():
    text = "# c\nnameserver 10.0.0.1\nsearch x\nnameserver fd00::1\nnameserver 10.0.0.2\nnameserver 10.0.0.3\n"
    assert probe.local_resolvers(text) == ["10.0.0.1", "fd00::1", "10.0.0.2"]


@contextlib.contextmanager
def proxy(status: int):
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        with contextlib.suppress(OSError):
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(f"HTTP/1.1 {status} X\r\n\r\n".encode())

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener.getsockname()
    finally:
        listener.close()


def test_a_neighbour_must_be_closed_both_ways():
    shut = ("127.0.0.1", closed_port())
    with proxy(403) as address:
        assert probe.peer_refused(address, [shut, shut], 1) == ("PASS", "peers=2")
        with socket.create_server(("127.0.0.1", 0)) as open_peer:
            assert probe.peer_refused(address, [shut, open_peer.getsockname()], 1) == ("FAIL", "peer=2,connected")
    with proxy(200) as address:
        assert probe.peer_refused(address, [shut], 1) == ("FAIL", "peer=1,tunnel_open")
    with proxy(502) as address:
        assert probe.peer_refused(address, [shut], 1) == ("UNKNOWN", "peer=1,status=502")
    assert probe.peer_refused(None, [shut], 1)[0] == "UNKNOWN"


@pytest.mark.parametrize("arguments", [["--peer", "host"], ["--peer", "a b:1"], ["--peer", "h:0"],
                                       ["--peer", "h:70000"], ["--dns-server", "resolver.test"],
                                       ["--peer", "h:1"] * 9])
def test_new_targets_are_validated(arguments):
    with pytest.raises(SystemExit):
        check.main(arguments)


def run_script(monkeypatch, capsys, arguments, *, host_reaches=True, host_resolver=True):
    from orchestra.sandbox.nemoclaw import ssh_launcher

    sent: dict = {}
    monkeypatch.setattr(ssh_launcher.OpenShellSshLauncher, "argv",
                        lambda self, sandbox, command: ["ssh-stand-in", sandbox, command])

    def connect(address, *args, **kwargs):
        if not host_reaches and address[0] == "10.1.2.3":
            raise OSError
        return contextlib.nullcontext()

    monkeypatch.setattr(check.socket, "create_connection", connect)
    monkeypatch.setattr(probe, "ask_resolver", lambda *a, **k: (b"reply" if host_resolver else b"", ""))

    def answer(argv, **kwargs):
        sent["config"] = json.loads(argv[2].split(" - ", 1)[1].strip("'"))
        names = probe.CHECK_NAMES + probe.DNS_CHECKS + (probe.PEER_CHECK,)
        text = "\n".join(f"CHECK {name} PASS ok" for name in names)
        return subprocess.CompletedProcess(argv, 0, text.encode(), b"")

    monkeypatch.setattr(check.subprocess, "run", answer)
    status = check.main(arguments)
    return status, capsys.readouterr().out, sent["config"]


def test_the_default_run_is_unchanged(monkeypatch, capsys):
    status, output, config = run_script(monkeypatch, capsys, [])
    assert status == 0 and "dns_" not in output and "peer_" not in output
    assert "dns_server" not in config and "peers" not in config


def test_the_optional_checks_are_sent_and_judged(monkeypatch, capsys):
    status, output, config = run_script(monkeypatch, capsys, ["--dns", "--peer", "10.1.2.3:80"])
    assert status == 0 and "CHECK dns_query_refused PASS" in output and "CHECK peer_refused PASS" in output
    assert config["dns_server"] == "1.1.1.1" and config["peers"] == [["10.1.2.3", 80]]


def test_without_a_host_control_the_new_checks_are_not_shown(monkeypatch, capsys):
    status, output, _ = run_script(monkeypatch, capsys, ["--dns", "--peer", "10.1.2.3:80"],
                                   host_reaches=False, host_resolver=False)
    assert status == 2
    assert "CHECK dns_query_refused UNKNOWN host_cannot_ask_resolver" in output
    assert "CHECK peer_refused UNKNOWN host_cannot_reach_target" in output
