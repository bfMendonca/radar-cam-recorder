from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from radar_cam_recorder.notify import notify


def test_notify_sends() -> None:
    # AF_UNIX paths are limited to ~104 bytes: pytest's tmp_path can exceed that.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        _check_send(Path(short) / "n")


def _check_send(path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(str(path))
        sock.settimeout(5)
        assert notify("READY=1", {"NOTIFY_SOCKET": str(path)}) is True
        assert sock.recv(64) == b"READY=1"
    finally:
        sock.close()


def test_notify_without_socket() -> None:
    assert notify("READY=1", {}) is False


def test_notify_unreachable_socket(tmp_path: Path) -> None:
    assert notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "missing")}) is False
