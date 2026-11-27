"""Build the ssh command that runs the persistent worker through OpenShell.

`openshell sandbox exec` (and `nemoclaw exec`, which calls it) reads piped stdin to EOF before it
starts the command (OpenShell 0.0.116), so it cannot carry a long-lived request stream. OpenShell's
SSH endpoint reaches the same in-sandbox process launcher and forwards stdin as it arrives.
"""
from __future__ import annotations

import os
import re
import shlex

from .cli import NemoClawCli, SERVE_COMMAND

_SAFE = re.compile(r"[A-Za-z0-9_./:@-]{1,512}")
_PROXY_FLAGS = ("--gateway-name", "--name", "--workspace")
# Host keys are not pinned, as in OpenShell's own ssh-config: the channel is authenticated by the
# ssh-proxy connection to the gateway. User config, keys, agent and forwarding stay disabled.
_OPTIONS = (
    "BatchMode=yes", "RequestTTY=no", "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null",
    "GlobalKnownHostsFile=/dev/null", "LogLevel=ERROR", "ServerAliveInterval=15", "ServerAliveCountMax=3",
    "PubkeyAuthentication=no", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
    "IdentityAgent=none", "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
)


def parse_ssh_config(text: str, sandbox: str, openshell: str) -> tuple[str, str]:
    """Return (host alias, proxy command) from `openshell sandbox ssh-config`, refusing anything unexpected.

    ssh runs ProxyCommand through a shell, so only the exact `openshell ssh-proxy` form is accepted.
    """
    hosts: list[str] = []
    proxies: list[str] = []
    for raw in text.splitlines():
        key, _, value = raw.strip().partition(" ")
        if key == "Host":
            hosts.append(value.strip())
        elif key == "ProxyCommand":
            proxies.append(value.strip())
    if len(hosts) != 1 or len(proxies) != 1 or not _SAFE.fullmatch(hosts[0]) or hosts[0].startswith("-"):
        raise ValueError("unrecognized_ssh_config")
    tokens = shlex.split(proxies[0])
    if (len(tokens) != 8 or tokens[1] != "ssh-proxy" or tuple(tokens[2::2]) != _PROXY_FLAGS
            or os.path.basename(tokens[0]) != os.path.basename(openshell)
            or not all(_SAFE.fullmatch(token) for token in tokens) or tokens[5] != sandbox):
        raise ValueError("unrecognized_ssh_proxy_command")
    return hosts[0], shlex.join(tokens)


class OpenShellSshLauncher:
    """Callable used by PersistentTransport; the remote command is fixed."""

    def __init__(self, cli: NemoClawCli, *, ssh: str = "ssh") -> None:
        self.cli, self.ssh = cli, ssh

    def __call__(self, sandbox: str) -> list[str]:
        result = self.cli.run("ssh_config", sandbox=sandbox)
        if not result.ok or result.dry_run:
            raise RuntimeError("ssh_config_unavailable")
        host, proxy = parse_ssh_config(result.stdout, sandbox, self.cli.openshell)
        options = [part for option in (f"ProxyCommand={proxy}", *_OPTIONS) for part in ("-o", option)]
        return [self.ssh, "-F", "/dev/null", "-T", *options, f"sandbox@{host}", shlex.join(SERVE_COMMAND)]
