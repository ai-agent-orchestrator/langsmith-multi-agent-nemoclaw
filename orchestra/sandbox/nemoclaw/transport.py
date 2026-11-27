"""JSON transport with a fixed worker command; local transport is for tests only."""
from __future__ import annotations

from typing import Callable, Protocol

from .cli import NemoClawCli


class Transport(Protocol):
    """Transport receives serialized data, never a host function or command."""

    def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str: ...


class StdioTransport:
    """Call only the allowlisted worker through an explicitly verified CLI."""

    def __init__(self, cli: NemoClawCli, *, exec_verified: bool = False) -> None:
        self.cli = cli
        self.exec_verified = exec_verified

    def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str:
        """Refuse unverified or dry-run execution instead of reporting false success."""
        if not self.exec_verified or self.cli.dry_run:
            raise RuntimeError("worker_transport_unverified")
        result = self.cli.run("worker", sandbox=sandbox, input_text=payload, timeout_s=timeout_s)
        if not result.ok:
            raise RuntimeError(result.error)
        if len(result.stdout.encode()) > 1_048_576:
            raise RuntimeError("worker_output_too_large")
        return result.stdout


class LocalTransport:
    """In-process JSON round trip for deterministic offline contract testing."""

    def __init__(self, handler: Callable[[str], str]) -> None:
        self.handler = handler

    def exchange(self, sandbox: str, payload: str, timeout_s: float) -> str:
        """Forward serialized requests to a separately configured test worker."""
        return self.handler(payload)
