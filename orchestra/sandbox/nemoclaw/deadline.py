"""Stop a tool that overruns its time limit inside the worker process."""
from __future__ import annotations

import contextlib
import faulthandler
import os
import signal
import threading
from typing import Any, Iterator

# An alarm that fired while the context managers were still being entered could skip their cleanup.
_MIN_ALARM_S = 0.001


class ToolDeadlineExceeded(BaseException):
    """Not an Exception, so a tool's or a filter's own error handling does not swallow it."""


@contextlib.contextmanager
def tool_deadline(seconds: float, *, hard_exit_after_s: float | None = None) -> Iterator[None]:
    """Raise ToolDeadlineExceeded in the block after `seconds`.

    The interrupt needs the POSIX main thread and an unused alarm timer; without them the block runs on
    and the caller has to check the elapsed time. With `hard_exit_after_s` the whole process exits that
    much later if the block is still running, which also covers code that holds the interpreter lock or
    catches every exception. Use it only in a process that exists to be replaced.
    """
    with _hard_exit(None if hard_exit_after_s is None else seconds + hard_exit_after_s):
        with _interrupt(seconds):
            yield


@contextlib.contextmanager
def _hard_exit(seconds: float | None) -> Iterator[None]:
    if seconds is None:
        yield
        return
    # faulthandler's watchdog is a C thread: it fires even when no Python code can run.
    # Its traceback is discarded because it would name sandbox paths.
    # Open for writing only: the sandbox policy grants /dev/null, and nothing here needs to create it.
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        faulthandler.dump_traceback_later(seconds, exit=True, file=sink)
        try:
            yield
        finally:
            faulthandler.cancel_dump_traceback_later()
    finally:
        os.close(sink)


@contextlib.contextmanager
def _interrupt(seconds: float) -> Iterator[None]:
    if (not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL)[0] > 0):
        yield
        return
    armed = True

    def expire(signum: int, frame: Any) -> None:
        if armed:
            raise ToolDeadlineExceeded

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(seconds, _MIN_ALARM_S))
        yield
    finally:
        armed = False  # first, so a late alarm cannot interrupt the cleanup below
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL if previous is None else previous)
