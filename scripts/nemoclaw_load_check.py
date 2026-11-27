"""Measure the persistent worker on a real install: latency, memory, and replacement after a loss.

Run inside WSL2 from a checkout, after the worker sandbox exists, with nothing else using it. Every
request names a tool that does not exist, so no tool runs and no key is needed. The script starts
worker processes of its own, kills some of them to check recovery, and removes any it leaves behind.
It changes no policy, file or sandbox setting.

    python scripts/nemoclaw_load_check.py
    python scripts/nemoclaw_load_check.py --requests 200 --callers 8 --workers 4

Exit status: 0 LOAD=PASS, 1 FAIL, 2 INCOMPLETE (something could not be observed).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "orchestra-code"
PYTHON = "/opt/orchestra/venv/bin/python"
MARKER = "orchestra.sandbox.nemoclaw.worker"
PROBE = ROOT / "orchestra" / "sandbox" / "nemoclaw" / "process_probe.py"
REQUEST_TIMEOUT_S = 30.0
GONE_WAIT_S = 15.0
# Sandbox output is untrusted: only these shapes are read.
_ALL = re.compile(r"ALL (\d{1,7}) (\d{1,12})")
_WORKER = re.compile(r"WORKER (\d{1,10}) (\d{1,12}) (\d{1,12})")
_KILL = re.compile(r"KILL (\d{1,10}) (OK|E[A-Z]{1,20})")


class Unobservable(RuntimeError):
    """The sandbox could not be inspected; carries only a fixed code."""


class Snapshot:
    """What one probe run reported."""
    def __init__(self, text: str) -> None:
        self.processes = self.rss_kb = -1
        self.workers: dict[int, tuple[int, int]] = {}
        self.kills: dict[int, str] = {}
        for line in text.splitlines()[:500]:
            if found := _ALL.fullmatch(line):
                self.processes, self.rss_kb = int(found[1]), int(found[2])
            elif found := _WORKER.fullmatch(line):
                self.workers[int(found[1])] = (int(found[2]), int(found[3]))
            elif found := _KILL.fullmatch(line):
                self.kills[int(found[1])] = found[2]
        if self.processes < 0:
            raise Unobservable("probe_output_unrecognized")


class Check:
    """Drive one sandbox through the measurements; `launch` and `inspect` hide how it is reached."""

    def __init__(self, sandbox: str, launch: Callable[[str], list[str]], inspect: Callable[[str], list[str]],
                 *, marker: str = MARKER, say: Callable[[str], None] = print, gone_wait_s: float = GONE_WAIT_S) -> None:
        self.sandbox, self.launch, self.inspect = sandbox, launch, inspect
        self.marker, self.say, self.gone_wait_s = marker, say, gone_wait_s
        self.results: list[str] = []
        self.baseline: set[int] = set()

    def judge(self, line: str, result: str) -> None:
        """Print one measurement with PASS, FAIL or UNKNOWN."""
        self.results.append(result)
        self.say(f"{line} {result}")

    def snapshot(self, kill: list[int] | None = None) -> Snapshot:
        """Run the probe in the sandbox, optionally killing the given worker processes."""
        config = json.dumps({"marker": self.marker, "kill": kill or []})
        try:
            done = subprocess.run(self.inspect(config), shell=False, input=PROBE.read_bytes(),
                                  capture_output=True, timeout=90)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            raise Unobservable("probe_could_not_run") from None
        if done.returncode != 0:
            raise Unobservable("probe_failed")
        return Snapshot(done.stdout[:100_000].decode("utf-8", errors="replace"))

    def new_workers(self, snapshot: Snapshot) -> dict[int, tuple[int, int]]:
        """Workers that were not running before this check started."""
        return {pid: used for pid, used in snapshot.workers.items() if pid not in self.baseline}

    def wait_until_gone(self) -> int:
        """Return how many of this run's workers are still there after waiting for them to exit."""
        deadline = time.monotonic() + self.gone_wait_s
        while True:
            left = len(self.new_workers(self.snapshot()))
            if not left or time.monotonic() >= deadline:
                return left
            time.sleep(1)

    def ask(self, transport: Any) -> tuple[float, str]:
        """One request for a tool that does not exist; returns (seconds, "" or an error code)."""
        payload = json.dumps({"sandbox": self.sandbox, "tool": "orchestra_load_probe"})
        started = time.monotonic()
        try:
            reply = json.loads(transport.exchange(self.sandbox, payload, REQUEST_TIMEOUT_S))
            error = "" if reply.get("error") == "worker_tool_forbidden" else "unexpected_reply"
        except RuntimeError as failure:
            error = str(failure) if re.fullmatch(r"[a-z_]{1,60}", str(failure)) else "transport_error"
        except (ValueError, AttributeError):
            error = "unexpected_reply"
        return time.monotonic() - started, error

    def timings(self, label: str, samples: list[tuple[float, str]], elapsed: float) -> None:
        """Summarize latency and throughput; any failed request fails the line."""
        good = sorted(seconds for seconds, error in samples if not error)
        errors = sorted({error for _, error in samples if error})
        rank = lambda share: good[max(0, math.ceil(share * len(good)) - 1)] * 1000  # noqa: E731
        shape = f"p50={rank(0.5):.0f}ms p95={rank(0.95):.0f}ms max={good[-1] * 1000:.0f}ms " if good else ""
        failed = len(samples) - len(good)
        self.judge(f"{label} n={len(samples)} {shape}per_s={len(good) / max(elapsed, 1e-9):.1f} errors={failed}"
                   + (f" codes={','.join(errors)}" if errors else ""), "FAIL" if failed else "PASS")

    def load(self, requests: int, callers: int) -> None:
        """Measure one persistent worker, then its replacement after a crash."""
        from orchestra.sandbox.nemoclaw.persistent import PersistentTransport

        transport = PersistentTransport(self.launch, startup_timeout_s=60)
        try:
            seconds, error = self.ask(transport)
            self.judge(f"STARTUP first_request={seconds:.2f}s" + (f" code={error}" if error else ""),
                       "FAIL" if error else "PASS")
            if error:
                return
            started = time.monotonic()
            samples = [self.ask(transport) for _ in range(requests)]
            self.timings("SEQUENTIAL", samples, time.monotonic() - started)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=callers) as pool:
                samples = list(pool.map(lambda _: self.ask(transport), range(requests)))
            self.timings(f"CONCURRENT callers={callers}", samples, time.monotonic() - started)
            snapshot = self.snapshot()
            mine = self.new_workers(snapshot)
            if not mine:
                self.judge("ONE_WORKER worker_not_visible", "UNKNOWN")
                return
            rss, peak = (sum(used[index] for used in mine.values()) / 1024 for index in (0, 1))
            self.judge(f"ONE_WORKER processes={len(mine)} rss={rss:.0f}MiB peak={peak:.0f}MiB "
                       f"visible_in_sandbox={snapshot.rss_kb / 1024:.0f}MiB", "PASS" if len(mine) == 1 else "FAIL")
            self.crash(transport, list(mine))
        finally:
            transport.close()

    def crash(self, transport: Any, pids: list[int]) -> None:
        """Kill the worker inside the sandbox; the transport has to answer again on a new one."""
        killed = self.snapshot(kill=pids)
        if any(killed.kills.get(pid) != "OK" for pid in pids):
            codes = ",".join(sorted(set(killed.kills.values()))) or "missing"
            self.judge(f"AFTER_WORKER_CRASH kill_refused={codes}", "UNKNOWN")
            return
        time.sleep(1)  # let the ssh session notice, as it would between two real requests
        started = time.monotonic()
        errors = []
        for attempt in (1, 2):
            _, error = self.ask(transport)
            if not error:
                break
            errors.append(error)
        recovered = len(errors) < 2
        fresh = set(self.new_workers(self.snapshot())) - set(pids)
        self.judge(f"AFTER_WORKER_CRASH attempts={attempt} recovery={time.monotonic() - started:.2f}s "
                   f"new_worker={'yes' if fresh else 'no'}" + (f" first_error={errors[0]}" if errors else ""),
                   "PASS" if recovered and fresh else "FAIL")

    def connection_loss(self) -> None:
        """Kill only the host side of a session; the worker in the sandbox has to end by itself."""
        process = subprocess.Popen(self.launch(self.sandbox), shell=False, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                                   bufsize=0)
        try:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                ready = bool(selector.select(60)) and b'"ready": true' in process.stdout.read(4096)
            if not ready:
                self.judge("AFTER_CONNECTION_LOSS worker_did_not_start", "FAIL")
                return
            if not self.new_workers(self.snapshot()):
                self.judge("AFTER_CONNECTION_LOSS worker_not_visible", "UNKNOWN")
                return
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            left = self.wait_until_gone()
            self.judge(f"AFTER_CONNECTION_LOSS leftover={left}", "FAIL" if left else "PASS")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def many(self, workers: int) -> None:
        """Hold several workers open at once, as several host processes sharing a sandbox would."""
        from orchestra.sandbox.nemoclaw.persistent import PersistentTransport

        transports = [PersistentTransport(self.launch, startup_timeout_s=120) for _ in range(workers)]
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                samples = list(pool.map(self.ask, transports))
            failed = sorted({error for _, error in samples if error})
            snapshot = self.snapshot()
            mine = self.new_workers(snapshot)
            rss = sum(used[0] for used in mine.values()) / 1024
            result = "FAIL" if failed else "PASS" if len(mine) == workers else "UNKNOWN"
            self.judge(f"MANY_WORKERS asked={workers} running={len(mine)} rss_total={rss:.0f}MiB "
                       f"rss_each={rss / max(len(mine), 1):.0f}MiB visible_in_sandbox={snapshot.rss_kb / 1024:.0f}MiB"
                       + (f" codes={','.join(failed)}" if failed else ""), result)
        finally:
            for transport in transports:
                transport.close()

    def run(self, requests: int, callers: int, workers: int) -> str:
        """Run every measurement, remove leftovers and return PASS, FAIL or INCOMPLETE."""
        try:
            before = self.snapshot()
            self.baseline = set(before.workers)
            self.say(f"BASELINE workers={len(self.baseline)} visible_in_sandbox={before.rss_kb / 1024:.0f}MiB")
            try:
                self.load(requests, callers)
                self.connection_loss()
                self.many(workers)
                left = self.wait_until_gone()
                self.judge(f"CLEANUP leftover={left}", "FAIL" if left else "PASS")
            finally:
                mine = list(self.new_workers(self.snapshot()))
                if mine:
                    self.snapshot(kill=mine)
                    self.say(f"removed {len(mine)} worker process(es) this run left behind")
        except Unobservable as error:
            self.judge(f"INSPECT {error}", "UNKNOWN")
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.judge(f"RUN stopped={type(error).__name__}", "UNKNOWN")
        return "FAIL" if "FAIL" in self.results else "INCOMPLETE" if "UNKNOWN" in self.results else "PASS"


def bounded(low: int, high: int) -> Callable[[str], int]:
    """An argparse type for a whole number in a fixed range."""
    def parse(value: str) -> int:
        if not value.isdigit() or not low <= int(value) <= high:
            raise argparse.ArgumentTypeError(f"expected a whole number from {low} to {high}")
        return int(value)

    return parse


def main(argv: list[str] | None = None) -> int:
    """Run the measurements against the worker sandbox and print one line for each."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", type=bounded(1, 2000), default=100, help="requests per latency run")
    parser.add_argument("--callers", type=bounded(1, 32), default=8, help="threads in the concurrent run")
    parser.add_argument("--workers", type=bounded(1, 16), default=4, help="workers held open at once")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from orchestra.sandbox.nemoclaw.cli import NemoClawCli
    from orchestra.sandbox.nemoclaw.ssh_launcher import OpenShellSshLauncher

    launcher = OpenShellSshLauncher(NemoClawCli(openshell=os.environ.get("OPENSHELL_BIN", "openshell"),
                                                dialect="modern"), ssh=os.environ.get("SSH_BIN", "ssh"))

    def inspect(config: str) -> list[str]:
        return launcher.argv(SANDBOX, shlex.join((PYTHON, "-I", "-", config)))

    verdict = Check(SANDBOX, launcher, inspect).run(args.requests, args.callers, args.workers)
    print(f"LOAD={verdict}")
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[verdict]


if __name__ == "__main__":
    raise SystemExit(main())
