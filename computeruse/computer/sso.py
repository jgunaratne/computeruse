"""How a security-key (WebAuthn) prompt raised inside the sandboxed Chrome can reach a real key.

Corporate single sign-on usually ends with a security-key touch. The `browser` computer runs
Chrome on a virtual display on this machine, which has no key plugged in, so the request has to
be forwarded to a machine that does. Two transports exist on managed Linux hosts, and both are
inherited by Chrome from the *server's* environment:

* **Remote desktop.** Inside a Chrome Remote Desktop session the host sets
  `CHROME_REMOTE_DESKTOP_SESSION=1` and serves `chromoting.host_services_mojo_ipc` under
  `XDG_RUNTIME_DIR`; the policy-installed "Chrome Remote Desktop Security Key" extension proxies
  WebAuthn through it to the key on the client machine. (Chromium checks exactly that variable
  before it will connect: `remoting/host/chromoting_host_services_client.cc`.)
* **SSH agent.** `SSH_AUTH_SOCK` pointing at an agent that forwards security-key operations
  (security-key agent forwarding over SSH); the security-key helper extensions talk to it.

This module only *reports* what the environment offers so the console and `computeruse doctor`
can tell the operator what to expect; it never changes anything. The extensions themselves come
from enterprise policy, which is why `chrome_command` leaves Chrome's extension updater on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CRD_SESSION_VAR = "CHROME_REMOTE_DESKTOP_SESSION"
CRD_HOST_SERVICES_SOCKET = "chromoting.host_services_mojo_ipc"

NO_FORWARDING_HINT = (
    "No security-key forwarding detected: security-key prompts cannot be answered in the sandbox. "
    "Start the server from a terminal inside your remote-desktop session (so Chrome inherits it), "
    "or sign in with a one-time security code instead of touching a key."
)


def security_key_forwarding(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Describe the security-key transport Chrome would inherit from `env` (default: this process).

    Returns `transport` (`"remote-desktop"`, `"ssh-agent"` or `None`), `available`, a one-line
    `detail` for humans, and the raw facts (`crd_session`, `ssh_auth_sock`) for diagnostics.
    """
    env = os.environ if env is None else env
    runtime_dir = env.get("XDG_RUNTIME_DIR") or ""
    crd_socket = Path(runtime_dir, CRD_HOST_SERVICES_SOCKET) if runtime_dir else None
    crd_session = env.get(CRD_SESSION_VAR, "") not in ("", "0") and crd_socket is not None and crd_socket.exists()
    agent = env.get("SSH_AUTH_SOCK") or ""
    agent_ok = bool(agent) and Path(agent).exists()

    if crd_session:
        transport = "remote-desktop"
        detail = ("Security-key prompts raised in the sandbox are forwarded to the Chrome Remote Desktop client "
                  "you are connected from; touch the key plugged into that machine.")
    elif agent_ok:
        transport = "ssh-agent"
        detail = (f"Security-key requests go to the SSH agent at {agent}; this works when that agent forwards "
                  "security-key operations (security-key agent forwarding over SSH).")
    else:
        transport = None
        detail = NO_FORWARDING_HINT
    return {
        "transport": transport,
        "available": transport is not None,
        "detail": detail,
        "crd_session": bool(crd_session),
        "ssh_auth_sock": agent if agent_ok else None,
    }
