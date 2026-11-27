"""Keep one sandbox worker running and exchange one JSON line per request.

A request is sent at most once: after any failure the session is killed and the error is
returned, because the worker may already have executed a side effect. The worker's journal
answers a caller's own retry that reuses the idempotency key.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import selectors
import signal
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

from .cli import _child_env
from .worker import MAX_BYTES, MAX_LINE_BYTES, PROTOCOL_VERSION

Launcher = Callable[[str], list[str]]


class _SessionFailed(Exception):
    """Carries only a fixed error code."""


class _Session:
    """One worker process in its own process group, read and written under deadlines."""

    def __init__(self, argv: list[str]) -> None:
        # stderr is discarded: it may hold sandbox paths or tool output and nothing here needs it.
        self.process = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=_child_env(), start_new_session=True,
                                        bufsize=0)
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin, self.stdout = self.process.stdin.fileno(), self.process.stdout.fileno()
        os.set_blocking(self.stdin, False)
        os.set_blocking(self.stdout, False)
        self.buffer = bytearray()

    @staticmethod
    def _wait(fd: int, event: int, deadline: float) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise _SessionFailed("worker_timeout")

    def write(self, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            self._wait(self.stdin, selectors.EVENT_WRITE, deadline)
            try:
                view = view[os.write(self.stdin, view):]
            except BlockingIOError:
                continue
            except OSError:
                raise _SessionFailed("worker_exited") from None

    def readline(self, deadline: float) -> bytes:
        scanned = 0
        while True:
            end = self.buffer.find(b"\n", scanned)
            if end >= 0:
                # The worker only speaks when asked, so anything after the reply breaks the framing.
                if end + 1 != len(self.buffer):
                    raise _SessionFailed("worker_protocol_error")
                line = bytes(self.buffer[:end])
                self.buffer.clear()
                return line
            scanned = len(self.buffer)
            if scanned > MAX_LINE_BYTES:
                raise _SessionFailed("worker_output_too_large")
            self._wait(self.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self.stdout, 65_536)
            except BlockingIOError:
                continue
            except OSError:
                raise _SessionFailed("worker_exited") from None
            if not chunk:
                raise _SessionFailed("worker_exited")
            self.buffer.extend(chunk)

    def usable(self) -> bool:
        """An idle session is reusable only if it is alive and has said nothing since the last reply."""
        if self.process.poll() is not None or self.buffer:
            return False
        with selectors.DefaultSelector() as selector:
            selector.register(self.stdout, selectors.EVENT_READ)
            return not selector.select(0)

    def signal_group(self, sig: int) -> None:
        """Signal the whole group even if the leader already exited (e.g. a leftover ssh proxy)."""
        with contextlib.suppress(OSError):
            os.killpg(self.process.pid, sig)

    def kill(self) -> None:
        """Stop the group and release the pipes; only the thread that owns the slot may call this."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self.signal_group(sig)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


@dataclass
class _Slot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    session: _Session | None = None
    failures: int = 0
    blocked_until: float = 0.0


def _shutdown(slots: dict[str, _Slot]) -> None:
    for slot in list(slots.values()):
        if slot.lock.acquire(blocking=False):
            try:
                if slot.session is not None:
                    slot.session.kill()
                    slot.session = None
            finally:
                slot.lock.release()
        else:
            # A request is in flight: its thread sees the pipe close and cleans up itself.
            session = slot.session
            if session is not None:
                session.signal_group(signal.SIGKILL)


class PersistentTransport:
    """One long-lived worker per sandbox; requests to the same sandbox are serialized."""

    def __init__(self, launcher: Launcher, *, startup_timeout_s: float = 30.0, max_failures: int = 3,
                 cooldown_s: float = 30.0) -> None:
        if not (math.isfinite(startup_timeout_s) and startup_timeout_s > 0 and max_failures >= 1
                and math.isfinite(cooldown_s) and cooldown_s >= 0):
            raise ValueError("invalid_worker_transport_configuration")
        self.launcher, self.startup_timeout_s = launcher, startup_timeout_s
        self.max_failures, self.cooldown_s = max_failures, cooldown_s
        self._slots: dict[str, _Slot] = {}
        self._guard = threading.Lock()
        self._closed = False
        self._finalizer = weakref.finalize(self, _shutdown, self._slots)

    def close(self) -> None:
        """Stop every worker; later exchanges are refused."""
        with self._guard:
            self._closed = True
        _shutdown(self._slots)

    def _start(self, sandbox: str) -> _Session:
        try:
            session = _Session(self.launcher(sandbox))
        except Exception:
            raise _SessionFailed("worker_launch_failed") from None
        try:
            hello = json.loads(session.readline(time.monotonic() + self.startup_timeout_s))
            if hello != {"orchestra_worker": PROTOCOL_VERSION, "ready": True}:
                raise _SessionFailed("worker_handshake_failed")
        except (ValueError, RecursionError):
            session.kill()
            raise _SessionFailed("worker_handshake_failed") from None
        except BaseException:
            session.kill()
            raise
        return session

    def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str:
        """Send one serialized request and return the serialized result, or raise a fixed error code.

        `timeout_s` bounds waiting for the sandbox's turn and, separately, the request itself;
        starting a worker is bounded by `startup_timeout_s`.
        """
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise RuntimeError("worker_invalid_timeout")
        if len(payload.encode()) > MAX_BYTES:
            raise RuntimeError("worker_request_too_large")
        request_id = uuid4().hex
        line = json.dumps({"id": request_id, "request": payload}, ensure_ascii=False).encode() + b"\n"
        if len(line) > MAX_LINE_BYTES:
            raise RuntimeError("worker_request_too_large")
        with self._guard:
            if self._closed:
                raise RuntimeError("worker_transport_closed")
            slot = self._slots.setdefault(sandbox, _Slot())
        if not slot.lock.acquire(timeout=min(timeout_s, threading.TIMEOUT_MAX)):
            raise RuntimeError("worker_busy")
        try:
            if self._closed:
                raise RuntimeError("worker_transport_closed")
            return self._exchange(slot, sandbox, request_id, line, timeout_s)
        finally:
            slot.lock.release()

    def _exchange(self, slot: _Slot, sandbox: str, request_id: str, line: bytes, timeout_s: float) -> str:
        if slot.session is not None and not slot.session.usable():
            slot.session.kill()  # nothing has been sent yet, so starting over is safe
            slot.session = None
        if slot.session is None and slot.failures >= self.max_failures and time.monotonic() < slot.blocked_until:
            raise RuntimeError("worker_restart_limited")
        succeeded = False
        try:
            if slot.session is None:
                slot.session = self._start(sandbox)
            if self._closed:
                raise _SessionFailed("worker_transport_closed")
            deadline = time.monotonic() + timeout_s
            slot.session.write(line, deadline)
            try:
                frame = json.loads(slot.session.readline(deadline))
            except (ValueError, RecursionError):
                raise _SessionFailed("worker_protocol_error") from None
            if (not isinstance(frame, dict) or set(frame) != {"id", "response"} or frame["id"] != request_id
                    or not isinstance(frame["response"], str)):
                raise _SessionFailed("worker_protocol_error")
            if len(frame["response"].encode(errors="replace")) > MAX_BYTES:
                raise _SessionFailed("worker_output_too_large")
            succeeded = True
            return frame["response"]
        except _SessionFailed as error:
            raise RuntimeError(str(error)) from None
        finally:
            if succeeded:
                slot.failures = 0
                if self._closed and slot.session is not None:
                    slot.session.kill()
                    slot.session = None
            else:  # includes interrupts: the session state is unknown
                slot.failures += 1
                slot.blocked_until = time.monotonic() + self.cooldown_s
                if slot.session is not None:
                    slot.session.kill()
                    slot.session = None
