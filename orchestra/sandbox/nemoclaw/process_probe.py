"""Report, from inside a worker sandbox, which worker processes exist and how much memory they use.

Standard library only: scripts/nemoclaw_load_check.py sends this file to the sandbox's Python over
ssh stdin. Output is `ALL <processes> <rss_kb>`, one `WORKER <pid> <rss_kb> <peak_kb>` per worker,
and `KILL <pid> OK|<errno>` for each requested process. It signals only processes that are workers
at that moment, and never prints command lines or environment values.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import sys


def command_line(pid: int) -> list[str]:
    """The process's argument list, empty when it cannot be read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read(65_536).decode("utf-8", errors="replace").split("\0")
    except OSError:
        return []


def memory_kb(pid: int) -> tuple[int, int] | None:
    """(resident, peak resident) in KiB, or None when the process cannot be read or has exited."""
    values: dict[str, int] = {}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in ("VmRSS", "VmHWM"):
                    values[name] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return (values["VmRSS"], values.get("VmHWM", values["VmRSS"])) if "VmRSS" in values else None


def is_worker(argv: list[str], marker: str) -> bool:
    """A Python process started with the marker as one whole argument (a shell quoting it does not count)."""
    return bool(argv) and os.path.basename(argv[0]).startswith("python") and marker in argv[1:]


def report(marker: str, kill: list[int]) -> list[str]:
    """Kill the requested workers, then describe what is running."""
    lines = []
    for pid in kill:
        if not is_worker(command_line(pid), marker):
            lines.append(f"KILL {pid} ESRCH")
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as error:
            lines.append(f"KILL {pid} {errno.errorcode.get(error.errno or 0, 'EUNKNOWN')}")
        else:
            lines.append(f"KILL {pid} OK")
    count = total = 0
    workers = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        used = memory_kb(int(name))
        if used is None:
            continue
        count, total = count + 1, total + used[0]
        if is_worker(command_line(int(name)), marker):
            workers.append(f"WORKER {name} {used[0]} {used[1]}")
    return [*lines, f"ALL {count} {total}", *workers]


def main() -> int:
    """Read the JSON configuration argument and print the report."""
    config = json.loads(sys.argv[1])
    print("\n".join(report(str(config["marker"]), [int(pid) for pid in config.get("kill", [])])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
