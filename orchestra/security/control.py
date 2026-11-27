"""Thread-safe team circuit breakers and a global stop set by an environment variable or a stop file."""
from __future__ import annotations

import os
import threading

STOP_FILE_ENV = "ORCHESTRA_KILL_SWITCH_FILE"


def _stop_file_present(path: str) -> bool:
    """Only a clear "no such file" in an existing directory means run; anything uncertain means stop."""
    if not os.path.isabs(path):
        return True
    try:
        os.lstat(path)
    except FileNotFoundError:
        return not os.path.isdir(os.path.dirname(path))
    except (OSError, ValueError):
        return True
    return True


class SecurityControl:
    """Latch a team after consecutive denies; only explicit operator reset clears it."""

    def __init__(self, threshold: int = 5, teams: dict[str, str] | None = None) -> None:
        if type(threshold) is not int or threshold < 1:
            raise ValueError("invalid security circuit threshold")
        self.threshold, self.teams = threshold, teams or {}
        self._failures: dict[str, int] = {}
        self._tripped: set[str] = set()
        self._lock = threading.RLock()

    def guard(self, scope: str) -> str | None:
        """Check the kill switch and the stop file on every call so running processes see updates."""
        stop = os.environ.get("ORCHESTRA_KILL_SWITCH", "0").strip().lower()
        stop_file = os.environ.get(STOP_FILE_ENV, "")
        if stop not in {"0", "false", "off", ""} or (stop_file and _stop_file_present(stop_file)):
            return "global_kill_switch"
        with self._lock:
            return "team_circuit_open" if self.teams.get(scope, scope) in self._tripped else None

    def record(self, scope: str, *, blocked: bool) -> None:
        """Count consecutive denials without allowing successes to clear a latched stop."""
        scope = self.teams.get(scope, scope)
        with self._lock:
            self._failures[scope] = self._failures.get(scope, 0) + 1 if blocked else 0
            if self._failures[scope] >= self.threshold:
                self._tripped.add(scope)

    def reset(self, scope: str) -> None:
        """Explicit operator action; this method is never exposed as an agent tool."""
        scope = self.teams.get(scope, scope)
        with self._lock:
            self._failures.pop(scope, None)
            self._tripped.discard(scope)
