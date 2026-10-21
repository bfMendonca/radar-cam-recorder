"""Simulated radar for tests and ``run --fake``: answers commands like the manual says."""

from __future__ import annotations

import ld2451
from ld2451.framing import FrameDecoder, FrameKind
from ld2451.protocol import CommandCode, decode_command, encode_ack

__all__ = ["FakeRadarResponder", "fake_radar_transport"]


class FakeRadarResponder:
    """Callable responder for :class:`ld2451.LoopbackTransport`."""

    def __init__(self) -> None:
        self.params = bytes.fromhex("64010502")
        self.sensitivity = bytes.fromhex("01000000")
        self.firmware = bytes.fromhex("5124010110150524")  # V1.01.24051510
        self.in_config = False
        self.received: list[int] = []
        self._decoder = FrameDecoder()

    def __call__(self, data: bytes) -> bytes | None:
        out = b""
        for frame in self._decoder.feed(data, 0):
            if frame.kind is FrameKind.COMMAND:
                code, value = decode_command(frame.payload)
                reply = self.handle(code, value)
                if reply:
                    out += reply
        return out or None

    def handle(self, code: int, value: bytes) -> bytes | None:
        self.received.append(code)
        if code == CommandCode.ENABLE_CONFIG:
            self.in_config = True
            return encode_ack(code, 0, bytes.fromhex("01000000"))
        if not self.in_config:
            return None  # the manual: commands outside config mode are ignored
        if code == CommandCode.END_CONFIG:
            self.in_config = False
            return encode_ack(code)
        if code == CommandCode.SET_PARAMS:
            self.params = value
            return encode_ack(code)
        if code == CommandCode.GET_PARAMS:
            return encode_ack(code, 0, self.params)
        if code == CommandCode.SET_SENSITIVITY:
            self.sensitivity = value
            return encode_ack(code)
        if code == CommandCode.GET_SENSITIVITY:
            return encode_ack(code, 0, self.sensitivity)
        if code == CommandCode.GET_FIRMWARE:
            return encode_ack(code, 0, self.firmware)
        return encode_ack(code, 1)


def fake_radar_transport() -> ld2451.LoopbackTransport:
    """A loopback transport wired to a new :class:`FakeRadarResponder`."""
    return ld2451.LoopbackTransport(responder=FakeRadarResponder(), timeout=0.01)
