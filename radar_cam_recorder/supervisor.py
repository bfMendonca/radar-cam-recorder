"""Supervisor: owns the session, rotates segments on the boot clock and stops in order."""

from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from radar_cam_recorder.camera.base import CameraRun, CameraSource
from radar_cam_recorder.camera.metadata import parse_metadata, write_frames_csv
from radar_cam_recorder.clocks import Clock, boot_clock, boot_id, ntp_synced, sample_clocks
from radar_cam_recorder.config import Config, config_to_dict
from radar_cam_recorder.radar import RadarStream, SegmentedRadarWriter
from radar_cam_recorder.session import SegmentPaths, Session, SessionState, SessionStore
from radar_cam_recorder.status import State, StatusBus, StatusEvent
from radar_cam_recorder.storage import StorageCheck, check_storage

__all__ = ["Supervisor"]

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class _PendingGap:
    """A rotation whose video gap is reported once the new segment has a frame."""

    segment: SegmentPaths
    last_old_ts: int | None


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        *,
        bus: StatusBus,
        camera: CameraSource | None,
        radar: RadarStream | None,
        store: SessionStore | None = None,
        clock: Clock | None = None,
        storage_check: Callable[[Path, bool], StorageCheck] = check_storage,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._camera = camera if camera is not None and cfg.camera.enabled else None
        self._radar = radar if radar is not None and cfg.radar.enabled else None
        self._store = store if store is not None else SessionStore(cfg.storage.recordings_dir)
        self._clock = clock if clock is not None else boot_clock()
        self._storage_check = storage_check
        self._writer = SegmentedRadarWriter()
        self._session: Session | None = None
        self._segment: SegmentPaths | None = None
        self._segment_start_ns = 0
        self._next_check_ns = 0
        self._camera_run: CameraRun | None = None
        self._pending_gap: _PendingGap | None = None
        self._shutdown_lock = threading.Lock()
        self._shut_down = False
        bus.add_sink(self)

    @property
    def session(self) -> Session | None:
        return self._session

    def handle(self, event: StatusEvent) -> None:
        session = self._session
        if session is not None:
            session.append_event(event)

    def run(self, stop: threading.Event, tick_s: float = 0.5) -> None:
        try:
            self._recover()
            while not stop.is_set():
                self.tick()
                stop.wait(tick_s)
        finally:
            self.shutdown()

    def tick(self) -> None:
        if self._shut_down:
            return
        now = self._clock()
        if self._session is None:
            self._open_session(now)
            return
        if now - self._segment_start_ns >= self._cfg.segment_s * 1e9:
            self._rotate(now)
        elif self._pending_gap is not None:
            self._report_gap()
        if now >= self._next_check_ns:
            self._next_check_ns = now + int(self._cfg.storage.check_interval_s * 1e9)
            self._writer.flush()

    def shutdown(self, reason: str = "stopped by signal") -> None:
        """Stop in order (spec 6.3); idempotent, bounded and never raises."""
        with self._shutdown_lock:
            if self._shut_down:
                return
            self._shut_down = True
        try:
            self._bus.set_state(State.STOPPING, reason)
            if self._session is not None:
                self._close_session(SessionState.COMPLETE, reason)
        except Exception:
            log.exception("error during shutdown")
        finally:
            self._bus.set_state(State.STOPPED, reason)
            self._bus.close()

    # -- session and segments -------------------------------------------------------------

    def _recover(self) -> None:
        check = self._check_storage()
        if not check.usable:
            log.warning("skipping recovery: %s", check.reason)
            return
        for session_id in self._store.recover_interrupted():
            log.warning("session %s marked interrupted", session_id)

    def _check_storage(self) -> StorageCheck:
        storage = self._cfg.storage
        return self._storage_check(storage.recordings_dir, storage.require_mountpoint)

    def _open_session(self, now: int) -> None:
        if not self._check_storage().usable:
            return
        session = self._store.create(
            config_to_dict(self._cfg), boot_id(), sample_clocks(boot=self._clock)
        )
        self._session = session
        segment = session.segment(0)
        devices: dict[str, Any] = {"radar": None, "camera": None}
        if self._radar is not None:
            self._writer.open(segment.radar)
            info = self._radar.start(self._writer)
            devices["radar"] = {"firmware": info.firmware, "params": info.params}
        if self._camera is not None:
            devices["camera"] = self._camera.describe()
        self._start_camera_segment(segment, now)
        session.update(
            devices=devices,
            setup={"notes": self._cfg.setup.notes, "geometry": dict(self._cfg.setup.geometry)},
            ntp_synced=ntp_synced(),
        )
        self._next_check_ns = now + int(self._cfg.storage.check_interval_s * 1e9)
        self._bus.set_state(State.RECORDING, "recording")

    def _start_camera_segment(self, segment: SegmentPaths, now: int) -> None:
        self._segment = segment
        self._segment_start_ns = now
        if self._camera is not None:
            self._camera_run = self._camera.start(segment)

    def _stop_camera(self) -> int:
        """Stop the camera run and write the segment's frames CSV; return its frame count."""
        run, self._camera_run = self._camera_run, None
        if run is not None:
            run.stop(self._cfg.camera.stop_timeout_s)
        segment = self._segment
        if segment is None or not segment.meta.exists():
            return 0
        return write_frames_csv(segment.meta, segment.frames)

    def _rotate(self, now: int) -> None:
        session, old = self._session, self._segment
        assert session is not None and old is not None
        frames = self._stop_camera()
        new = session.segment(old.index + 1)
        if self._radar is not None:
            self._writer.rotate(new.radar)
        start_ns = self._segment_start_ns
        self._start_camera_segment(new, now)
        session.add_segment(old.index, start_ns, now, frames)
        session.add_clock_sample(f"segment {new.index}", sample_clocks(boot=self._clock))
        self._pending_gap = _PendingGap(new, _last_sensor_ts(old.frames))

    def _report_gap(self) -> None:
        pending = self._pending_gap
        assert pending is not None
        if pending.last_old_ts is None:
            self._pending_gap = None
            return
        if not pending.segment.meta.exists():
            return
        frames = parse_metadata(pending.segment.meta.read_text(encoding="utf-8", errors="replace"))
        first = next((f["SensorTimestamp"] for f in frames if "SensorTimestamp" in f), None)
        if first is None:
            return
        self._pending_gap = None
        self._bus.incident(
            "segment gap", segment=pending.segment.index, gap_ns=int(first) - pending.last_old_ts
        )

    def _close_session(self, state: SessionState, reason: str) -> None:
        """Close everything in spec 6.3 order; a failing step is logged and the rest still run."""
        session, segment = self._session, self._segment
        assert session is not None and segment is not None
        frames = _attempt("stop camera", self._stop_camera, 0)
        if self._radar is not None:
            _attempt("stop radar", self._radar.stop, None)
        _attempt("close radar file", self._writer.close, None)
        end = self._clock()
        _attempt(
            "record last segment",
            lambda: session.add_segment(segment.index, self._segment_start_ns, end, frames),
            None,
        )
        session.close(state, reason, sample_clocks(boot=self._clock))


def _attempt(what: str, fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception:
        log.exception("cannot %s", what)
        return default


def _last_sensor_ts(frames_csv: Path) -> int | None:
    try:
        with open(frames_csv, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    if not rows:
        return None
    try:
        return int(rows[-1]["sensor_ts_ns"])
    except (KeyError, ValueError):
        return None
