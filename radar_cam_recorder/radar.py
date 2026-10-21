"""Radar stream: the LD2451 driver feeding raw reads into segmented files."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ld2451
from ld2451.clock import default_clock

from .config import RadarConfig

__all__ = ["RadarInfo", "RadarStream", "SegmentedRadarWriter"]

log = logging.getLogger(__name__)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SegmentedRadarWriter:
    """Raw-listener target that writes ``t_rx_ns,hex`` lines and can switch files atomically.

    A read lands in exactly one file: ``write`` and ``rotate`` share one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recorder: ld2451.Recorder | None = None
        self._last_rx_ns: int | None = None
        self._reads = 0
        self._error: OSError | None = None
        self._write_errors = 0
        self._failing = False

    @property
    def write_errors(self) -> int:
        """Total writes that failed with OSError."""
        return self._write_errors

    def take_error(self) -> OSError | None:
        """Return the first stored write error (since the last call) and clear it."""
        with self._lock:
            error, self._error = self._error, None
            return error

    @property
    def last_rx_ns(self) -> int | None:
        return self._last_rx_ns

    @property
    def reads(self) -> int:
        return self._reads

    def open(self, path: Path) -> None:
        self.rotate(path)

    def rotate(self, path: Path) -> None:
        """Switch to a new file; the old one is flushed, fsynced and closed."""
        with self._lock:
            new = ld2451.Recorder(path)
            old, self._recorder = self._recorder, new
            if old is not None:
                self._finish(old)

    def write(self, t_rx_ns: int, data: bytes) -> None:
        with self._lock:
            self._last_rx_ns = t_rx_ns
            if self._recorder is None or not data:
                return
            try:
                self._recorder.write(t_rx_ns, data)
            except OSError as exc:
                # The driver only logs listener errors; keep it for the supervisor instead.
                self._write_errors += 1
                if self._error is None:
                    self._error = exc
                if not self._failing:
                    self._failing = True
                    log.warning("radar write failed: %s", exc)
                return
            self._failing = False
            self._reads += 1

    def flush(self) -> None:
        with self._lock:
            if self._recorder is not None:
                self._recorder.flush()

    def close(self) -> None:
        with self._lock:
            old, self._recorder = self._recorder, None
            if old is not None:
                self._finish(old)

    @staticmethod
    def _finish(recorder: ld2451.Recorder) -> None:
        try:
            recorder.flush()
            _fsync_path(recorder.path)
        finally:
            recorder.close()


@dataclass(frozen=True)
class RadarInfo:
    firmware: str | None
    params: dict[str, Any] | None


class RadarStream:
    def __init__(
        self,
        cfg: RadarConfig,
        transport_factory: Callable[[], ld2451.Transport] | None = None,
    ) -> None:
        self._cfg = cfg
        self._factory: Callable[[], ld2451.Transport] = transport_factory or (
            lambda: ld2451.SerialTransport(cfg.port, cfg.baudrate)
        )
        self._radar: ld2451.LD2451 | None = None
        self._started_ns = default_clock()[0]()

    @property
    def running(self) -> bool:
        return self._radar is not None and self._radar.running

    def start(self, writer: SegmentedRadarWriter) -> RadarInfo:
        """Open the radar and start recording into ``writer``.

        Raises ``ld2451.LD2451Error`` and ``OSError`` unchanged (after cleaning up).
        """
        self.stop()
        radar = ld2451.LD2451(self._factory())
        radar.add_raw_listener(writer.write)  # before open/start: keep the first bytes
        try:
            radar.open()
            radar.start()
            self._started_ns = default_clock()[0]()
            if self._cfg.detection is not None:
                radar.set_params(self._cfg.detection, verify=True)
            firmware = radar.get_firmware().version
            params = asdict(radar.get_params())
            params["direction"] = int(params["direction"])
        except BaseException:
            radar.close()
            raise
        self._radar = radar
        return RadarInfo(firmware=firmware, params=params)

    def stop(self) -> None:
        radar, self._radar = self._radar, None
        if radar is not None:
            radar.close()

    def silent_for_ns(self, now_ns: int, writer: SegmentedRadarWriter) -> int:
        last = writer.last_rx_ns
        return now_ns - (self._started_ns if last is None else last)
