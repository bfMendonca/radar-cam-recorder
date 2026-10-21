"""Minimal sd_notify: READY=1 and WATCHDOG=1 to systemd's NOTIFY_SOCKET."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping


def notify(message: str, env: Mapping[str, str] = os.environ) -> bool:
    path = env.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), path)
    except OSError:
        return False
    return True
